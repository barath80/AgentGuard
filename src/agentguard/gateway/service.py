"""Gateway orchestration: intake -> identity -> policy -> (approval) -> broker -> audit.

This module is the only place that ties the other layers together end
to end; routes in gateway/app.py are thin wrappers around it.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from agentguard.approval import service as approval_service
from agentguard.audit import service as audit_service
from agentguard.broker import service as broker_service
from agentguard.gateway.schemas import ToolCallRequest, ToolCallResponse
from agentguard.identity.service import ResolvedIdentity
from agentguard.models import Approval, Execution, RequestPayload, Tool
from agentguard.policy.engine import PolicyContext, evaluate


class GatewayError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _ref(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def submit_tool_call(
    session: Session, request: ToolCallRequest, identity: ResolvedIdentity
) -> ToolCallResponse:
    tool = session.query(Tool).filter(Tool.name == request.tool_name).one_or_none()
    if tool is None:
        raise GatewayError(f"unknown tool: {request.tool_name}", status_code=404)

    existing = (
        session.query(Execution)
        .filter(
            Execution.agent_id == identity.agent_id,
            Execution.idempotency_key == request.idempotency_key,
        )
        .one_or_none()
    )
    if existing is not None:
        return _to_response(existing, result=None, reason="duplicate idempotency key — not re-executed")

    if request.environment not in identity.approved_environments:
        return _deny_without_policy(
            session, request, identity, tool, reason="environment not approved for this agent"
        )

    if request.resource_tenant_id != identity.tenant_id:
        return _deny_without_policy(
            session, request, identity, tool, reason="tenant mismatch: resource belongs to a different tenant"
        )

    ctx = PolicyContext(
        tool_name=tool.name,
        risk_class=tool.risk_class,
        environment=request.environment,
        tenant_id=identity.tenant_id,
        agent_risk_category=identity.risk_category,
        args=request.args,
    )
    policy_decision = evaluate(session, ctx)

    execution = Execution(
        agent_id=identity.agent_id,
        tenant_id=identity.tenant_id,
        tool_id=tool.id,
        tool_name=tool.name,
        environment=request.environment,
        target_resource=request.target_resource,
        policy_version=policy_decision.policy_version,
        decision=policy_decision.decision,
        idempotency_key=request.idempotency_key,
        decided_at=_now(),
        safe_request_ref=_ref(request.args),
    )
    session.add(execution)
    session.flush()  # assigns execution.id so RequestPayload can reference it
    session.add(RequestPayload(execution_id=execution.id, args=request.args))
    session.commit()
    audit_service.record(
        session,
        execution.id,
        "decision_made",
        {"decision": policy_decision.decision, "policy_version": policy_decision.policy_version,
         "matched_rule_id": policy_decision.matched_rule_id},
    )

    if policy_decision.decision == "deny":
        execution.outcome = "denied"
        execution.completed_at = _now()
        execution.trace_complete = True
        session.commit()
        return _to_response(execution, result=None, reason="denied by policy")

    if policy_decision.decision == "approval_required":
        action_hash = approval_service.compute_action_hash(
            tool.name, request.target_resource, request.args, policy_decision.policy_version
        )
        approval = approval_service.create_pending_approval(
            session,
            execution_id=execution.id,
            tenant_id=identity.tenant_id,
            action_hash=action_hash,
            resource=request.target_resource,
            policy_version=policy_decision.policy_version,
            required_role=policy_decision.required_role,
        )
        execution.approval_id = approval.id
        session.commit()
        audit_service.record(session, execution.id, "approval_requested", {"approval_id": approval.id})
        return _to_response(execution, result=None, reason="approval required before execution")

    # allow
    result = _execute_and_record(session, execution, tool, request.args)
    return _to_response(execution, result=result, reason=None)


def _deny_without_policy(
    session: Session, request: ToolCallRequest, identity: ResolvedIdentity, tool: Tool, *, reason: str
) -> ToolCallResponse:
    """Identity-scope violations (unapproved environment, tenant mismatch) are denied
    before policy evaluation — they're about the caller's own registered scope, not
    a policy rule outcome, but they still fail closed and still produce a full trace.
    """
    execution = Execution(
        agent_id=identity.agent_id,
        tenant_id=identity.tenant_id,
        tool_id=tool.id,
        tool_name=tool.name,
        environment=request.environment,
        target_resource=request.target_resource,
        policy_version=-1,
        decision="deny",
        idempotency_key=request.idempotency_key,
        decided_at=_now(),
        completed_at=_now(),
        outcome="denied",
        trace_complete=True,
        safe_request_ref=_ref(request.args),
    )
    session.add(execution)
    session.commit()
    audit_service.record(session, execution.id, "decision_made", {"decision": "deny", "reason": reason})
    return _to_response(execution, result=None, reason=reason)


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

    approval = session.get(Approval, execution.approval_id)
    if approval is None:
        raise GatewayError("approval not found", status_code=404)

    tool = session.get(Tool, execution.tool_id)
    payload = session.get(RequestPayload, execution.id)
    args = payload.args if payload is not None else {}

    action_hash = approval_service.compute_action_hash(
        tool.name, execution.target_resource, args, execution.policy_version
    )
    current_policy_decision = evaluate(
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
    try:
        approval_service.validate_for_execution(
            session,
            approval,
            action_hash=action_hash,
            current_policy_version=current_policy_decision.policy_version,
        )
    except approval_service.ApprovalError as exc:
        raise GatewayError(str(exc), status_code=409) from exc

    consumed = approval_service.consume(session, approval.id)
    if not consumed:
        raise GatewayError("approval already consumed", status_code=409)

    audit_service.record(session, execution.id, "approval_consumed", {"approval_id": approval.id})
    result = _execute_and_record(session, execution, tool, args)
    return _to_response(execution, result=result, reason=None)


def _execute_and_record(
    session: Session, execution: Execution, tool: Tool, args: dict[str, Any]
) -> dict[str, Any] | None:
    execution.executed_at = _now()
    session.commit()
    try:
        result = broker_service.execute(tool.executor_ref, args)
    except broker_service.BrokerTimeoutError:
        execution.outcome = "timeout"
        execution.trace_complete = False
        session.commit()
        audit_service.record(session, execution.id, "execution_timeout", {})
        return None
    except Exception as exc:  # noqa: BLE001 - broker boundary, must not raise raw
        execution.outcome = "failure"
        execution.completed_at = _now()
        execution.trace_complete = True
        session.commit()
        audit_service.record(session, execution.id, "execution_failed", {"error": str(exc)})
        return None

    execution.outcome = "success"
    execution.completed_at = _now()
    execution.trace_complete = True
    execution.safe_result_ref = _ref(result)
    session.commit()
    audit_service.record(session, execution.id, "execution_completed", {"result": result})
    return result


def _to_response(execution: Execution, *, result: dict[str, Any] | None, reason: str | None) -> ToolCallResponse:
    return ToolCallResponse(
        execution_id=execution.id,
        decision=execution.decision,
        outcome=execution.outcome,
        policy_version=execution.policy_version,
        approval_id=execution.approval_id,
        result=result,
        reason=reason,
    )
