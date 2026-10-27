import pytest
from sqlalchemy.orm import Session

from agentguard.approval import service as approval_service
from agentguard.config import settings
from agentguard.controls.anomalies import detect
from agentguard.controls.service import set_agent_status
from agentguard.gateway import service as gateway_service
from agentguard.identity.service import IdentityError, resolve_identity
from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF, MOCK_APPROVER_ROLE
from tests.conftest import tool_call


def _refund(amount: float, **overrides):
    return tool_call("issue-refund", {"ticket_id": "T-1001", "amount": amount}, **overrides)


# --- Kill switches ------------------------------------------------------------


def test_global_kill_switch_denies_everything(session: Session, monkeypatch):
    monkeypatch.setattr(settings, "kill_switch", True)
    response = gateway_service.submit_tool_call(session, tool_call(), resolve_identity(session, MOCK_AGENT_WORKLOAD_REF))
    assert response.decision == "deny"
    assert response.reason.startswith("control:kill_switch")


def test_global_kill_switch_blocks_already_approved_calls(session: Session, monkeypatch):
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)
    submitted = gateway_service.submit_tool_call(session, _refund(900), identity)
    approval_service.decide(
        session, submitted.approval_id, approve=True,
        approver=approval_service.ApproverIdentity("lead", MOCK_APPROVER_ROLE, "tenant-a"),
    )
    monkeypatch.setattr(settings, "kill_switch", True)
    with pytest.raises(gateway_service.GatewayError) as exc:
        gateway_service.execute_approved(session, submitted.execution_id, identity)
    assert exc.value.status_code == 503


def test_suspended_agent_is_rejected_on_its_next_call_and_can_be_resumed(session: Session):
    set_agent_status(session, MOCK_AGENT_WORKLOAD_REF, "suspended")
    with pytest.raises(IdentityError):
        resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)
    set_agent_status(session, MOCK_AGENT_WORKLOAD_REF, "active")
    assert resolve_identity(session, MOCK_AGENT_WORKLOAD_REF).status == "active"


# --- Rate limit, circuit breaker, budget --------------------------------------


def test_rate_limit_denies_calls_over_the_per_minute_limit(session: Session, make_agent):
    agent = make_agent(rate_limit_per_minute=3)
    decisions = [gateway_service.submit_tool_call(session, tool_call(), agent).decision for _ in range(5)]
    assert decisions == ["allow", "allow", "allow", "deny", "deny"]


def test_repeated_denials_open_the_circuit(session: Session, make_agent):
    agent = make_agent()
    for _ in range(settings.breaker_threshold):
        gateway_service.submit_tool_call(session, tool_call(resource_tenant_id="tenant-b"), agent)

    legit = gateway_service.submit_tool_call(session, tool_call(), agent)
    assert legit.decision == "deny"
    assert legit.reason.startswith("control:circuit_open")


def test_control_denials_do_not_count_towards_the_circuit_breaker(session: Session, make_agent):
    agent = make_agent(rate_limit_per_minute=1)
    reasons = [gateway_service.submit_tool_call(session, tool_call(), agent).reason for _ in range(8)]
    assert all(r.startswith("control:rate_limit") for r in reasons[1:])


def test_spend_budget_caps_total_refunds(session: Session, make_agent):
    agent = make_agent(daily_spend_limit=1000)
    outcomes = [gateway_service.submit_tool_call(session, _refund(400), agent) for _ in range(3)]
    assert [o.outcome for o in outcomes] == ["success", "success", "denied"]
    assert outcomes[2].reason.startswith("control:budget")


def test_budget_is_rechecked_when_approved_calls_execute(session: Session, make_agent):
    """Pending approvals don't spend budget, so each fits alone — but not both together."""
    agent = make_agent(daily_spend_limit=1000)
    approver = approval_service.ApproverIdentity("lead", MOCK_APPROVER_ROLE, "tenant-a")
    pending = [gateway_service.submit_tool_call(session, _refund(600), agent) for _ in range(2)]
    for p in pending:
        approval_service.decide(session, p.approval_id, approve=True, approver=approver)

    assert gateway_service.execute_approved(session, pending[0].execution_id, agent).outcome == "success"
    with pytest.raises(gateway_service.GatewayError, match="budget"):
        gateway_service.execute_approved(session, pending[1].execution_id, agent)


# --- Fail closed (THREAT_MODEL #6) --------------------------------------------


@pytest.fixture()
def policy_down(monkeypatch):
    def broken(*_args, **_kwargs):
        raise ConnectionError("policy store unreachable")

    monkeypatch.setattr(gateway_service, "evaluate", broken)


def test_policy_outage_denies_by_default(session: Session, policy_down):
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)
    for request in (tool_call(), _refund(10)):
        response = gateway_service.submit_tool_call(session, request, identity)
        assert response.decision == "deny"
        assert response.reason == "policy unavailable: failing closed"


def test_read_only_fallback_allows_reads_but_never_writes(session: Session, policy_down, monkeypatch):
    monkeypatch.setattr(settings, "allow_reads_when_policy_unavailable", True)
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)

    read = gateway_service.submit_tool_call(session, tool_call(), identity)
    write = gateway_service.submit_tool_call(session, _refund(10), identity)

    assert (read.decision, read.outcome) == ("allow", "success")
    assert read.reason == "policy unavailable: read-only fallback"
    assert write.decision == "deny"


def test_policy_outage_blocks_execution_of_approved_calls(session: Session, monkeypatch):
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)
    submitted = gateway_service.submit_tool_call(session, _refund(900), identity)
    approval_service.decide(
        session, submitted.approval_id, approve=True,
        approver=approval_service.ApproverIdentity("lead", MOCK_APPROVER_ROLE, "tenant-a"),
    )
    monkeypatch.setattr(gateway_service, "evaluate", lambda *a, **k: (_ for _ in ()).throw(ConnectionError()))
    with pytest.raises(gateway_service.GatewayError) as exc:
        gateway_service.execute_approved(session, submitted.execution_id, identity)
    assert exc.value.status_code == 503


# --- Anomaly report -----------------------------------------------------------


def test_anomaly_report_flags_bursts_spikes_and_unusual_sequences(session: Session, make_agent):
    spammer = make_agent(rate_limit_per_minute=2)
    for _ in range(4):
        gateway_service.submit_tool_call(session, tool_call(), spammer)

    prober = make_agent()
    for _ in range(settings.breaker_threshold):
        gateway_service.submit_tool_call(session, tool_call(resource_tenant_id="tenant-b"), prober)

    blind_refunder = make_agent()
    gateway_service.submit_tool_call(
        session, tool_call("issue-refund", {"ticket_id": "T-777", "amount": 10}), blind_refunder
    )

    kinds = {(f.kind, f.agent_id) for f in detect(session)}
    assert ("control:rate_limit", spammer.agent_id) in kinds
    assert ("denial_burst", prober.agent_id) in kinds
    assert ("refund_without_read", blind_refunder.agent_id) in kinds
