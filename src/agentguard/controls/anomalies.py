"""Offline anomaly detection over the execution trace (report only, read-only).

Enforcement happens inline in controls/service.py; this surfaces patterns
for a human to look at, including ones that were already blocked.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from agentguard.config import settings
from agentguard.controls.service import CONTROL_PREFIX, control_kind
from agentguard.models import AuditEvent, Execution, RequestPayload


@dataclass(frozen=True)
class Finding:
    kind: str
    agent_id: str
    detail: str
    execution_ids: list[str] = field(default_factory=list)


def _denial_bursts(by_agent: dict[str, list[Execution]]) -> list[Finding]:
    window = timedelta(seconds=settings.breaker_window_seconds)
    findings = []
    for agent_id, executions in by_agent.items():
        denials = [
            e for e in executions
            if e.decision == "deny" and not (e.reason or "").startswith(CONTROL_PREFIX)
        ]
        start = 0
        for end in range(len(denials)):
            while denials[end].requested_at - denials[start].requested_at > window:
                start += 1
            if end - start + 1 >= settings.breaker_threshold:
                burst = denials[start:end + 1]
                findings.append(Finding(
                    "denial_burst", agent_id,
                    f"{len(burst)} denials within {settings.breaker_window_seconds}s",
                    [e.id for e in burst],
                ))
                break
    return findings


def _control_denials(by_agent: dict[str, list[Execution]]) -> list[Finding]:
    """Rate-limit hits are call spikes; budget hits are overruns; circuit hits follow bursts."""
    findings = []
    for agent_id, executions in by_agent.items():
        by_kind: dict[str, list[Execution]] = defaultdict(list)
        for e in executions:
            kind = control_kind(e.reason)
            if kind:
                by_kind[kind].append(e)
        for kind, rows in by_kind.items():
            findings.append(Finding(
                f"control:{kind}", agent_id, f"{len(rows)} calls denied ({rows[0].reason})", [e.id for e in rows],
            ))
    return findings


def _ticket_ids(session: Session, executions: list[Execution]) -> dict[str, str | None]:
    """Ticket id per read/refund call, fetched in batches rather than one query per call."""
    ids = [e.id for e in executions if e.tool_name in ("read-ticket", "issue-refund")]
    tickets: dict[str, str | None] = {}
    for i in range(0, len(ids), 500):
        for execution_id, args in session.query(RequestPayload.execution_id, RequestPayload.args).filter(
            RequestPayload.execution_id.in_(ids[i:i + 500])
        ):
            tickets[execution_id] = (args or {}).get("ticket_id")
    return tickets


def _refund_without_read(session: Session, by_agent: dict[str, list[Execution]]) -> list[Finding]:
    """A refund for a ticket the agent never successfully read is an unusual sequence."""
    tickets = _ticket_ids(session, [e for rows in by_agent.values() for e in rows])
    findings = []
    for agent_id, executions in by_agent.items():
        read_tickets: set[str] = set()
        for e in executions:
            ticket_id = tickets.get(e.id)
            if e.tool_name == "read-ticket" and e.outcome == "success":
                read_tickets.add(ticket_id)
            elif e.tool_name == "issue-refund" and ticket_id not in read_tickets:
                findings.append(Finding(
                    "refund_without_read", agent_id,
                    f"refund for ticket {ticket_id} with no prior successful read ({e.decision}/{e.outcome})",
                    [e.id],
                ))
    return findings


def _unresolved_timeouts(executions: list[Execution]) -> list[Finding]:
    return [
        Finding("unresolved_timeout", e.agent_id, f"{e.tool_name} timed out and was never confirmed at the target", [e.id])
        for e in executions if e.outcome == "timeout"
    ]


def _clock_skew(session: Session, since: datetime | None) -> list[Finding]:
    query = (
        session.query(AuditEvent, Execution.agent_id)
        .join(Execution, Execution.id == AuditEvent.execution_id)
        .filter(AuditEvent.event_type == "clock_skew")
    )
    if since is not None:
        query = query.filter(Execution.requested_at >= since)
    events = query.all()
    return [
        Finding(
            "clock_skew", agent_id,
            f"broker and gateway clocks disagree by {event.payload.get('skew_ms')} ms; "
            "cross-service ordering of this trace isn't reliable",
            [event.execution_id],
        )
        for event, agent_id in events
    ]


def detect(session: Session, *, since: datetime | None = None) -> list[Finding]:
    """Findings over the whole trace, or only calls requested at or after `since`."""
    query = session.query(Execution).order_by(Execution.requested_at)
    if since is not None:
        query = query.filter(Execution.requested_at >= since)
    executions = query.all()
    by_agent: dict[str, list[Execution]] = defaultdict(list)
    for e in executions:
        by_agent[e.agent_id].append(e)

    return [
        *_denial_bursts(by_agent),
        *_control_denials(by_agent),
        *_refund_without_read(session, by_agent),
        *_unresolved_timeouts(executions),
        *_clock_skew(session, since),
    ]
