"""Dry-run executors used by replay.

Replay never imports `agentguard.broker` — that is the only module that
can perform a real side effect, and keeping it out of this package's
import graph is what makes "replay cannot cause a real action" a
structural property rather than a convention (edge case #16 in
docs/THREAT_MODEL.md). tests/test_replay.py checks this.
"""

import copy
from collections.abc import Callable
from typing import Any

from agentguard.mock_systems import TICKETS

DryRunFn = Callable[[dict[str, Any]], dict[str, Any]]


def _read_ticket(args: dict[str, Any]) -> dict[str, Any]:
    # Read against a snapshot so even an accidental mutation can't leak back.
    ticket = copy.deepcopy(TICKETS).get(args.get("ticket_id"))
    return {"dry_run": True, "found": ticket is not None, **(ticket or {})}


def _would_write(tool: str) -> DryRunFn:
    def run(args: dict[str, Any]) -> dict[str, Any]:
        return {"dry_run": True, "would_execute": tool, "args": args}

    return run


DRY_RUN_EXECUTORS: dict[str, DryRunFn] = {
    "tickets.read": _read_ticket,
    "tickets.create": _would_write("create-ticket"),
    "billing.refund": _would_write("issue-refund"),
}
