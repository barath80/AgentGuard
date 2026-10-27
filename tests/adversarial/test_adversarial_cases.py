"""Adversarial cases from docs/EVALUATION.md.

Every case here must result in: deny / rejected, and no side effect.
If any of these ever starts passing with a different (permissive)
outcome, that's a false-allow and must block the change — see
docs/EVALUATION.md's CI wiring note.
"""

import uuid

import pytest
from sqlalchemy.orm import Session

from agentguard.approval import service as approval_service
from agentguard.gateway import service as gateway_service
from agentguard.gateway.schemas import ToolCallRequest
from agentguard.identity.service import IdentityError, resolve_identity
from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF, MOCK_APPROVER_ROLE, REFUND_THRESHOLD


def idempotency_key() -> str:
    return str(uuid.uuid4())


def test_spoofed_identity_header_is_rejected(session: Session):
    """An unknown/forged workload identity must not resolve to any agent."""
    with pytest.raises(IdentityError):
        resolve_identity(session, "someone-else@attacker-controlled")


def test_agent_id_in_request_body_is_ignored(session: Session):
    """ToolCallRequest has no agent_id/tenant_id field for the caller's own
    identity — only `resource_tenant_id` describing the target. Even if an
    attacker stuffs extra fields into the JSON body, Pydantic drops unknown
    fields and the gateway never reads identity from the body.
    """
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)
    malicious_payload = {
        "tool_name": "read-ticket",
        "environment": "dev",
        "resource_tenant_id": "tenant-a",
        "target_resource": "ticket:T-1001",
        "args": {"ticket_id": "T-1001"},
        "idempotency_key": idempotency_key(),
        "agent_id": "root-admin",  # attacker-supplied, must be ignored
        "tenant_id": "tenant-b",  # attacker-supplied, must be ignored
    }
    request = ToolCallRequest(**malicious_payload)
    response = gateway_service.submit_tool_call(session, request, identity)

    # Resolved from the verified identity, not from the forged body fields.
    assert response.decision == "allow"


def test_tenant_isolation_denies_cross_tenant_access(session: Session):
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)  # belongs to tenant-a
    request = ToolCallRequest(
        tool_name="read-ticket",
        environment="dev",
        resource_tenant_id="tenant-b",
        target_resource="ticket:T-9999",
        args={"ticket_id": "T-9999"},
        idempotency_key=idempotency_key(),
    )
    response = gateway_service.submit_tool_call(session, request, identity)

    assert response.decision == "deny"
    assert "tenant" in response.reason


def test_prohibited_production_access_is_denied(session: Session):
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)  # approved_environments: dev, staging
    request = ToolCallRequest(
        tool_name="issue-refund",
        environment="prod",
        resource_tenant_id="tenant-a",
        target_resource="ticket:T-1001",
        args={"ticket_id": "T-1001", "amount": 10},
        idempotency_key=idempotency_key(),
    )
    response = gateway_service.submit_tool_call(session, request, identity)

    assert response.decision == "deny"
    assert response.outcome == "denied"


def test_duplicate_call_does_not_execute_twice(session: Session):
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)
    key = idempotency_key()
    request = ToolCallRequest(
        tool_name="issue-refund",
        environment="dev",
        resource_tenant_id="tenant-a",
        target_resource="ticket:T-1001",
        args={"ticket_id": "T-1001", "amount": 50},
        idempotency_key=key,
    )
    first = gateway_service.submit_tool_call(session, request, identity)
    second = gateway_service.submit_tool_call(session, request, identity)

    assert first.execution_id == second.execution_id
    assert second.reason is not None and "duplicate" in second.reason

    from agentguard.models import Execution

    count = (
        session.query(Execution)
        .filter(Execution.idempotency_key == key, Execution.agent_id == identity.agent_id)
        .count()
    )
    assert count == 1


def test_replayed_approval_cannot_be_consumed_twice(session: Session):
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)
    request = ToolCallRequest(
        tool_name="issue-refund",
        environment="dev",
        resource_tenant_id="tenant-a",
        target_resource="ticket:T-1001",
        args={"ticket_id": "T-1001", "amount": REFUND_THRESHOLD + 200},
        idempotency_key=idempotency_key(),
    )
    submitted = gateway_service.submit_tool_call(session, request, identity)
    approver = approval_service.ApproverIdentity(
        name="lead@tenant-a", role=MOCK_APPROVER_ROLE, tenant_id="tenant-a"
    )
    approval_service.decide(session, submitted.approval_id, approve=True, approver=approver)

    gateway_service.execute_approved(session, submitted.execution_id, identity)

    # Replay: attempt to consume (execute against) the same approval again.
    with pytest.raises(gateway_service.GatewayError):
        gateway_service.execute_approved(session, submitted.execution_id, identity)


def test_wrong_tenant_approver_is_rejected(session: Session):
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)  # tenant-a
    request = ToolCallRequest(
        tool_name="issue-refund",
        environment="dev",
        resource_tenant_id="tenant-a",
        target_resource="ticket:T-1001",
        args={"ticket_id": "T-1001", "amount": REFUND_THRESHOLD + 10},
        idempotency_key=idempotency_key(),
    )
    submitted = gateway_service.submit_tool_call(session, request, identity)

    wrong_tenant_approver = approval_service.ApproverIdentity(
        name="someone@tenant-b", role=MOCK_APPROVER_ROLE, tenant_id="tenant-b"
    )
    with pytest.raises(approval_service.ApprovalError):
        approval_service.decide(
            session, submitted.approval_id, approve=True, approver=wrong_tenant_approver
        )


def test_wrong_role_approver_is_rejected(session: Session):
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)
    request = ToolCallRequest(
        tool_name="issue-refund",
        environment="dev",
        resource_tenant_id="tenant-a",
        target_resource="ticket:T-1001",
        args={"ticket_id": "T-1001", "amount": REFUND_THRESHOLD + 10},
        idempotency_key=idempotency_key(),
    )
    submitted = gateway_service.submit_tool_call(session, request, identity)

    unauthorized_approver = approval_service.ApproverIdentity(
        name="random@tenant-a", role="intern", tenant_id="tenant-a"
    )
    with pytest.raises(approval_service.ApprovalError):
        approval_service.decide(
            session, submitted.approval_id, approve=True, approver=unauthorized_approver
        )


def test_policy_change_while_approval_pending_requires_revalidation(session: Session):
    """Edge case #3: an approval bound to policy v1 must not silently
    authorize execution once v1 is retired in favor of v2.
    """
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)
    request = ToolCallRequest(
        tool_name="issue-refund",
        environment="dev",
        resource_tenant_id="tenant-a",
        target_resource="ticket:T-1001",
        args={"ticket_id": "T-1001", "amount": REFUND_THRESHOLD + 10},
        idempotency_key=idempotency_key(),
    )
    submitted = gateway_service.submit_tool_call(session, request, identity)
    approver = approval_service.ApproverIdentity(
        name="lead@tenant-a", role=MOCK_APPROVER_ROLE, tenant_id="tenant-a"
    )
    approval_service.decide(session, submitted.approval_id, approve=True, approver=approver)

    from agentguard.models import Policy

    active_policy = session.query(Policy).filter(Policy.is_active.is_(True)).one()
    active_policy.is_active = False
    new_policy = Policy(version=active_policy.version + 1, scope="global", is_active=True)
    session.add(new_policy)
    session.commit()

    with pytest.raises(gateway_service.GatewayError):
        gateway_service.execute_approved(session, submitted.execution_id, identity)
