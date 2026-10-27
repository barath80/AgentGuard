"""Mock tool executors.

Stand in for real external systems. Each executor gets only the
arguments it needs — never a raw credential the agent could have
supplied — and simulates a plausible result. Real integrations would
each hold their own narrowly-scoped credential, injected here by the
broker, not by the agent.
"""

from collections.abc import Callable
from typing import Any

from agentguard.mock_systems import TICKETS

ExecutorFn = Callable[[dict[str, Any]], dict[str, Any]]


def read_ticket(args: dict[str, Any]) -> dict[str, Any]:
    ticket_id = args["ticket_id"]
    ticket = TICKETS.get(ticket_id)
    if ticket is None:
        return {"found": False, "ticket_id": ticket_id}
    return {"found": True, **ticket}


def create_ticket(args: dict[str, Any]) -> dict[str, Any]:
    ticket_id = f"T-{1000 + len(TICKETS) + 1}"
    ticket = {"ticket_id": ticket_id, "subject": args["subject"], "status": "open"}
    TICKETS[ticket_id] = ticket
    return {"created": True, **ticket}


def issue_refund(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "refunded": True,
        "ticket_id": args.get("ticket_id"),
        "amount": args["amount"],
    }


EXECUTORS: dict[str, ExecutorFn] = {
    "mock.read_ticket": read_ticket,
    "mock.create_ticket": create_ticket,
    "mock.issue_refund": issue_refund,
}
