"""Gateway orchestration: intake -> identity -> controls -> policy -> (approval) -> broker -> audit.

This module is the only place that ties the other layers together end
to end; routes in gateway/app.py are thin wrappers around it.
"""

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from agentguard.approval import service as approval_service
from agentguard.audit import service as audit_service
from agentguard.broker import service as broker_service
from agentguard.config import settings
from agentguard.controls import service as controls
from agentguard.gateway.schemas import ToolCallRequest, ToolCallResponse
from agentguard.gateway.telemetry import tracer
from agentguard.gateway.validation import unknown_arg_rejection
from agentguard.identity.service import ResolvedIdentity
from agentguard.models import Agent, Approval, Execution, RequestPayload, Tool
from agentguard.policy.engine import PolicyContext, PolicyDecision, evaluate

log = logging.getLogger(__name__)

PRE_POLICY_VERSION = -1  # decided before (or without) a policy evaluation


class GatewayError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _ref(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def submit_tool_call(session: Session, request: ToolCallRequest, identity: ResolvedIdentity) -> ToolCallResponse:
    requested_at = _now()
    with tracer.start_as_current_span("agentguard.tool_call") as span:
        span.set_attributes({
            "agentguard.agent_id": identity.agent_id,
            "agentguard.tenant_id": identity.tenant_id,
            "agentguard.tool": request.tool_name,
            "agentguard.environment": request.environment,
        })
        response = _submit(session, request, identity, requested_at)
        span.set_attributes({
            "agentguard.execution_id": response.execution_id,
            "agentguard.decision": response.decision,
            "agentguard.outcome": response.outcome,
            "agentguard.policy_version": response.policy_version,
        })
        return response


def _submit(
    session: Session, request: ToolCallRequest, identity: ResolvedIdentity, requested_at: datetime
) -> ToolCallResponse:
    tool = session.query(Tool).filter(Tool.name == request.tool_name).one_or_none()
    if tool is None:
        raise GatewayError(f"unknown tool: {request.tool_name}", status_code=404)

    existing = (
        session.query(Execution)
        .filter(Execution.agent_id == identity.agent_id, Execution.idempotency_key == request.idempotency_key)
        .one_or_none()
    )
    if existing is not None:
        if existing.outcome == "timeout":
            # A retry after a timeout: find out what happened, never re-execute.
            result = _reconcile(session, existing, session.get(Tool, existing.tool_id))
            return _to_response(existing, result=result)
        return _to_response(existing, reason="duplicate idempotency key — not re-executed")

    def deny(reason: str) -> ToolCallResponse:
        execution = _record_decision(
            session, request, identity, tool, requested_at,
            decision="deny", policy_version=PRE_POLICY_VERSION, reason=reason,
        )
        return _finish_denied(session, execution)

    schema_reason = unknown_arg_rejection(tool.risk_class, tool.input_schema, request.args)
    if schema_reason:
        return deny(schema_reason)
    if request.environment not in identity.approved_environments:
        return deny("environment not approved for this agent")
    if request.resource_tenant_id != identity.tenant_id:
        return deny("tenant mismatch: resource belongs to a different tenant")
    control_reason = controls.pre_policy_denial(session, identity.agent_id, tool, request.args)
    if control_reason:
        return deny(control_reason)

    ctx = PolicyContext(
        tool_name=tool.name,
        risk_class=tool.risk_class,
        environment=request.environment,
        tenant_id=identity.tenant_id,
        agent_risk_category=identity.risk_category,
        args=request.args,
    )
    try:
        with tracer.start_as_current_span("agentguard.policy.evaluate"):
            policy_decision = evaluate(session, ctx)
    except Exception:
        log.exception("policy evaluation failed")
        session.rollback()
        if tool.risk_class == "read" and settings.allow_reads_when_policy_unavailable:
            policy_decision = PolicyDecision("allow", PRE_POLICY_VERSION, None)
            fallback_reason = "policy unavailable: read-only fallback"
        else:
            return deny("policy unavailable: failing closed")
    else:
        fallback_reason = None

    execution = _record_decision(
        session, request, identity, tool, requested_at,
        decision=policy_decision.decision,
        policy_version=policy_decision.policy_version,
        reason=fallback_reason,
        matched_rule_id=policy_decision.matched_rule_id,
    )

    if policy_decision.decision == "deny":
        execution.reason = "denied by policy"
        return _finish_denied(session, execution)

    if policy_decision.decision == "approval_required":
        approval = approval_service.create_pending_approval(
            session,
            execution_id=execution.id,
            tenant_id=identity.tenant_id,
            action_hash=approval_service.compute_action_hash(
                tool.name, request.target_resource, request.args, policy_decision.policy_version
            ),
            resource=request.target_resource,
            policy_version=policy_decision.policy_version,
            required_role=policy_decision.required_role,
        )
        execution.approval_id = approval.id
        session.commit()
        audit_service.record(session, execution.id, "approval_requested", {"approval_id": approval.id})
        return _to_response(execution, reason="approval required before execution")

    result = _execute_and_record(session, execution, tool, request.args)
    return _to_response(execution, result=result)


def _record_decision(
    session: Session,
    request: ToolCallRequest,
    identity: ResolvedIdentity,
    tool: Tool,
    requested_at: datetime,
    *,
    decision: str,
    policy_version: int,
    reason: str | None,
    matched_rule_id: str | None = None,
) -> Execution:
    execution = Execution(
        agent_id=identity.agent_id,
        tenant_id=identity.tenant_id,
        tool_id=tool.id,
        tool_name=tool.name,
        environment=request.environment,
        target_resource=request.target_resource,
        policy_version=policy_version,
        decision=decision,
        reason=reason,
        idempotency_key=request.idempotency_key,
        workflow_id=request.workflow_id,
        requested_at=requested_at,
        decided_at=_now(),
        safe_request_ref=_ref(request.args),
        agent_risk_category=identity.risk_category,
        tool_risk_class=tool.risk_class,
    )
    session.add(execution)
    session.flush()  # assigns execution.id so RequestPayload can reference it
    session.add(RequestPayload(execution_id=execution.id, args=request.args))
    session.commit()
    audit_service.record(
        session, execution.id, "decision_made",
        {"decision": decision, "policy_version": policy_version, "matched_rule_id": matched_rule_id, "reason": reason},
    )
    return execution


def _finish_denied(session: Session, execution: Execution) -> ToolCallResponse:
    execution.outcome = "denied"
    execution.completed_at = _now()
    execution.trace_complete = True
    session.commit()
    return _to_response(execution)


def execute_approved(session: Session, execution_id: str, identity: ResolvedIdentity) -> ToolCallResponse:
    execution = session.get(Execution, execution_id)
    if execution is None:
        raise GatewayError("execution not found", status_code=404)
    if execution.agent_id != identity.agent_id:
        raise GatewayError("execution does not belong to this agent", status_code=403)
    if execution.decision != "approval_required":
        raise GatewayError(f"execution does not require approval (decision={execution.decision})")
    if execution.approval_id is None:
        raise GatewayError("execution has no associated approval")
    if settings.kill_switch:
        raise GatewayError("global kill switch is engaged", status_code=503)

    approval = session.get(Approval, execution.approval_id)
    if approval is None:
        raise GatewayError("approval not found", status_code=404)

    tool = session.get(Tool, execution.tool_id)
    payload = session.get(RequestPayload, execution.id)
    args = payload.args if payload is not None else {}

    try:
        current_policy = evaluate(
            session,
            PolicyContext(
                tool_name=tool.name,
                risk_class=tool.risk_class,
                environment=execution.environment,
                tenant_id=identity.tenant_id,
                agent_risk_category=identity.risk_category,
                args=args,
            ),
        )
    except Exception as exc:
        log.exception("policy evaluation failed during approved execution")
        session.rollback()
        raise GatewayError("policy unavailable: not executing", status_code=503) from exc

    try:
        approval_service.validate_for_execution(
            session,
            approval,
            action_hash=approval_service.compute_action_hash(
                tool.name, execution.target_resource, args, execution.policy_version
            ),
            current_policy_version=current_policy.policy_version,
        )
    except approval_service.ApprovalError as exc:
        raise GatewayError(str(exc), status_code=409) from exc

    # Budget is re-checked here, not only at request time: otherwise an agent could
    # queue many approvals that each fit the budget alone but not together.
    budget_reason = controls.budget_denial(session, session.get(Agent, identity.agent_id), tool, args)
    if budget_reason:
        raise GatewayError(budget_reason, status_code=409)

    if not approval_service.consume(session, approval.id):
        raise GatewayError("approval already consumed", status_code=409)

    audit_service.record(session, execution.id, "approval_consumed", {"approval_id": approval.id})
    result = _execute_and_record(session, execution, tool, args)
    return _to_response(execution, result=result)


def _execute_and_record(
    session: Session, execution: Execution, tool: Tool, args: dict[str, Any]
) -> dict[str, Any] | None:
    execution.executed_at = _now()
    session.commit()
    broker_clock: list[datetime] = []
    try:
        with tracer.start_as_current_span("agentguard.broker.execute"):
            # The execution id, not the agent's key, is what the target system dedupes on:
            # agent keys are only unique per agent, so two agents' keys could collide downstream.
            result = broker_service.execute(
                tool.executor_ref, args,
                idempotency_key=execution.id, tenant_id=execution.tenant_id,
                timeout_seconds=settings.broker_timeout_seconds,
                on_clock=lambda started, finished: broker_clock.extend((started, finished)),
            )
    except broker_service.BrokerTimeoutError:
        audit_service.record(session, execution.id, "execution_timeout", {})
        return _reconcile(session, execution, tool)
    except Exception as exc:  # noqa: BLE001 - broker boundary, must not raise raw
        execution.outcome = "failure"
        execution.reason = f"execution failed: {exc}"
        execution.completed_at = _now()
        execution.trace_complete = True
        session.commit()
        audit_service.record(session, execution.id, "execution_failed", {"error": str(exc)})
        return None

    if broker_clock:
        _check_clocks(session, execution, *broker_clock)
    return _finish_success(session, execution, result)


def _check_clocks(session: Session, execution: Execution, broker_started: datetime, broker_finished: datetime) -> None:
    """Cross-check the broker's clock against the gateway's (THREAT_MODEL #17).

    The broker's run must fall inside the gateway's request to it. If it doesn't
    by more than the tolerance, one clock is off and the trace's timeline can't
    be trusted to order events across services; that's recorded, not hidden.
    """
    sent, received = _as_utc(execution.executed_at), _now()
    tolerance = timedelta(seconds=settings.clock_skew_tolerance_seconds)
    ahead = broker_started - sent  # negative: broker started "before" the gateway sent
    behind = broker_finished - received  # positive: broker finished "after" the gateway got the answer
    skew = ahead if ahead < -tolerance else behind if behind > tolerance else None
    if skew is not None:
        audit_service.record(session, execution.id, "clock_skew", {
            "broker_started_at": broker_started.isoformat(),
            "broker_finished_at": broker_finished.isoformat(),
            "skew_ms": round(skew.total_seconds() * 1000),
            "tolerance_ms": round(tolerance.total_seconds() * 1000),
        })


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _finish_success(session: Session, execution: Execution, result: dict[str, Any]) -> dict[str, Any]:
    execution.outcome = "success"
    execution.completed_at = _now()
    execution.trace_complete = True
    execution.safe_result_ref = _ref(result)
    session.commit()
    audit_service.record(session, execution.id, "execution_completed", {"result_ref": execution.safe_result_ref})
    return result


def _reconcile(session: Session, execution: Execution, tool: Tool) -> dict[str, Any] | None:
    """Resolve a timed-out execution from the target system's own record (THREAT_MODEL #5)."""
    reconciliation = broker_service.reconcile(tool.executor_ref, execution.id)
    audit_service.record(session, execution.id, "reconciliation", {"status": reconciliation.status})

    if reconciliation.status == "applied":
        execution.reason = "timed out, then confirmed applied at the target"
        return _finish_success(session, execution, reconciliation.result)

    if reconciliation.status == "no_side_effect":
        execution.outcome = "failure"
        execution.reason = "timed out; read-only call, no side effect"
        execution.completed_at = _now()
        execution.trace_complete = True
    else:
        execution.outcome = "timeout"
        execution.reason = "timed out; not yet observed at the target — retry with the same idempotency key to re-check"
        execution.trace_complete = False
    session.commit()
    return None


def _to_response(
    execution: Execution, *, result: dict[str, Any] | None = None, reason: str | None = None
) -> ToolCallResponse:
    return ToolCallResponse(
        execution_id=execution.id,
        decision=execution.decision,
        outcome=execution.outcome,
        policy_version=execution.policy_version,
        approval_id=execution.approval_id,
        result=result,
        reason=reason or execution.reason,
    )
