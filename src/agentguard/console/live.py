"""Live console updates over Server-Sent Events.

The stream tells the console *what changed* (new or finished tool calls,
approval state changes), scoped to the principal's tenants, and the console
refetches the affected views. Changes are found with a timestamp cursor on
the shared database, so any gateway replica can serve any client and no
extra infrastructure is needed; a pub/sub fan-out (Redis is provisioned)
is the step up if polling the database ever becomes the bottleneck.
"""

import asyncio
import json
from collections import OrderedDict
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from agentguard.console.auth import Principal
from agentguard.console.service import _agent_names, _execution_summary, _scoped
from agentguard.db import SessionLocal
from agentguard.models import Approval, Execution

POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 15.0
OVERLAP = timedelta(seconds=2)  # re-read a little behind the cursor so commits in flight aren't missed


def collect_changes(session: Session, principal: Principal, since: datetime) -> dict[str, Any]:
    executions = (
        _scoped(session.query(Execution), principal, Execution.tenant_id)
        .filter(or_(Execution.requested_at >= since, Execution.executed_at >= since, Execution.completed_at >= since))
        .order_by(Execution.requested_at)
        .limit(200)
        .all()
    )
    approvals = (
        _scoped(session.query(Approval), principal, Approval.tenant_id)
        .filter(or_(Approval.created_at >= since, Approval.decided_at >= since, Approval.consumed_at >= since))
        .all()
    )
    names = _agent_names(session) if executions else {}
    return {
        "executions": [_execution_summary(e, names) for e in executions],
        "approvals": [{"id": a.id, "status": a.status, "execution_id": a.execution_id} for a in approvals],
    }


class _Seen:
    """Bounded memory of (object, state) pairs already sent, so the overlap window doesn't repeat events."""

    def __init__(self, size: int = 2000):
        self.items: OrderedDict[str, None] = OrderedDict()
        self.size = size

    def first_time(self, key: str) -> bool:
        if key in self.items:
            return False
        self.items[key] = None
        if len(self.items) > self.size:
            self.items.popitem(last=False)
        return True


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def stream(principal: Principal, is_disconnected) -> AsyncIterator[str]:
    seen = _Seen()
    cursor = datetime.now(UTC)
    yield _sse("ready", {"poll_seconds": POLL_SECONDS})
    last_sent = asyncio.get_running_loop().time()
    while not await is_disconnected():
        polled_at = datetime.now(UTC)

        def poll(since=cursor - OVERLAP) -> dict[str, Any]:
            with SessionLocal() as session:
                return collect_changes(session, principal, since)

        changes = await asyncio.to_thread(poll)
        cursor = polled_at
        fresh = {
            "executions": [e for e in changes["executions"] if seen.first_time(f"e:{e['id']}:{e['outcome']}")],
            "approvals": [a for a in changes["approvals"] if seen.first_time(f"a:{a['id']}:{a['status']}")],
        }
        now = asyncio.get_running_loop().time()
        if fresh["executions"] or fresh["approvals"]:
            yield _sse("change", fresh)
            last_sent = now
        elif now - last_sent >= HEARTBEAT_SECONDS:
            yield ": heartbeat\n\n"  # keeps proxies from closing an idle stream
            last_sent = now
        await asyncio.sleep(POLL_SECONDS)
