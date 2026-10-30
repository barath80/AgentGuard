"""Console read models and operator actions.

Every query is scoped to the tenants the principal may read. Request
payloads (tool arguments) are returned only to principals allowed to see
them; everyone else gets the `safe_request_ref` hash. State changes
(approval decisions, suspend/resume) go through the same services the
gateway and CLI use, and are recorded in `operator_actions`.
"""

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
    Tool,
)
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


def overview(session: Session, principal: Principal, window_hours: int) -> dict[str, Any]:
    if window_hours not in WINDOWS:
        raise ConsoleError(f"window_hours must be one of {sorted(WINDOWS)}", status_code=422)
    bucket = WINDOWS[window_hours]
    now = datetime.now(UTC)
    since = now - timedelta(hours=window_hours)

    executions = [
        e for e in _scoped(session.query(Execution), principal, Execution.tenant_id)
        .filter(Execution.requested_at >= since)
        .order_by(Execution.requested_at)
        .all()
    ]
    names = _agent_names(session)

    # Fixed buckets ending now, so an idle period shows as zeros rather than a gap.
    bucket_count = int(timedelta(hours=window_hours) / bucket)
    start = now - bucket * bucket_count
    series = [
        {"start": _iso(start + bucket * i), "allow": 0, "deny": 0, "approval_required": 0}
        for i in range(bucket_count)
    ]
    for e in executions:
        index = min(int((_utc(e.requested_at) - start) / bucket), bucket_count - 1)
        if index >= 0 and e.decision in series[index]:
            series[index][e.decision] += 1

    terminal = [e for e in executions if e.outcome != "not_attempted"]
    latencies = [ms for e in executions if (ms := _latency_ms(e)) is not None]
    denial_reasons = Counter(_reason_group(e.reason) for e in executions if e.decision == "deny")
    active = session.query(Policy).filter(Policy.is_active.is_(True)).one_or_none()
    pending = _scoped(session.query(Approval), principal, Approval.tenant_id).filter(
        Approval.status == "pending", Approval.expires_at > now
    )

    return {
        "window_hours": window_hours,
        "generated_at": _iso(now),
        "totals": {
            "calls": len(executions),
            "by_decision": dict(Counter(e.decision for e in executions)),
            "by_outcome": dict(Counter(e.outcome for e in executions)),
            "by_tool": dict(Counter(e.tool_name for e in executions)),
        },
        "latency_ms": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95)},
        "trace_completeness": (sum(e.trace_complete for e in terminal) / len(terminal)) if terminal else None,
        "unresolved_timeouts": sum(1 for e in executions if e.outcome == "timeout"),
        "pending_approvals": pending.count(),
        "denials_by_reason": dict(denial_reasons.most_common(8)),
        "series": series,
        "recent": [_execution_summary(e, names) for e in reversed(executions[-8:])],
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


def list_anomalies(read_session: Session, principal: Principal) -> list[dict[str, Any]]:
    agents = {a.id: a for a in read_session.query(Agent).all()}
    findings = []
    for f in anomalies.detect(read_session):
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
        from agentguard.ml.model import ModelUnavailable, agent_risk, load
    except ImportError:
        return {"available": False, "reason": "ML extras not installed (pip install -e '.[ml]')", "agents": []}
    try:
        model = load(Path(settings.anomaly_model_path))
    except ModelUnavailable as exc:
        return {"available": False, "reason": str(exc), "agents": []}
    agents = {a.id: a for a in read_session.query(Agent).all()}
    rows = []
    for row in agent_risk(read_session, model, hours=hours):
        agent = agents.get(row["agent_id"])
        if agent is None or not principal.can_read_tenant(agent.tenant_id):
            continue
        rows.append({**row, "agent_name": agent.name, "tenant_id": agent.tenant_id})
    return {
        "available": True,
        "model": {"trained_at": model.trained_at, "windows": model.windows, "agents": model.agents},
        "agents": rows,
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
