import uuid
from datetime import UTC

from sqlalchemy.orm import Session

from agentguard.approval import service as approval_service
from agentguard.gateway import service as gateway_service
from agentguard.gateway.schemas import ToolCallRequest
from agentguard.identity.service import resolve_identity
from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF, MOCK_APPROVER_ROLE, REFUND_THRESHOLD


def idempotency_key() -> str:
    return str(uuid.uuid4())


def _mock_identity(session: Session):
    return resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)


def test_read_ticket_is_allowed(session: Session):
    identity = _mock_identity(session)
    request = ToolCallRequest(
        tool_name="read-ticket",
        environment="dev",
        resource_tenant_id="tenant-a",
        target_resource="ticket:T-1001",
        args={"ticket_id": "T-1001"},
        idempotency_key=idempotency_key(),
    )
    response = gateway_service.submit_tool_call(session, request, identity)

    assert response.decision == "allow"
    assert response.outcome == "success"
    assert response.result == {"found": True, "ticket_id": "T-1001", "subject": "Can't log in", "status": "open"}


def test_create_ticket_is_allowed(session: Session):
    identity = _mock_identity(session)
    request = ToolCallRequest(
        tool_name="create-ticket",
        environment="dev",
        resource_tenant_id="tenant-a",
        target_resource="ticket:new",
        args={"subject": "Billing question"},
        idempotency_key=idempotency_key(),
    )
    response = gateway_service.submit_tool_call(session, request, identity)

    assert response.decision == "allow"
    assert response.outcome == "success"
    assert response.result["created"] is True


def test_refund_under_threshold_auto_allows(session: Session):
    identity = _mock_identity(session)
    request = ToolCallRequest(
        tool_name="issue-refund",
        environment="dev",
        resource_tenant_id="tenant-a",
        target_resource="ticket:T-1001",
        args={"ticket_id": "T-1001", "amount": REFUND_THRESHOLD - 50},
        idempotency_key=idempotency_key(),
    )
    response = gateway_service.submit_tool_call(session, request, identity)

    assert response.decision == "allow"
    assert response.outcome == "success"
    assert response.result["refunded"] is True


def test_refund_over_threshold_requires_approval_then_executes(session: Session):
    identity = _mock_identity(session)
    request = ToolCallRequest(
        tool_name="issue-refund",
        environment="dev",
        resource_tenant_id="tenant-a",
        target_resource="ticket:T-1001",
        args={"ticket_id": "T-1001", "amount": REFUND_THRESHOLD + 400},
        idempotency_key=idempotency_key(),
    )
    submitted = gateway_service.submit_tool_call(session, request, identity)

    assert submitted.decision == "approval_required"
    assert submitted.outcome == "not_attempted"
    assert submitted.approval_id is not None

    approver = approval_service.ApproverIdentity(
        name="lead@tenant-a", role=MOCK_APPROVER_ROLE, tenant_id="tenant-a"
    )
    approval = approval_service.decide(session, submitted.approval_id, approve=True, approver=approver)
    assert approval.status == "approved"

    executed = gateway_service.execute_approved(session, submitted.execution_id, identity)
    assert executed.outcome == "success"
    assert executed.result["refunded"] is True


def test_expired_approval_is_rejected(session: Session):
    identity = _mock_identity(session)
    request = ToolCallRequest(
        tool_name="issue-refund",
        environment="dev",
        resource_tenant_id="tenant-a",
        target_resource="ticket:T-1001",
        args={"ticket_id": "T-1001", "amount": REFUND_THRESHOLD + 1},
        idempotency_key=idempotency_key(),
    )
    submitted = gateway_service.submit_tool_call(session, request, identity)

    from datetime import datetime, timedelta

    from agentguard.models import Approval

    approval = session.get(Approval, submitted.approval_id)
    approval.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()

    approver = approval_service.ApproverIdentity(
        name="lead@tenant-a", role=MOCK_APPROVER_ROLE, tenant_id="tenant-a"
    )
    try:
        approval_service.decide(session, submitted.approval_id, approve=True, approver=approver)
        assert False, "expected ApprovalError"
    except approval_service.ApprovalError as exc:
        assert "expired" in str(exc)
