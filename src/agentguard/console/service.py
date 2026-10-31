"""Console read models and operator actions.

Every query is scoped to the tenants the principal may read. Request
payloads (tool arguments) are returned only to principals allowed to see
them; everyone else gets the `safe_request_ref` hash. State changes
(approval decisions, suspend/resume) go through the same services the
gateway and CLI use, and are recorded in `operator_actions`.
"""

import json
import math
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Integer, case, cast, func, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query, Session

from agentguard.approval import service as approval_service
from agentguard.audit import service as audit_service
from agentguard.audit.stats import percentile
from agentguard.config import settings
from agentguard.console.auth import Principal
from agentguard.controls import anomalies
from agentguard.controls.service import CONTROL_PREFIX, set_agent_status, spent_last_24h
from agentguard.models import (
    Agent,
    Approval,
    AuditEvent,
    Execution,
    OperatorAction,
    Policy,
    PolicyRule,
    RequestPayload,
    ShadowPolicy,
    Tool,
)
from agentguard.policy import shadow as shadow_policy
from agentguard.replay.service import load_policy_file
from agentguard.replay.service import replay as replay_traces
from agentguard.replay.service import simulate as simulate_policy

WINDOWS = {1: timedelta(minutes=5), 24: timedelta(hours=1), 168: timedelta(hours=6)}  # hours -> bucket


class ConsoleError(Exception):
    def __init__(self, message: str, *, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    value = _utc(value)
    return value.isoformat() if value else None


def _scoped(query: Query, principal: Principal, column) -> Query:
    return query if principal.reads_all_tenants else query.filter(column.in_(principal.tenants))


def _agent_names(session: Session) -> dict[str, str]:
    return dict(session.query(Agent.id, Agent.name).all())


def _latency_ms(e: Execution) -> float | None:
    if e.decided_at is None or e.requested_at is None:
        return None
    return (_utc(e.decided_at) - _utc(e.requested_at)).total_seconds() * 1000


def _execution_summary(e: Execution, names: dict[str, str]) -> dict[str, Any]:
    return {
        "id": e.id,
        "agent_id": e.agent_id,
        "agent_name": names.get(e.agent_id, e.agent_id),
        "tenant_id": e.tenant_id,
        "tool_name": e.tool_name,
        "environment": e.environment,
        "target_resource": e.target_resource,
        "policy_version": e.policy_version,
        "decision": e.decision,
        "outcome": e.outcome,
        "reason": e.reason,
        "approval_id": e.approval_id,
        "workflow_id": e.workflow_id,
        "requested_at": _iso(e.requested_at),
        "completed_at": _iso(e.completed_at),
        "latency_ms": _latency_ms(e),
        "trace_complete": e.trace_complete,
    }


def whoami(principal: Principal) -> dict[str, Any]:
    return {
        "name": principal.name,
        "roles": sorted(principal.roles),
        "tenants": sorted(principal.tenants),
        "can_operate": principal.can_operate,
        "can_view_payloads": principal.can_view_payloads,
        "reads_all_tenants": principal.reads_all_tenants,
    }


# --- Overview -----------------------------------------------------------------


def _seconds_after(session: Session, column, origin: datetime):
    """SQL expression: seconds from `origin` to `column`, in this database's dialect."""
    if session.get_bind().dialect.name == "postgresql":
        return func.extract("epoch", column - origin)
    # SQLite stores these as naive UTC text; julianday() parses it.
    naive = origin.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ")
    return (func.julianday(column) - func.julianday(naive)) * 86400.0


def _latency_percentiles(session: Session, scoped: Query) -> dict[str, float | None]:
    decided = scoped.filter(Execution.decided_at.is_not(None))
    if session.get_bind().dialect.name == "postgresql":
        latency = func.extract("epoch", Execution.decided_at - Execution.requested_at) * 1000
        p50, p95 = decided.with_entities(
            func.percentile_cont(0.5).within_group(latency),
            func.percentile_cont(0.95).within_group(latency),
        ).one()
        return {"p50": None if p50 is None else float(p50), "p95": None if p95 is None else float(p95)}
    # SQLite has no percentile aggregate: fetch only the two timestamps (dev/test volumes).
    latencies = [
        (_utc(decided_at) - _utc(requested_at)).total_seconds() * 1000
        for requested_at, decided_at in decided.with_entities(Execution.requested_at, Execution.decided_at)
    ]
    return {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95)}


