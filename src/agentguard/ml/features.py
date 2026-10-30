"""Per-agent behaviour windows built from the execution trace.

One row per (agent, fixed time window). Everything comes from recorded
executions and their stored arguments; nothing is self-reported by agents.
"""

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from agentguard.models import Execution, RequestPayload

WINDOW = timedelta(minutes=15)
# Span for `burst_calls`: how bunched a window's calls are, not just how many. Reading twenty
# tickets in two seconds and twenty over a minute have the same count but different intent.
BURST_SPAN = timedelta(seconds=10)

FEATURES = [
    "calls",
    "burst_calls",
    "deny_rate",
    "approval_rate",
    "policy_denials",
    "control_denials",
    "tenant_mismatches",
    "schema_rejections",
    "environment_rejections",
    "distinct_tools",
    "distinct_targets",
    "refunds",
    "refund_amount",
    "max_refund",
    "refunds_without_read",
    "failures",
    "timeouts",
]
# Heavy-tailed counts and amounts are log-scaled so one big window doesn't dominate.
LOG_SCALED = {"calls", "burst_calls", "policy_denials", "control_denials", "tenant_mismatches", "schema_rejections",
              "environment_rejections", "distinct_targets", "refunds", "refund_amount", "max_refund",
              "refunds_without_read", "failures", "timeouts"}


@dataclass
class Window:
    agent_id: str
    start: datetime
    values: dict[str, float] = field(default_factory=dict)
    execution_ids: list[str] = field(default_factory=list)

    def vector(self) -> list[float]:
        return [math.log1p(self.values[f]) if f in LOG_SCALED else self.values[f] for f in FEATURES]


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _window_start(ts: datetime, window: timedelta = WINDOW) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return epoch + ((ts - epoch) // window) * window


def build_windows(session: Session, *, since: datetime | None = None, window: timedelta = WINDOW) -> list[Window]:
    """One row per (agent, `window`-long bucket) that has at least one call."""
    query = session.query(Execution).order_by(Execution.requested_at)
    if since is not None:
        query = query.filter(Execution.requested_at >= since)
    executions = query.all()
    payloads = {
        p.execution_id: p.args or {}
        for p in session.query(RequestPayload).filter(RequestPayload.execution_id.in_([e.id for e in executions]))
    } if executions else {}

    grouped: dict[tuple[str, datetime], list[Execution]] = defaultdict(list)
    for e in executions:
        grouped[(e.agent_id, _window_start(_utc(e.requested_at), window))].append(e)

    read_tickets: dict[str, set] = defaultdict(set)  # per agent, across windows, in time order
    windows = []
    for (agent_id, start), rows in sorted(grouped.items(), key=lambda kv: kv[0][1]):
        n = len(rows)
        reasons = [(e.reason or "") for e in rows if e.decision == "deny"]
        refunds = [e for e in rows if e.tool_name == "issue-refund"]
        amounts = [float(payloads.get(e.id, {}).get("amount", 0) or 0) for e in refunds]
        without_read = 0
        for e in rows:
            ticket = payloads.get(e.id, {}).get("ticket_id")
            if e.tool_name == "read-ticket" and e.outcome == "success":
                read_tickets[agent_id].add(ticket)
            elif e.tool_name == "issue-refund" and ticket not in read_tickets[agent_id]:
                without_read += 1
        times = [_utc(e.requested_at) for e in rows]  # rows are in time order
        burst, first = 0, 0
        for last in range(n):
            while times[last] - times[first] > BURST_SPAN:
                first += 1
            burst = max(burst, last - first + 1)
        windows.append(Window(
            agent_id=agent_id,
            start=start,
            execution_ids=[e.id for e in rows],
            values={
                "calls": n,
                "burst_calls": burst,
                "deny_rate": len(reasons) / n,
                "approval_rate": sum(e.decision == "approval_required" for e in rows) / n,
                "policy_denials": sum(r == "denied by policy" for r in reasons),
                "control_denials": sum(r.startswith("control:") for r in reasons),
                "tenant_mismatches": sum(r.startswith("tenant mismatch") for r in reasons),
                "schema_rejections": sum(r.startswith("unexpected argument") for r in reasons),
                "environment_rejections": sum(r.startswith("environment not approved") for r in reasons),
                "distinct_tools": len({e.tool_name for e in rows}),
                "distinct_targets": len({e.target_resource for e in rows}),
                "refunds": len(refunds),
                "refund_amount": sum(amounts),
                "max_refund": max(amounts, default=0.0),
                "refunds_without_read": without_read,
                "failures": sum(e.outcome == "failure" for e in rows),
                "timeouts": sum(e.outcome == "timeout" for e in rows),
            },
        ))
    return windows
