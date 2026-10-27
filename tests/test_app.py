"""HTTP-level checks for endpoint authorization."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from agentguard.config import settings
from agentguard.db import get_session
from agentguard.gateway.app import app
from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF, register_agent

AGENT = {"x-workload-identity": MOCK_AGENT_WORKLOAD_REF}
CALL = {
    "tool_name": "read-ticket",
    "environment": "dev",
    "resource_tenant_id": "tenant-a",
    "target_resource": "ticket:T-1001",
    "args": {"ticket_id": "T-1001"},
    "idempotency_key": "http-1",
}


@pytest.fixture()
def client(session: Session):
    app.dependency_overrides[get_session] = lambda: session
    # Not used as a context manager, so the dev startup hook (which touches the real DB) doesn't run.
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_execution_status_is_only_visible_to_the_owning_agent(client, session):
    register_agent(session, "other@dev-workload-pool")
    execution_id = client.post("/v1/tool-calls", json=CALL, headers=AGENT).json()["execution_id"]

    assert client.get(f"/v1/executions/{execution_id}", headers=AGENT).status_code == 200
    assert client.get(f"/v1/executions/{execution_id}").status_code == 401
    other = {"x-workload-identity": "other@dev-workload-pool"}
    assert client.get(f"/v1/executions/{execution_id}", headers=other).status_code == 404


def test_stats_is_disabled_without_an_operator_token(client, monkeypatch):
    monkeypatch.setattr(settings, "operator_token", None)
    assert client.get("/v1/stats", headers={"x-operator-token": "anything"}).status_code == 403


def test_stats_requires_the_operator_token(client, monkeypatch):
    monkeypatch.setattr(settings, "operator_token", "test-operator-token")
    client.post("/v1/tool-calls", json=CALL, headers=AGENT)

    assert client.get("/v1/stats", headers={"x-operator-token": "wrong"}).status_code == 403
    response = client.get("/v1/stats", headers={"x-operator-token": "test-operator-token"})
    assert response.status_code == 200
    stats = response.json()
    assert stats["total_calls"] == 1
    assert stats["decision_latency_ms"]["p95"] is not None
    assert stats["trace_completeness"] == 1.0