def overview(session: Session, principal: Principal, window_hours: int) -> dict[str, Any]:
    """Headline metrics for a window, aggregated in the database rather than in Python."""
    if window_hours not in WINDOWS:
        raise ConsoleError(f"window_hours must be one of {sorted(WINDOWS)}", status_code=422)
    bucket = WINDOWS[window_hours]
    now = datetime.now(UTC)
    # Fixed buckets ending now, so an idle period shows as zeros rather than a gap.
    bucket_count = int(timedelta(hours=window_hours) / bucket)
    start = now - bucket * bucket_count

    in_window = _scoped(session.query(Execution), principal, Execution.tenant_id).filter(
        Execution.requested_at >= start
    )

    def counts(column) -> dict[str, int]:
        return {key: n for key, n in in_window.with_entities(column, func.count()).group_by(column)}

    by_decision = counts(Execution.decision)
    by_outcome = counts(Execution.outcome)

    series = [
        {"start": _iso(start + bucket * i), "allow": 0, "deny": 0, "approval_required": 0}
        for i in range(bucket_count)
    ]
    bucket_index = cast(_seconds_after(session, Execution.requested_at, start) / bucket.total_seconds(), Integer)
    for index, decision, n in (
        in_window.with_entities(bucket_index, Execution.decision, func.count()).group_by(bucket_index, Execution.decision)
    ):
        index = min(max(int(index), 0), bucket_count - 1)
        if decision in series[index]:
            series[index][decision] += n

    terminal, complete = in_window.filter(Execution.outcome != "not_attempted").with_entities(
        func.count(), func.coalesce(func.sum(case((Execution.trace_complete.is_(True), 1), else_=0)), 0)
    ).one()

    denial_reasons: Counter[str] = Counter()
    for reason, n in (
        in_window.filter(Execution.decision == "deny")
        .with_entities(Execution.reason, func.count()).group_by(Execution.reason)
    ):
        denial_reasons[_reason_group(reason)] += n

    recent = in_window.order_by(Execution.requested_at.desc()).limit(8).all()
    names = _agent_names(session)
    active = session.query(Policy).filter(Policy.is_active.is_(True)).one_or_none()
    pending = _scoped(session.query(Approval), principal, Approval.tenant_id).filter(
        Approval.status == "pending", Approval.expires_at > now
    )

    return {
        "window_hours": window_hours,
        "generated_at": _iso(now),
        "totals": {
            "calls": sum(by_decision.values()),
            "by_decision": by_decision,
            "by_outcome": by_outcome,
            "by_tool": counts(Execution.tool_name),
        },
        "latency_ms": _latency_percentiles(session, in_window),
        "trace_completeness": (complete / terminal) if terminal else None,
        "unresolved_timeouts": by_outcome.get("timeout", 0),
        "pending_approvals": pending.count(),
        "denials_by_reason": dict(denial_reasons.most_common(8)),
        "series": series,
        "recent": [_execution_summary(e, names) for e in recent],
        "posture": {
            "environment": settings.environment,
            "kill_switch": settings.kill_switch,
            "allow_reads_when_policy_unavailable": settings.allow_reads_when_policy_unavailable,
            "active_policy_version": active.version if active else None,
            "breaker_threshold": settings.breaker_threshold,
            "breaker_window_seconds": settings.breaker_window_seconds,
            "approval_expiry_seconds": settings.approval_default_expiry_seconds,
        },
    }


def _reason_group(reason: str | None) -> str:
    """Collapse per-call detail (e.g. budget amounts) so reasons aggregate meaningfully."""
    if not reason:
        return "unspecified"
    if reason.startswith(CONTROL_PREFIX):
        return reason.split(" ", 1)[0]
    return reason.split(":", 1)[0] if reason.startswith("unexpected") else reason


# --- Workflows (THREAT_MODEL #18) ------------------------------------------------

WORKFLOW_STATUSES = ("partial", "in_progress", "incomplete", "completed")


def _step_state(e: Execution, approvals: dict[str, Approval]) -> str:
    """done | waiting | blocked | failed | unconfirmed (timed out, may have happened)."""
    if e.outcome == "success":
        return "done"
    if e.outcome == "timeout":
        return "unconfirmed"
    if e.outcome == "failure":
        return "failed"
    if e.decision == "approval_required":
        approval = approvals.get(e.approval_id or "")
        if approval is not None and approval.status in ("pending", "approved"):
            return "waiting"
    return "blocked"


def _workflow_status(steps: list[dict[str, Any]]) -> str:
    states = {s["state"] for s in steps}
    if states == {"done"}:
        return "completed"
    if "waiting" in states:
        return "in_progress"
    # Something after the first step didn't happen. If a write already took effect (or
    # might have), the task is half-done and someone has to decide what to undo or finish.
    if any(s["state"] == "unconfirmed" or (s["state"] == "done" and s["writes"]) for s in steps):
        return "partial"
    return "incomplete"


