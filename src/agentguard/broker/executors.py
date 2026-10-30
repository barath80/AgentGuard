"""Tool executors, one per tool, each holding exactly one credential scope.

The broker hands an executor a credential for its own scope only, never
anything the agent supplied, so the refund executor cannot create tickets
and vice versa. Executor refs are logical (`billing.refund`); which system
actually serves them is the configured backend:

- `helpdesk`: the real ticketing/billing service over HTTP (`src/helpdesk/`),
  which enforces scopes, tenant boundaries and idempotency itself.
- `inprocess`: the in-memory test double in `agentguard.mock_systems`, used by
  the test suite and the evaluation runner so they need no running services.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from agentguard import mock_systems
from agentguard.config import settings

# (args, credential, idempotency_key, tenant_id) -> result
RunFn = Callable[[dict[str, Any], str, str, str], dict[str, Any]]
# (credential, idempotency_key) -> recorded result, or None if the write never landed
LookupFn = Callable[[str, str], dict[str, Any] | None]


class TargetRejected(Exception):
    """The target system refused the call (e.g. a business rule). No side effect happened."""


@dataclass(frozen=True)
class Executor:
    run: RunFn
    scope: str
    writes: bool  # whether a call can change external state (and so needs reconciliation)
    lookup: LookupFn | None = None


# --- In-process test double ------------------------------------------------------


def _mock_read_ticket(args: dict[str, Any], credential: str, _key: str, _tenant: str) -> dict[str, Any]:
    ticket = mock_systems.get_ticket(credential, args["ticket_id"])
    if ticket is None:
        return {"found": False, "ticket_id": args["ticket_id"]}
    return {"found": True, **ticket}


def _mock_create_ticket(args: dict[str, Any], credential: str, key: str, _tenant: str) -> dict[str, Any]:
    return mock_systems.create_ticket(credential, key, args["subject"])


def _mock_issue_refund(args: dict[str, Any], credential: str, key: str, _tenant: str) -> dict[str, Any]:
    return mock_systems.issue_refund(credential, key, args.get("ticket_id"), args["amount"])


def _mock_lookup(scope: str) -> LookupFn:
    return lambda credential, key: mock_systems.lookup_write(credential, scope, key)


EXECUTORS: dict[str, Executor] = {
    "tickets.read": Executor(_mock_read_ticket, "tickets:read", writes=False),
    "tickets.create": Executor(_mock_create_ticket, "tickets:write", writes=True, lookup=_mock_lookup("tickets:write")),
    "billing.refund": Executor(_mock_issue_refund, "billing:refund", writes=True, lookup=_mock_lookup("billing:refund")),
}


# --- Real helpdesk service ---------------------------------------------------------


def _client(credential: str, tenant_id: str | None = None, key: str | None = None) -> httpx.Client:
    headers = {"Authorization": f"Bearer {credential}"}
    if tenant_id:
        headers["X-Tenant-Id"] = tenant_id
    if key:
        headers["Idempotency-Key"] = key
    return httpx.Client(base_url=settings.helpdesk_url, headers=headers, timeout=settings.broker_timeout_seconds + 5)


def _raise_for(response: httpx.Response) -> None:
    if response.status_code in (404, 409, 422):
        detail = response.json().get("detail", response.text) if response.content else response.reason_phrase
        raise TargetRejected(f"helpdesk rejected the call: {detail}")
    response.raise_for_status()


def _helpdesk_read_ticket(args: dict[str, Any], credential: str, _key: str, tenant: str) -> dict[str, Any]:
    with _client(credential, tenant) as http:
        response = http.get(f"/v1/tickets/{args['ticket_id']}")
    if response.status_code == 404:
        return {"found": False, "ticket_id": args["ticket_id"]}
    _raise_for(response)
    return {"found": True, **response.json()}


def _helpdesk_create_ticket(args: dict[str, Any], credential: str, key: str, tenant: str) -> dict[str, Any]:
    body = {"subject": args["subject"], **({"body": args["body"]} if "body" in args else {})}
    with _client(credential, tenant, key) as http:
        response = http.post("/v1/tickets", json=body)
    _raise_for(response)
    return response.json()


def _helpdesk_issue_refund(args: dict[str, Any], credential: str, key: str, tenant: str) -> dict[str, Any]:
    body = {"ticket_id": args.get("ticket_id"), "amount": args["amount"], "note": args.get("note", "")}
    with _client(credential, tenant, key) as http:
        response = http.post("/v1/refunds", json=body)
    _raise_for(response)
    return response.json()


def _helpdesk_lookup(scope: str) -> LookupFn:
    def lookup(credential: str, key: str) -> dict[str, Any] | None:
        with _client(credential) as http:
            response = http.get(f"/v1/writes/{key}", params={"scope": scope})
        response.raise_for_status()
        record = response.json()
        return record.get("result") if record.get("applied") else None

    return lookup


HELPDESK_EXECUTORS: dict[str, Executor] = {
    "tickets.read": Executor(_helpdesk_read_ticket, "tickets:read", writes=False),
    "tickets.create": Executor(
        _helpdesk_create_ticket, "tickets:write", writes=True, lookup=_helpdesk_lookup("tickets:write")
    ),
    "billing.refund": Executor(
        _helpdesk_issue_refund, "billing:refund", writes=True, lookup=_helpdesk_lookup("billing:refund")
    ),
}


def registry() -> dict[str, Executor]:
    if settings.target_backend == "helpdesk":
        return HELPDESK_EXECUTORS
    if settings.target_backend == "inprocess":
        return EXECUTORS
    raise ValueError(f"unknown TARGET_BACKEND {settings.target_backend!r}")
