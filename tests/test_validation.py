from sqlalchemy.orm import Session

from agentguard.gateway import service as gateway_service
from agentguard.gateway.validation import unknown_arg_rejection
from agentguard.identity.service import resolve_identity
from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF
from tests.conftest import tool_call


def test_unknown_field_rejected_for_high_risk_tool():
    schema = {"properties": {"ticket_id": {}, "amount": {}}}
    reason = unknown_arg_rejection("write-high", schema, {"ticket_id": "T-1", "amount": 10, "admin_override": True})
    assert reason is not None and "admin_override" in reason


def test_unknown_field_allowed_for_read_tool():
    schema = {"properties": {"ticket_id": {}}}
    assert unknown_arg_rejection("read", schema, {"ticket_id": "T-1", "extra": "fine"}) is None


def test_known_fields_only_is_never_rejected():
    schema = {"properties": {"ticket_id": {}, "amount": {}}}
    assert unknown_arg_rejection("write-high", schema, {"ticket_id": "T-1", "amount": 10}) is None


def test_gateway_denies_unexpected_fields_on_high_risk_tool(session: Session):
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)
    request = tool_call(
        "issue-refund", {"ticket_id": "T-1001", "amount": 10, "admin_override": True}
    )
    response = gateway_service.submit_tool_call(session, request, identity)
    assert response.decision == "deny"
    assert "admin_override" in response.reason


def test_gateway_still_allows_extra_fields_on_read_tool(session: Session):
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)
    request = tool_call("read-ticket", {"ticket_id": "T-1001", "trace_id": "abc"})
    response = gateway_service.submit_tool_call(session, request, identity)
    assert response.decision == "allow"