def list_workflows(
    session: Session, principal: Principal, *, status: str | None, hours: int, limit: int,
) -> list[dict[str, Any]]:
    """Group each agent's calls by workflow label and report how far each got.

    A workflow is keyed by (agent, workflow_id): the label is the agent's own, so
    one agent can't add steps to another's workflow.
    """
    if status is not None and status not in WORKFLOW_STATUSES:
        raise ConsoleError(f"status must be one of {list(WORKFLOW_STATUSES)}", status_code=422)
    since = datetime.now(UTC) - timedelta(hours=hours)
    in_scope = _scoped(session.query(Execution), principal, Execution.tenant_id).filter(
        Execution.workflow_id.is_not(None), Execution.requested_at >= since
    )
    # Pick the workflows first, in SQL: most are completed one-step tasks that the
    # unfinished views never show, so their steps aren't loaded at all.
    unfinished = func.sum(case((Execution.outcome != "success", 1), else_=0))
    last_step = func.max(Execution.requested_at)
    groups_query = in_scope.with_entities(Execution.agent_id, Execution.workflow_id).group_by(
        Execution.agent_id, Execution.workflow_id
    )
    if status == "completed":
        groups_query = groups_query.having(unfinished == 0).order_by(last_step.desc()).limit(limit)
    elif status is not None:
        groups_query = groups_query.having(unfinished > 0).order_by(last_step.desc()).limit(1000)
    else:
        groups_query = groups_query.order_by(last_step.desc()).limit(limit)
    keys = [tuple(k) for k in groups_query.all()]
    executions = []
    for i in range(0, len(keys), 300):
        executions += in_scope.filter(tuple_(Execution.agent_id, Execution.workflow_id).in_(keys[i:i + 300])).all()
    executions.sort(key=lambda e: _utc(e.requested_at))
    approval_ids = [e.approval_id for e in executions if e.approval_id]
    approvals = {a.id: a for a in session.query(Approval).filter(Approval.id.in_(approval_ids))} if approval_ids else {}
    risk = dict(session.query(Tool.id, Tool.risk_class).all())
    names = _agent_names(session)

    groups: dict[tuple[str, str], list[Execution]] = defaultdict(list)
    for e in executions:
        groups[(e.agent_id, e.workflow_id)].append(e)

    workflows = []
    for (agent_id, workflow_id), group in groups.items():
        steps = [
            {
                "execution_id": e.id,
                "tool_name": e.tool_name,
                "target_resource": e.target_resource,
                "decision": e.decision,
                "outcome": e.outcome,
                "reason": e.reason,
                "state": _step_state(e, approvals),
                "writes": risk.get(e.tool_id, "write-high") != "read",
                "requested_at": _iso(e.requested_at),
            }
            for e in group
        ]
        workflow_status = _workflow_status(steps)
        if status is not None and workflow_status != status:
            continue
        workflows.append({
            "workflow_id": workflow_id,
            "agent_id": agent_id,
            "agent_name": names.get(agent_id, agent_id),
            "tenant_id": group[0].tenant_id,
            "status": workflow_status,
            "started_at": steps[0]["requested_at"],
            "last_step_at": steps[-1]["requested_at"],
            "steps": steps,
            # What actually changed in target systems: the first thing to look at when a task stopped half-way.
            "side_effects": [s for s in steps if s["writes"] and s["state"] in ("done", "unconfirmed")],
        })
    workflows.sort(key=lambda w: w["last_step_at"] or "", reverse=True)
    return workflows[:limit]


# --- Executions -----------------------------------------------------------------


