"""Runtime controls checked before policy: kill switch, circuit breaker,
rate limit, spend budget (docs/THREAT_MODEL.md #14).

All state is derived from the execution trace itself rather than separate
counters, so the controls can't drift from what actually happened and
need no extra infrastructure. Each check returns a denial reason, or None.
Reasons look like "control:<kind> <message>" (see `Execution.reason`).
"""

import threading
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from agentguard.config import settings
from agentguard.models import Agent, Execution, RequestPayload, Tool

CONTROL_PREFIX = "control:"
SPEND_COUNTED_OUTCOMES = ("success", "timeout")  # a timed-out call may have applied

_local_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_local_locks_guard = threading.Lock()


@contextmanager
def agent_lock(session: Session, agent_id: str) -> Iterator[None]:
    """Serialise one agent's control checks with the recording of its decision.

    Rate limits and budgets are check-then-act: without this, concurrent calls (several
    gateway replicas, or one replica's thread pool) could all pass a check that only one
    should. On Postgres it's a row lock on the agent, held until the decision commits, so
    it holds across every replica. SQLite (dev/test, one process) uses an in-process lock.
    Tool execution happens after the decision commits, outside the lock.
    """
    if session.get_bind().dialect.name == "postgresql":
        session.query(Agent.id).filter(Agent.id == agent_id).with_for_update().one_or_none()
        yield  # released by the decision's commit (or any rollback)
        return
    with _local_locks_guard:
        lock = _local_locks[agent_id]
    with lock:
        yield


def _reason(kind: str, message: str) -> str:
    return f"{CONTROL_PREFIX}{kind} {message}"


def control_kind(reason: str | None) -> str | None:
    if not reason or not reason.startswith(CONTROL_PREFIX):
        return None
    return reason[len(CONTROL_PREFIX):].split(" ", 1)[0]


def _since(seconds: float) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


def spent_last_24h(session: Session, agent_id: str) -> float:
    rows = (
        session.query(RequestPayload.args, Tool.spend_arg)
        .join(Execution, Execution.id == RequestPayload.execution_id)
        .join(Tool, Tool.id == Execution.tool_id)
        .filter(
            Execution.agent_id == agent_id,
            # Spent, or allowed and still executing: in-flight spend is reserved, so a burst of
            # concurrent calls can't each see the old total and all pass.
            Execution.outcome.in_(SPEND_COUNTED_OUTCOMES)
            | ((Execution.decision == "allow") & (Execution.outcome == "not_attempted")),
            Execution.requested_at >= _since(24 * 3600),
            Tool.spend_arg.is_not(None),
        )
        .all()
    )
    return sum(float(args.get(spend_arg, 0)) for args, spend_arg in rows)


def budget_denial(session: Session, agent: Agent, tool: Tool, args: dict[str, Any]) -> str | None:
    if agent.daily_spend_limit is None or tool.spend_arg is None:
        return None
    requested = float(args.get(tool.spend_arg, 0))
    if spent_last_24h(session, agent.id) + requested > agent.daily_spend_limit:
        return _reason("budget", f"daily spend budget of {agent.daily_spend_limit:g} would be exceeded")
    return None


def pre_policy_denial(session: Session, agent_id: str, tool: Tool, args: dict[str, Any]) -> str | None:
    if settings.kill_switch:
        return _reason("kill_switch", "global kill switch is engaged")

    agent = session.get(Agent, agent_id)
    breaker_since = _since(settings.breaker_window_seconds)
    minute_ago = _since(60)
    # Both counts in one query over the agent's recent calls (ix_executions_agent_requested).
    denials, calls = session.query(
        func.coalesce(func.sum(case((
            (Execution.requested_at >= breaker_since) & (Execution.decision == "deny")
            # Don't count our own control denials, or the breaker could never close.
            & ((Execution.reason.is_(None)) | (~Execution.reason.startswith(CONTROL_PREFIX))), 1), else_=0)), 0),
        func.coalesce(func.sum(case((Execution.requested_at >= minute_ago, 1), else_=0)), 0),
    ).filter(Execution.agent_id == agent_id, Execution.requested_at >= min(breaker_since, minute_ago)).one()
    if denials >= settings.breaker_threshold:
        return _reason("circuit_open", f"after {denials} denials in {settings.breaker_window_seconds}s")

    if calls >= agent.rate_limit_per_minute:
        return _reason("rate_limit", f"{agent.rate_limit_per_minute}/min exceeded")

    return budget_denial(session, agent, tool, args)


def set_agent_status(session: Session, workload_identity_ref: str, status: str) -> Agent:
    """Per-agent kill switch: a suspended agent fails identity resolution on its next call."""
    agent = session.query(Agent).filter(Agent.workload_identity_ref == workload_identity_ref).one_or_none()
    if agent is None:
        raise ValueError(f"no agent with workload identity {workload_identity_ref!r}")
    agent.status = status
    session.commit()
    return agent
