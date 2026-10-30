"""In-memory stand-in for the external systems the mock tools act on.

Behaves like a real API in the ways that matter here:
- every call needs a credential valid for a specific scope, so holding
  the refund credential does not let you create tickets (least privilege),
  and calling without the broker's credential fails (gateway bypass);
- writes are deduplicated on an idempotency key, and the key can be looked
  up afterwards, which is how the broker reconciles after a timeout.

Kept separate from `agentguard.broker` so read-only consumers (replay) can
inspect state without importing anything capable of changing it.
"""

import os
import threading
from typing import Any

SCOPES = ("tickets:read", "tickets:write", "billing:refund")


class Unauthorized(Exception):
    pass


def expected_credential(scope: str) -> str:
    """What this external system accepts for a scope.

    Synthetic by default; set AGENTGUARD_CRED_<SCOPE> to override (same as the broker).
    """
    env = "AGENTGUARD_CRED_" + scope.replace(":", "_").upper()
    return os.environ.get(env, f"dev-only-{scope}")


TICKETS: dict[str, dict[str, Any]] = {
    "T-1001": {"ticket_id": "T-1001", "subject": "Can't log in", "status": "open"},
}
_APPLIED_WRITES: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _authorize(credential: str | None, scope: str) -> None:
    if credential is None or credential != expected_credential(scope):
        raise Unauthorized(f"credential not valid for scope {scope!r}")


def get_ticket(credential: str | None, ticket_id: str) -> dict[str, Any] | None:
    _authorize(credential, "tickets:read")
    return TICKETS.get(ticket_id)


def create_ticket(credential: str | None, idempotency_key: str, subject: str) -> dict[str, Any]:
    _authorize(credential, "tickets:write")
    with _lock:
        if idempotency_key in _APPLIED_WRITES:
            return _APPLIED_WRITES[idempotency_key]
        ticket_id = f"T-{1000 + len(TICKETS) + 1}"
        TICKETS[ticket_id] = {"ticket_id": ticket_id, "subject": subject, "status": "open"}
        result = {"created": True, **TICKETS[ticket_id]}
        _APPLIED_WRITES[idempotency_key] = result
        return result


def issue_refund(credential: str | None, idempotency_key: str, ticket_id: str | None, amount: float) -> dict[str, Any]:
    _authorize(credential, "billing:refund")
    with _lock:
        if idempotency_key in _APPLIED_WRITES:
            return _APPLIED_WRITES[idempotency_key]
        result = {"refunded": True, "ticket_id": ticket_id, "amount": amount}
        _APPLIED_WRITES[idempotency_key] = result
        return result


def lookup_write(credential: str | None, scope: str, idempotency_key: str) -> dict[str, Any] | None:
    """Did a write with this key happen? Requires a credential for the same scope."""
    _authorize(credential, scope)
    with _lock:
        return _APPLIED_WRITES.get(idempotency_key)