def list_executions(
    session: Session,
    principal: Principal,
    *,
    decision: str | None,
    outcome: str | None,
    tool: str | None,
    agent_id: str | None,
    q: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    query = _scoped(session.query(Execution), principal, Execution.tenant_id)
    if decision:
        query = query.filter(Execution.decision == decision)
    if outcome:
        query = query.filter(Execution.outcome == outcome)
    if tool:
        query = query.filter(Execution.tool_name == tool)
    if agent_id:
        query = query.filter(Execution.agent_id == agent_id)
    if q:
        like = f"%{q}%"
        query = query.filter(
            Execution.id.like(like) | Execution.target_resource.like(like) | Execution.reason.like(like)
        )
    total = query.count()
    rows = query.order_by(Execution.requested_at.desc()).offset(offset).limit(limit).all()
    names = _agent_names(session)
    return {"total": total, "items": [_execution_summary(e, names) for e in rows]}


def _readable_execution(session: Session, principal: Principal, execution_id: str) -> Execution:
    execution = session.get(Execution, execution_id)
    # 404 for other tenants' executions, so ids can't be probed.
    if execution is None or not principal.can_read_tenant(execution.tenant_id):
        raise ConsoleError("execution not found", status_code=404)
    return execution


def get_execution(session: Session, principal: Principal, execution_id: str) -> dict[str, Any]:
    execution = _readable_execution(session, principal, execution_id)
    events = (
        session.query(AuditEvent)
        .filter(AuditEvent.execution_id == execution.id)
        .order_by(AuditEvent.created_at)
        .all()
    )
    matched_rule = None
    for event in events:
        rule_id = (event.payload or {}).get("matched_rule_id") if event.event_type == "decision_made" else None
        if rule_id and (rule := session.get(PolicyRule, rule_id)):
            matched_rule = _rule(rule)

    payload = session.get(RequestPayload, execution.id) if principal.can_view_payloads else None
    approval = session.get(Approval, execution.approval_id) if execution.approval_id else None
    return {
        **_execution_summary(execution, _agent_names(session)),
        "idempotency_key": execution.idempotency_key,
        "decided_at": _iso(execution.decided_at),
        "executed_at": _iso(execution.executed_at),
        "safe_request_ref": execution.safe_request_ref,
        "safe_result_ref": execution.safe_result_ref,
        "args": payload.args if payload else None,
        "payload_visible": principal.can_view_payloads,
        "matched_rule": matched_rule,
        "approval": _approval_record(approval, principal) if approval else None,
        "audit_events": [
            {"id": e.id, "event_type": e.event_type, "payload": e.payload, "created_at": _iso(e.created_at)}
            for e in events
        ],
    }


# --- Approvals ------------------------------------------------------------------


def _approval_record(approval: Approval, principal: Principal) -> dict[str, Any]:
    expired = approval.status == "pending" and _utc(approval.expires_at) <= datetime.now(UTC)
    blocked = None
    if approval.status != "pending":
        blocked = f"already {approval.status}"
    elif expired:
        blocked = "expired"
    elif not principal.can_approve_for(approval.tenant_id, approval.required_role):
        blocked = f"requires role {approval.required_role!r} in {approval.tenant_id}"
    return {
        "id": approval.id,
        "execution_id": approval.execution_id,
        "tenant_id": approval.tenant_id,
        "resource": approval.resource,
        "action_hash": approval.action_hash,
        "policy_version": approval.policy_version,
        "required_role": approval.required_role,
        "status": "expired" if expired else approval.status,
        "approver": approval.approver,
        "created_at": _iso(approval.created_at),
        "expires_at": _iso(approval.expires_at),
        "decided_at": _iso(approval.decided_at),
        "consumed_at": _iso(approval.consumed_at),
        "can_decide": blocked is None,
        "blocked_reason": blocked,
    }


def list_approvals(session: Session, principal: Principal, status: str | None) -> list[dict[str, Any]]:
    query = _scoped(session.query(Approval), principal, Approval.tenant_id)
    if status:
        query = query.filter(Approval.status == status)
    approvals = query.order_by(Approval.created_at.desc()).limit(200).all()
    names = _agent_names(session)
    items = []
    for approval in approvals:
        execution = session.get(Execution, approval.execution_id)
        payload = session.get(RequestPayload, approval.execution_id) if principal.can_view_payloads else None
        items.append({
            **_approval_record(approval, principal),
            "tool_name": execution.tool_name if execution else None,
            "environment": execution.environment if execution else None,
            "agent_name": names.get(execution.agent_id) if execution else None,
            "args": payload.args if payload else None,
        })
    return items


def decide_approval(session: Session, principal: Principal, approval_id: str, *, approve: bool) -> dict[str, Any]:
    approval = session.get(Approval, approval_id)
    if approval is None or not principal.can_read_tenant(approval.tenant_id):
        raise ConsoleError("approval not found", status_code=404)
    if not principal.can_approve_for(approval.tenant_id, approval.required_role):
        raise ConsoleError(
            f"{principal.name} cannot decide approvals requiring role {approval.required_role!r} "
            f"in {approval.tenant_id}",
            status_code=403,
        )
    # Role and tenant are the ones the principal was verified to hold for this approval.
    approver = approval_service.ApproverIdentity(
        name=principal.name,
        role=approval.required_role or min(principal.roles),
        tenant_id=approval.tenant_id,
    )
    try:
        approval = approval_service.decide(session, approval_id, approve=approve, approver=approver)
    except approval_service.ApprovalError as exc:
        raise ConsoleError(str(exc), status_code=409) from exc

    audit_service.record(
        session, approval.execution_id, "approval_decided",
        {"approval_id": approval.id, "status": approval.status, "approver": principal.name},
    )
    _record_action(session, principal, "approval." + approval.status, "approval", approval.id,
                   {"execution_id": approval.execution_id})
    return _approval_record(approval, principal)


# --- Registry -------------------------------------------------------------------


def list_agents(session: Session, principal: Principal) -> list[dict[str, Any]]:
    agents = _scoped(session.query(Agent), principal, Agent.tenant_id).order_by(Agent.name).all()
    day_ago = datetime.now(UTC) - timedelta(hours=24)
    breaker_since = datetime.now(UTC) - timedelta(seconds=settings.breaker_window_seconds)
    items = []
    for agent in agents:
        recent = session.query(Execution).filter(Execution.agent_id == agent.id)
        breaker_denials = recent.filter(
            Execution.decision == "deny",
            Execution.requested_at >= breaker_since,
            (Execution.reason.is_(None)) | (~Execution.reason.startswith(CONTROL_PREFIX)),
        ).count()
        last = recent.order_by(Execution.requested_at.desc()).first()
        items.append({
            "id": agent.id,
            "name": agent.name,
            "owner": agent.owner,
            "tenant_id": agent.tenant_id,
            "workload_identity_ref": agent.workload_identity_ref,
            "status": agent.status,
            "approved_environments": agent.approved_environments or [],
            "risk_category": agent.risk_category,
            "rate_limit_per_minute": agent.rate_limit_per_minute,
            "daily_spend_limit": agent.daily_spend_limit,
            "spent_24h": spent_last_24h(session, agent.id),
            "calls_24h": recent.filter(Execution.requested_at >= day_ago).count(),
            "denials_24h": recent.filter(
                Execution.requested_at >= day_ago, Execution.decision == "deny"
            ).count(),
            "circuit_open": breaker_denials >= settings.breaker_threshold,
            "last_seen_at": _iso(last.requested_at) if last else None,
            "created_at": _iso(agent.created_at),
        })
    return items


ENVIRONMENTS = ("dev", "staging", "prod")
RISK_CATEGORIES = ("low", "medium", "high")
_WORKLOAD_REF = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}@[a-z0-9][a-z0-9.-]{0,62}$")
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def register_agent(session: Session, principal: Principal, spec: dict[str, Any]) -> dict[str, Any]:
    """Add an agent to the registry. Its workload identity is what the identity layer
    verifies; nothing an agent sends later can change these attributes."""
    if not principal.can_operate:
        raise ConsoleError("only operators can register agents", status_code=403)
    ref, tenant = spec["workload_identity_ref"], spec["tenant_id"]
    if not _WORKLOAD_REF.match(ref):
        raise ConsoleError("workload identity must look like name@pool (lowercase)", status_code=422)
    if not _TENANT.match(tenant):
        raise ConsoleError("tenant id must be lowercase letters, digits and dashes", status_code=422)
    if not principal.can_read_tenant(tenant):
        raise ConsoleError("you can't register agents for that tenant", status_code=403)
    environments = sorted(set(spec["approved_environments"]), key=ENVIRONMENTS.index)
    agent = Agent(
        name=spec.get("name") or ref.split("@")[0],
        owner=spec["owner"],
        tenant_id=tenant,
        workload_identity_ref=ref,
        status="active",
        approved_environments=environments,
        risk_category=spec["risk_category"],
        rate_limit_per_minute=spec["rate_limit_per_minute"],
        daily_spend_limit=spec.get("daily_spend_limit"),
    )
    session.add(agent)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise ConsoleError("an agent with that workload identity is already registered", status_code=409) from exc
    _record_action(session, principal, "agent.registered", "agent", agent.id, {
        "workload_identity_ref": ref, "tenant_id": tenant, "approved_environments": environments,
        "risk_category": agent.risk_category, "rate_limit_per_minute": agent.rate_limit_per_minute,
        "daily_spend_limit": agent.daily_spend_limit,
    })
    return {"id": agent.id, "name": agent.name, "workload_identity_ref": ref, "tenant_id": tenant}


