"""Mock tool executors.

Each executor declares the single scope it needs; the broker hands it a
credential for that scope only, never anything the agent supplied. A
compromised or confused executor for refunds therefore cannot create
tickets, and vice versa.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agentguard import mock_systems

RunFn = Callable[[dict[str, Any], str, str], dict[str, Any]]  # (args, credential, idempotency_key)


@dataclass(frozen=True)
class Executor:
    run: RunFn
    scope: str
    writes: bool  # whether a call can change external state (and so needs reconciliation)


def _read_ticket(args: dict[str, Any], credential: str, _key: str) -> dict[str, Any]:
    ticket = mock_systems.get_ticket(credential, args["ticket_id"])
    if ticket is None:
        return {"found": False, "ticket_id": args["ticket_id"]}
    return {"found": True, **ticket}


def _create_ticket(args: dict[str, Any], credential: str, key: str) -> dict[str, Any]:
    return mock_systems.create_ticket(credential, key, args["subject"])


def _issue_refund(args: dict[str, Any], credential: str, key: str) -> dict[str, Any]:
    return mock_systems.issue_refund(credential, key, args.get("ticket_id"), args["amount"])


EXECUTORS: dict[str, Executor] = {
    "mock.read_ticket": Executor(_read_ticket, "tickets:read", writes=False),
    "mock.create_ticket": Executor(_create_ticket, "tickets:write", writes=True),
    "mock.issue_refund": Executor(_issue_refund, "billing:refund", writes=True),
}
