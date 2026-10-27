"""Operational stats derived from the execution trace (served at /v1/stats)."""

import statistics
from collections import Counter
from typing import Any

from sqlalchemy.orm import Session

from agentguard.models import Approval, Execution


def _percentile(values: list[float], pct: int) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[pct - 1]


def compute_stats(session: Session) -> dict[str, Any]:
    executions = session.query(Execution).all()
    terminal = [e for e in executions if e.outcome != "not_attempted"]
    latencies_ms = [
        (e.decided_at - e.requested_at).total_seconds() * 1000
        for e in executions
        if e.decided_at is not None and e.requested_at is not None
    ]
    denial_reasons = Counter(e.reason or "unspecified" for e in executions if e.decision == "deny")

    return {
        "total_calls": len(executions),
        "by_decision": dict(Counter(e.decision for e in executions)),
        "by_outcome": dict(Counter(e.outcome for e in executions)),
        "denials_by_reason": dict(denial_reasons),
        "trace_completeness": (sum(e.trace_complete for e in terminal) / len(terminal)) if terminal else None,
        "unresolved_timeouts": sum(1 for e in executions if e.outcome == "timeout"),
        "pending_approvals": session.query(Approval).filter(Approval.status == "pending").count(),
        "decision_latency_ms": {
            "p50": _percentile(latencies_ms, 50),
            "p95": _percentile(latencies_ms, 95),
        },
    }