def set_agent_suspended(session: Session, principal: Principal, agent_id: str, *, suspended: bool) -> dict[str, Any]:
    if not principal.can_operate:
        raise ConsoleError("only operators can suspend or resume agents", status_code=403)
    agent = session.get(Agent, agent_id)
    if agent is None or not principal.can_read_tenant(agent.tenant_id):
        raise ConsoleError("agent not found", status_code=404)
    status = "suspended" if suspended else "active"
    previous = agent.status
    set_agent_status(session, agent.workload_identity_ref, status)
    _record_action(session, principal, f"agent.{status}", "agent", agent.id, {"previous_status": previous})
    return {"id": agent.id, "status": agent.status}


def list_tools(session: Session, principal: Principal) -> list[dict[str, Any]]:
    calls = _scoped(session.query(Execution.tool_name), principal, Execution.tenant_id)
    counts = Counter(name for (name,) in calls.all())
    return [
        {
            "id": t.id,
            "name": t.name,
            "schema_version": t.schema_version,
            "input_schema": t.input_schema,
            "risk_class": t.risk_class,
            "target_scope": t.target_scope,
            "executor_ref": t.executor_ref,
            "spend_arg": t.spend_arg,
            "calls_total": counts.get(t.name, 0),
        }
        for t in session.query(Tool).order_by(Tool.name).all()
    ]


# --- Policies, simulation, replay --------------------------------------------------


def _rule(rule: PolicyRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "priority": rule.priority,
        "decision": rule.decision,
        "approval_role": rule.approval_role,
        "match": rule.match or {},
    }


