"""Operator console API: authentication, tenant scoping, payload visibility, actions."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from agentguard.config import settings
from agentguard.console.auth import hash_token
from agentguard.console.routes import get_read_only_session
from agentguard.db import get_session
from agentguard.gateway.app import app
from agentguard.models import AuditEvent, OperatorAction
from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF, register_agent

AGENT = {"x-workload-identity": MOCK_AGENT_WORKLOAD_REF}

PRINCIPALS = {
    "ops-token": None,  # the OPERATOR_TOKEN principal: operator, all tenants
    "lead-a-token": {"name": "dana", "roles": ["support-lead"], "tenants": ["tenant-a"]},
    "lead-b-token": {"name": "lee", "roles": ["support-lead"], "tenants": ["tenant-b"]},
    "viewer-token": {"name": "sam", "roles": ["viewer"], "tenants": ["*"]},
}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def client(session: Session, monkeypatch):
    monkeypatch.setattr(settings, "operator_token", "ops-token")
    monkeypatch.setattr(settings, "console_principals", json.dumps([
        {**p, "token_sha256": hash_token(token)} for token, p in PRINCIPALS.items() if p
    ]))
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_read_only_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def submit(client: TestClient, tool="read-ticket", args=None, headers=AGENT, **overrides) -> dict:
    body = {
        "tool_name": tool,
        "environment": "dev",
        "resource_tenant_id": "tenant-a",
        "target_resource": "ticket:T-1001",
        "args": args if args is not None else {"ticket_id": "T-1001"},
        "idempotency_key": str(uuid.uuid4()),
        **overrides,
    }
    return client.post("/v1/tool-calls", json=body, headers=headers).json()


def test_console_requires_a_known_bearer_token(client):
    assert client.get("/v1/console/me").status_code == 401
    assert client.get("/v1/console/me", headers=bearer("nope")).status_code == 401
    assert client.get("/v1/console/me", headers={"Authorization": "ops-token"}).status_code == 401
    me = client.get("/v1/console/me", headers=bearer("lead-a-token")).json()
    assert me["name"] == "dana" and me["tenants"] == ["tenant-a"]


def test_malformed_principal_config_signs_nobody_in(client, monkeypatch):
    monkeypatch.setattr(settings, "console_principals", "{not json")
    assert client.get("/v1/console/me", headers=bearer("ops-token")).status_code == 401


def test_executions_are_scoped_to_the_principals_tenants(client, session):
    register_agent(session, "b-agent@dev-workload-pool", tenant_id="tenant-b")
    submit(client)
    submit(client, headers={"x-workload-identity": "b-agent@dev-workload-pool"}, resource_tenant_id="tenant-b")

    everything = client.get("/v1/console/executions", headers=bearer("ops-token")).json()
    tenant_a = client.get("/v1/console/executions", headers=bearer("lead-a-token")).json()
    assert everything["total"] == 2
    assert tenant_a["total"] == 1 and tenant_a["items"][0]["tenant_id"] == "tenant-a"

    a_id = tenant_a["items"][0]["id"]
    assert client.get(f"/v1/console/executions/{a_id}", headers=bearer("lead-b-token")).status_code == 404


def test_execution_detail_has_audit_timeline_and_hides_payload_from_viewers(client):
    execution_id = submit(client, "issue-refund", {"ticket_id": "T-1001", "amount": 50})["execution_id"]

    detail = client.get(f"/v1/console/executions/{execution_id}", headers=bearer("lead-a-token")).json()
    assert detail["args"] == {"ticket_id": "T-1001", "amount": 50}
    assert detail["matched_rule"]["decision"] == "allow"
    assert [e["event_type"] for e in detail["audit_events"]] == ["decision_made", "execution_completed"]

    viewer = client.get(f"/v1/console/executions/{execution_id}", headers=bearer("viewer-token")).json()
    assert viewer["args"] is None and viewer["payload_visible"] is False
    assert viewer["safe_request_ref"].startswith("sha256:")


def test_overview_counts_decisions_in_the_window(client):
    submit(client)
    submit(client, environment="prod")
    overview = client.get("/v1/console/overview?window_hours=1", headers=bearer("ops-token")).json()
    assert overview["totals"]["calls"] == 2
    assert overview["totals"]["by_decision"] == {"allow": 1, "deny": 1}
    assert sum(b["allow"] + b["deny"] for b in overview["series"]) == 2
    assert overview["posture"]["active_policy_version"] == 1
    assert client.get("/v1/console/overview?window_hours=5", headers=bearer("ops-token")).status_code == 422


def test_approver_decides_from_the_console_and_agent_executes(client, session):
    pending = submit(client, "issue-refund", {"ticket_id": "T-1001", "amount": 900})
    approval_id = pending["approval_id"]

    queue = client.get("/v1/console/approvals?status=pending", headers=bearer("lead-a-token")).json()
    assert queue[0]["id"] == approval_id and queue[0]["can_decide"] is True
    assert queue[0]["args"]["amount"] == 900

    decided = client.post(f"/v1/console/approvals/{approval_id}/decide", json={"approve": True},
                          headers=bearer("lead-a-token"))
    assert decided.status_code == 200 and decided.json()["status"] == "approved"

    executed = client.post(f"/v1/tool-calls/{pending['execution_id']}/execute", headers=AGENT).json()
    assert executed["outcome"] == "success"

    events = [e.event_type for e in session.query(AuditEvent).filter_by(execution_id=pending["execution_id"])]
    assert "approval_decided" in events
    action = session.query(OperatorAction).one()
    assert (action.actor, action.action, action.target_id) == ("dana", "approval.approved", approval_id)

    again = client.post(f"/v1/console/approvals/{approval_id}/decide", json={"approve": True},
                        headers=bearer("lead-a-token"))
    assert again.status_code == 409


def test_operator_suspends_and_resumes_an_agent(client):
    agent = client.get("/v1/console/agents", headers=bearer("ops-token")).json()[0]
    assert client.post(f"/v1/console/agents/{agent['id']}/suspend", headers=bearer("lead-a-token")).status_code == 403

    assert client.post(f"/v1/console/agents/{agent['id']}/suspend", headers=bearer("ops-token")).json()["status"] == "suspended"
    blocked = client.post("/v1/tool-calls", headers=AGENT, json={
        "tool_name": "read-ticket", "environment": "dev", "resource_tenant_id": "tenant-a",
        "target_resource": "ticket:T-1001", "args": {"ticket_id": "T-1001"}, "idempotency_key": "s-1",
    })
    assert blocked.status_code == 401

    client.post(f"/v1/console/agents/{agent['id']}/resume", headers=bearer("ops-token"))
    assert submit(client)["decision"] == "allow"
    actions = client.get("/v1/console/operator-actions", headers=bearer("ops-token")).json()
    assert [a["action"] for a in actions] == ["agent.active", "agent.suspended"]


def test_simulation_reports_changed_decisions_without_executing(client, session):
    submit(client, "issue-refund", {"ticket_id": "T-1001", "amount": 300})
    with open(settings.proposed_policies_dir + "/v2-stricter-refunds.json") as f:
        proposal = json.load(f)
    calls_before = session.query(AuditEvent).count()

    report = client.post("/v1/console/simulations", json=proposal, headers=bearer("viewer-token")).json()
    assert report["transitions"] == {"allow->approval_required": 1}
    assert report["newly_restricted"] == 1 and report["verdict"] == "safe_to_review"
    assert report["changes"][0]["impact"] == "task_success_risk"
    assert session.query(AuditEvent).count() == calls_before

    loosen = {"version": 3, "rules": [{"priority": 1, "decision": "allow", "match": {}}]}
    submit(client, "issue-refund", {"ticket_id": "T-1001", "amount": 900})  # gated under v1
    assert client.post("/v1/console/simulations", json=loosen, headers=bearer("ops-token")).json()["verdict"] == "loosens_security"


def test_simulation_needs_all_tenant_read_and_a_valid_policy(client):
    proposal = {"version": 2, "rules": [{"decision": "allow", "match": {}}]}
    assert client.post("/v1/console/simulations", json=proposal, headers=bearer("lead-a-token")).status_code == 403
    bad = {"version": 2, "rules": [{"decision": "maybe", "match": {}}]}
    assert client.post("/v1/console/simulations", json=bad, headers=bearer("ops-token")).status_code == 422
    bad_op = {"version": 2, "rules": [{"decision": "allow", "match": {"arg": {"field": "amount", "op": "~"}}}]}
    submit(client, "issue-refund", {"ticket_id": "T-1001", "amount": 5})
    assert client.post("/v1/console/simulations", json=bad_op, headers=bearer("ops-token")).status_code == 422


def test_replay_and_policies_and_tools(client):
    submit(client)
    replay = client.post("/v1/console/replay", headers=bearer("ops-token")).json()
    assert replay["replayed"] == replay["reproduced"] == 1

    policies = client.get("/v1/console/policies", headers=bearer("lead-a-token")).json()
    assert policies["versions"][0]["is_active"] and len(policies["versions"][0]["rules"]) == 4
    assert any(p["name"] == "v2-stricter-refunds" for p in policies["proposed"])

    tools = {t["name"]: t for t in client.get("/v1/console/tools", headers=bearer("lead-b-token")).json()}
    assert tools["issue-refund"]["risk_class"] == "write-high"
    assert tools["read-ticket"]["calls_total"] == 0  # tenant-b can't count tenant-a's calls


def test_health_probes(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_live_changes_are_tenant_scoped_and_include_approvals(client, session):
    from datetime import UTC, datetime, timedelta

    from agentguard.console.auth import Principal
    from agentguard.console.live import collect_changes

    since = datetime.now(UTC) - timedelta(seconds=5)
    register_agent(session, "b2@dev-workload-pool", tenant_id="tenant-b")
    submit(client)
    submit(client, headers={"x-workload-identity": "b2@dev-workload-pool"}, resource_tenant_id="tenant-b")
    pending = submit(client, "issue-refund", {"ticket_id": "T-1001", "amount": 900})

    dana = Principal("dana", frozenset({"support-lead"}), frozenset({"tenant-a"}))
    changes = collect_changes(session, dana, since)
    assert {e["tenant_id"] for e in changes["executions"]} == {"tenant-a"}
    assert len(changes["executions"]) == 2
    assert changes["approvals"] == [{"id": pending["approval_id"], "status": "pending", "execution_id": pending["execution_id"]}]


def test_live_stream_requires_sign_in(client):
    assert client.get("/v1/console/stream").status_code == 401
