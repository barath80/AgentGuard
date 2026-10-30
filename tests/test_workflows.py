"""Multi-step workflow grouping and partial-completion reporting (THREAT_MODEL #18)."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from agentguard.config import settings
from agentguard.console.auth import hash_token
from agentguard.db import get_session
from agentguard.gateway.app import app
from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF, register_agent

AGENT = {"x-workload-identity": MOCK_AGENT_WORKLOAD_REF}
OPS = {"Authorization": "Bearer ops-token"}


@pytest.fixture()
def client(session: Session, monkeypatch):
    monkeypatch.setattr(settings, "operator_token", "ops-token")
    monkeypatch.setattr(settings, "console_principals", "")
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def step(client, workflow, tool="read-ticket", args=None, headers=AGENT, **overrides):
    body = {
        "tool_name": tool, "environment": "dev", "resource_tenant_id": "tenant-a", "target_resource": "ticket:T-1001",
        "args": args if args is not None else {"ticket_id": "T-1001"}, "idempotency_key": str(uuid.uuid4()),
        "workflow_id": workflow, **overrides,
    }
    response = client.post("/v1/tool-calls", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def workflows(client, **params) -> dict[str, dict]:
    response = client.get("/v1/console/workflows", params=params, headers=OPS)
    assert response.status_code == 200, response.text
    return {w["workflow_id"]: w for w in response.json()}


def test_workflow_statuses(client):
    step(client, "done")
    step(client, "done", "issue-refund", {"ticket_id": "T-1001", "amount": 20})

    step(client, "half-done", "create-ticket", {"subject": "Replacement order", "body": ""}, target_resource="ticket:new")
    step(client, "half-done", "issue-refund", {"ticket_id": "T-1001", "amount": 20}, environment="prod")  # denied

    step(client, "safe-to-retry")
    step(client, "safe-to-retry", "issue-refund", {"ticket_id": "T-1001", "amount": 20}, environment="prod")

    step(client, "waiting")
    step(client, "waiting", "issue-refund", {"ticket_id": "T-1001", "amount": 900})  # needs approval

    found = workflows(client)
    assert {k: w["status"] for k, w in found.items()} == {
        "done": "completed", "half-done": "partial", "safe-to-retry": "incomplete", "waiting": "in_progress",
    }
    half = found["half-done"]
    assert [s["state"] for s in half["steps"]] == ["done", "blocked"]
    assert [s["tool_name"] for s in half["side_effects"]] == ["create-ticket"]
    assert found["safe-to-retry"]["side_effects"] == []
    assert list(workflows(client, status="partial")) == ["half-done"]


def test_workflow_labels_are_scoped_to_the_agent(client, session):
    # Another agent reusing the label can't append steps to (or hide steps in) this agent's workflow.
    register_agent(session, "other-agent@dev-workload-pool")
    step(client, "shared-label", "create-ticket", {"subject": "x", "body": ""}, target_resource="ticket:new")
    step(client, "shared-label", "issue-refund", {"ticket_id": "T-1001", "amount": 20}, environment="prod",
         headers={"x-workload-identity": "other-agent@dev-workload-pool"})

    response = client.get("/v1/console/workflows", headers=OPS).json()
    assert sorted((w["agent_name"], w["status"]) for w in response) == [
        ("mock-support-agent", "completed"), ("other-agent", "incomplete"),
    ]


def test_workflow_label_is_validated_and_never_affects_the_decision(client):
    bad = client.post("/v1/tool-calls", headers=AGENT, json={
        "tool_name": "read-ticket", "environment": "dev", "resource_tenant_id": "tenant-a",
        "target_resource": "ticket:T-1001", "args": {"ticket_id": "T-1001"}, "idempotency_key": "k1",
        "workflow_id": "x" * 101,
    })
    assert bad.status_code == 422
    denied = step(client, "approved-by-ops", "issue-refund", {"ticket_id": "T-1001", "amount": 1}, environment="prod")
    assert denied["decision"] == "deny"


def test_tenant_scoped_readers_only_see_their_tenants_workflows(client, monkeypatch):
    monkeypatch.setattr(settings, "console_principals", json.dumps([
        {"name": "lee", "roles": ["support-lead"], "tenants": ["tenant-b"], "token_sha256": hash_token("lead-b")},
    ]))
    step(client, "tenant-a-work")
    assert client.get("/v1/console/workflows", headers={"Authorization": "Bearer lead-b"}).json() == []