def list_policies(session: Session) -> dict[str, Any]:
    policies = session.query(Policy).order_by(Policy.version.desc()).all()
    proposed = []
    directory = Path(settings.proposed_policies_dir)
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            data = json.loads(path.read_text())
            load_policy_file(path)  # only list files that would actually parse
        except (ValueError, KeyError, TypeError):
            continue
        proposed.append({"name": path.stem, "description": data.get("description"), "policy": data})
    return {
        "versions": [
            {
                "id": p.id,
                "version": p.version,
                "scope": p.scope,
                "is_active": p.is_active,
                "published_at": _iso(p.published_at),
                "rules": [_rule(r) for r in sorted(p.rules, key=lambda r: r.priority)],
            }
            for p in policies
        ],
        "proposed": proposed,
    }


def _require_global_reader(principal: Principal) -> None:
    # Policy is global, so its simulation and replay cover every tenant's traces.
    if not principal.reads_all_tenants:
        raise ConsoleError("simulation and replay need read access to all tenants", status_code=403)


def run_simulation(read_session: Session, principal: Principal, version: int, rules: list) -> dict[str, Any]:
    _require_global_reader(principal)
    try:
        report = simulate_policy(read_session, rules, version)
    except ValueError as exc:  # e.g. an unknown arg operator in a proposed rule
        raise ConsoleError(f"proposed policy is invalid: {exc}", status_code=422) from exc

    executions = {
        e.id: e for e in read_session.query(Execution).filter(
            Execution.id.in_([c.execution_id for c in report.changes])
        )
    }
    names = _agent_names(read_session)
    loosening = {c.execution_id for c in report.newly_allowed}

    def change(c) -> dict[str, Any]:
        e = executions.get(c.execution_id)
        return {
            "execution_id": c.execution_id,
            "tool_name": c.tool_name,
            "recorded": c.recorded,
            "simulated": c.simulated,
            "impact": "security_loosening" if c.execution_id in loosening else (
                "task_success_risk" if c.recorded == "allow" else "other"
            ),
            "agent_name": names.get(e.agent_id) if e else None,
            "environment": e.environment if e else None,
            "target_resource": e.target_resource if e else None,
            "requested_at": _iso(e.requested_at) if e else None,
        }

    return {
        "proposed_version": report.proposed_version,
        "evaluated": report.evaluated,
        "skipped_pre_policy": report.skipped_pre_policy,
        "transitions": report.transitions,
        "newly_allowed": len(report.newly_allowed),
        "newly_restricted": len(report.newly_restricted),
        "unattended_rate_before": report.unattended_rate_before,
        "unattended_rate_after": report.unattended_rate_after,
        "verdict": "loosens_security" if report.newly_allowed else "safe_to_review",
        "changes": [change(c) for c in report.changes],
    }


def publish_policy(
    session: Session, principal: Principal, rules: list, *, description: str | None,
    acknowledge_loosening: bool, reason: str | None,
) -> dict[str, Any]:
    """Publish rules as the next policy version and make it active.

    The proposal is simulated against every recorded call first, server-side (a
    client-supplied simulation result is never trusted). If it would allow anything
    the current record shows was denied or gated, publishing needs an explicit
    acknowledgement and a reason, both kept in the operator log. Pending approvals
    issued under the old version stop being executable, as with any policy change.
    """
    if not principal.can_operate:
        raise ConsoleError("only operators can publish policy", status_code=403)
    _require_global_reader(principal)
    if not rules:
        raise ConsoleError("a policy needs at least one rule", status_code=422)
    for rule in rules:
        if rule.decision == "approval_required" and not rule.approval_role:
            raise ConsoleError(f"rule {rule.id}: approval_required needs an approval_role", status_code=422)

    current = session.query(func.max(Policy.version)).scalar() or 0
    version = current + 1
    report = simulate_policy(session, rules, version)
    loosening = [c.execution_id for c in report.newly_allowed]
    if loosening and not acknowledge_loosening:
        raise ConsoleError(
            f"this policy would newly allow {len(loosening)} recorded call(s) that were denied or gated; "
            "review the simulation and acknowledge the loosening with a reason to publish",
            status_code=409,
        )
    if loosening and (not reason or len(reason.strip()) < 10):
        raise ConsoleError("explain why the loosening is intended (at least 10 characters)", status_code=422)

    session.query(Policy).filter(Policy.is_active.is_(True)).update({Policy.is_active: False})
    policy = Policy(version=version, scope="global", is_active=True)
    session.add(policy)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise ConsoleError("another policy version was published at the same time; try again", status_code=409) from exc
    for rule in rules:
        session.add(PolicyRule(
            policy_id=policy.id, match=rule.match, decision=rule.decision,
            approval_role=rule.approval_role, priority=rule.priority,
        ))
    _record_action(session, principal, "policy.published", "policy", policy.id, {
        "version": version,
        "previous_version": current,
        "description": description,
        "rules": len(rules),
        "evaluated_calls": report.evaluated,
        "newly_allowed": len(loosening),
        "newly_restricted": len(report.newly_restricted),
        "loosening_acknowledged": bool(loosening),
        "reason": reason,
    })
    return {
        "id": policy.id, "version": version, "previous_version": current,
        "newly_allowed": len(loosening), "newly_restricted": len(report.newly_restricted),
    }


# --- Policy shadow mode -------------------------------------------------------------

DECISION_STRICTNESS = {"allow": 0, "approval_required": 1, "deny": 2}


def _shadow_record(row: ShadowPolicy) -> dict[str, Any]:
    return {
        "id": row.id, "description": row.description, "rules": row.rules,
        "started_by": row.started_by, "started_at": _iso(row.started_at),
        "stopped_by": row.stopped_by, "stopped_at": _iso(row.stopped_at),
    }


def start_shadow(session: Session, principal: Principal, rules: list, *, description: str | None) -> dict[str, Any]:
    """Start running `rules` silently next to the live policy. One shadow at a time: starting
    a new one stops the current one."""
    if not principal.can_operate:
        raise ConsoleError("only operators can start a shadow policy", status_code=403)
    _require_global_reader(principal)
    now = datetime.now(UTC)
    for current in session.query(ShadowPolicy).filter(ShadowPolicy.stopped_at.is_(None)):
        current.stopped_at, current.stopped_by = now, principal.name
    row = ShadowPolicy(description=description, rules=shadow_policy.rules_to_json(rules), started_by=principal.name,
                       started_at=now)
    session.add(row)
    session.flush()
    _record_action(session, principal, "shadow.started", "shadow_policy", row.id,
                   {"description": description, "rules": len(rules)})
    shadow_policy.invalidate()
    return _shadow_record(row)


def stop_shadow(session: Session, principal: Principal) -> dict[str, Any]:
    if not principal.can_operate:
        raise ConsoleError("only operators can stop a shadow policy", status_code=403)
    row = session.query(ShadowPolicy).filter(ShadowPolicy.stopped_at.is_(None)).one_or_none()
    if row is None:
        raise ConsoleError("no shadow policy is running", status_code=404)
    row.stopped_at, row.stopped_by = datetime.now(UTC), principal.name
    _record_action(session, principal, "shadow.stopped", "shadow_policy", row.id, {})
    shadow_policy.invalidate()
    return _shadow_record(row)


def shadow_report(session: Session, principal: Principal, *, limit: int = 25) -> dict[str, Any]:
    """How the running (or most recent) shadow policy compares with the live one, call by call."""
    _require_global_reader(principal)
    row = session.query(ShadowPolicy).order_by(ShadowPolicy.started_at.desc()).first()
    if row is None:
        return {"shadow": None}
    compared = session.query(Execution).filter(Execution.shadow_policy_id == row.id)
    pairs = {
        (live, shadow): n for live, shadow, n in compared.with_entities(
            Execution.decision, Execution.shadow_decision, func.count()
        ).group_by(Execution.decision, Execution.shadow_decision)
    }
    total = sum(pairs.values())
    agree = sum(n for (live, shadow), n in pairs.items() if live == shadow)
    # Looser: the shadow would let through (or ask less of) a call the live policy held back.
    looser = sum(n for (live, shadow), n in pairs.items()
                 if DECISION_STRICTNESS.get(shadow, 2) < DECISION_STRICTNESS.get(live, 2))
    stricter = total - agree - looser
    differing = (
        compared.filter(Execution.decision != Execution.shadow_decision)
        .order_by(Execution.requested_at.desc()).limit(limit).all()
    )
    names = _agent_names(session)
    return {
        "shadow": _shadow_record(row),
        "running": row.stopped_at is None,
        "compared": total,
        "agreement": (agree / total) if total else None,
        "looser": looser,
        "stricter": stricter,
        "matrix": [{"live": live, "shadow": shadow, "calls": n} for (live, shadow), n in sorted(pairs.items())],
        "differences": [
            {**_execution_summary(e, names), "shadow_decision": e.shadow_decision,
             "direction": "looser" if DECISION_STRICTNESS.get(e.shadow_decision, 2)
             < DECISION_STRICTNESS.get(e.decision, 2) else "stricter"}
            for e in differing
        ],
    }


def run_replay(read_session: Session, principal: Principal) -> dict[str, Any]:
    _require_global_reader(principal)
    report = replay_traces(read_session)
    by_version = defaultdict(lambda: {"replayed": 0, "reproduced": 0})
    for r in report.results:
        version = read_session.get(Execution, r.execution_id).policy_version
        by_version[version]["replayed"] += 1
        by_version[version]["reproduced"] += r.reproduced
    return {
        "replayed": len(report.results),
        "reproduced": len(report.results) - len(report.mismatches),
        "skipped_pre_policy": report.skipped_pre_policy,
        "by_policy_version": {str(k): v for k, v in sorted(by_version.items())},
        "mismatches": [
            {
                "execution_id": r.execution_id,
                "tool_name": r.tool_name,
                "recorded": r.recorded_decision,
                "replayed": r.replayed_decision,
            }
            for r in report.mismatches
        ],
    }


# --- Anomalies & operator log -------------------------------------------------------


_anomaly_cache: dict[tuple[str, int], tuple[float, list]] = {}
_anomaly_lock = threading.Lock()


def _detect_cached(read_session: Session, days: int) -> list:
    """The anomaly report scans the whole recent trace, so it's computed at most once per
    CONSOLE_CACHE_SECONDS and shared by every viewer; each viewer still gets only their tenants."""
    key = (str(read_session.get_bind().url), days)
    ttl = settings.console_cache_seconds
    with _anomaly_lock:
        hit = _anomaly_cache.get(key)
        if ttl > 0 and hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
    findings = anomalies.detect(read_session, since=datetime.now(UTC) - timedelta(days=days))
    with _anomaly_lock:
        _anomaly_cache[key] = (time.monotonic(), findings)
    return findings


def list_anomalies(read_session: Session, principal: Principal, *, days: int = 7) -> list[dict[str, Any]]:
    agents = {a.id: a for a in read_session.query(Agent).all()}
    findings = []
    for f in _detect_cached(read_session, days):
        agent = agents.get(f.agent_id)
        if agent is None or not principal.can_read_tenant(agent.tenant_id):
            continue
        findings.append({
            "kind": f.kind,
            "agent_id": f.agent_id,
            "agent_name": agent.name,
            "tenant_id": agent.tenant_id,
            "detail": f.detail,
            "execution_ids": f.execution_ids,
        })
    return findings


def agent_risk(read_session: Session, principal: Principal, hours: int) -> dict[str, Any]:
    """Advisory behavioural risk per agent from the anomaly model, if one is trained."""
    try:
        from agentguard.ml.model import (
            ModelUnavailable,
            agent_risk_from_windows,
            drift_report,
            load,
            recent_windows,
        )
    except ImportError:
        return {"available": False, "reason": "ML extras not installed (pip install -e '.[ml]')", "agents": []}
    try:
        model = load(Path(settings.anomaly_model_path))
    except ModelUnavailable as exc:
        return {"available": False, "reason": str(exc), "agents": []}
    agents = {a.id: a for a in read_session.query(Agent).all()}
    rows = []
    recent = recent_windows(read_session, model, hours=hours)
    drift = drift_report(model, recent)
    for row in agent_risk_from_windows(model, recent):
        agent = agents.get(row["agent_id"])
        if agent is None or not principal.can_read_tenant(agent.tenant_id):
            continue
        rows.append({**row, "agent_name": agent.name, "tenant_id": agent.tenant_id})
    return {
        "available": True,
        "model": {
            "trained_at": model.trained_at, "windows": model.windows, "agents": model.agents,
            "detector": model.detector_name, "window_minutes": model.window_seconds / 60,
            # Held-out evaluation from the training notebook; absent for models trained without labels.
            "evaluation": {
                k: v for k, v in (model.metrics or {}).items()
                if k.startswith("test_") and not (isinstance(v, float) and not math.isfinite(v))  # NaN isn't JSON
            } or None,
        },
        "agents": rows,
        # Drift describes the whole fleet's traffic; feature-level detail (fleet-wide averages)
        # goes only to all-tenant readers, others see just whether the model is still current.
        "drift": drift if principal.reads_all_tenants else {"status": drift["status"]},
    }


def _record_action(
    session: Session, principal: Principal, action: str, target_type: str, target_id: str, detail: dict
) -> None:
    session.add(OperatorAction(
        actor=principal.name, action=action, target_type=target_type, target_id=target_id, detail=detail
    ))
    session.commit()


def list_operator_actions(session: Session, principal: Principal, limit: int) -> list[dict[str, Any]]:
    if not principal.reads_all_tenants:
        # Actions aren't tenant-tagged, so a tenant-scoped principal only sees their own.
        query = session.query(OperatorAction).filter(OperatorAction.actor == principal.name)
    else:
        query = session.query(OperatorAction)
    return [
        {
            "id": a.id,
            "actor": a.actor,
            "action": a.action,
            "target_type": a.target_type,
            "target_id": a.target_id,
            "detail": a.detail,
            "created_at": _iso(a.created_at),
        }
        for a in query.order_by(OperatorAction.created_at.desc()).limit(limit).all()
    ]
