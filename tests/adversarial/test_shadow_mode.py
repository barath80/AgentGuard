"""Policy shadow mode: a proposed policy runs beside the live one and only observes.

The guarantees that matter: whatever the shadow says, even "allow everything", the live
decision is unchanged; a broken shadow can't fail or alter a live call; only operators with
all-tenant read access can start one; and the report counts agreements and differences exactly.
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from agentguard.config import settings
from agentguard.console.auth import hash_token
from agentguard.db import get_session
from agentguard.gateway.app import app
from agentguard.models import Execution, ShadowPolicy
from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF

AGENT = {"x-workload-identity": MOCK_AGENT_WORKLOAD_REF}
OPS = {"Authorization": "Bearer ops-token"}
ALLOW_ALL = [{"match": {}, "decision": "allow", "priority": 1}]
DENY_REFUNDS = [
    {"match": {"tool": "issue-refund"}, "decision": "deny", "priority": 1},
    {"match": {}, "decision": "allow", "priority": 100},
]


@pytest.fixture()
def client(session: Session, monkeypatch):
    monkeypatch.setattr(settings, "operator_token", "ops-token")
    monkeypatch.setattr(settings, "console_principals", json.dumps([
        {"name": "dana", "roles": ["support-lead"], "tenants": ["tenant-a"], "token_sha256": hash_token("lead-a")},
        {"name": "sam", "roles": ["viewer"], "tenants": ["*"], "token_sha256": hash_token("viewer")},
    ]))
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def call(client, tool="read-ticket", args=None, **overrides) -> dict:
    body = {
        "tool_name": tool, "environment": "dev", "resource_tenant_id": "tenant-a", "target_resource": "ticket:T-1001",
        "args": args if args is not None else {"ticket_id": "T-1001"}, "idempotency_key": str(uuid.uuid4()), **overrides,
    }
    response = client.post("/v1/tool-calls", json=body, headers=AGENT)
    assert response.status_code == 200, response.text
    return response.json()


def start(client, rules, token="ops-token"):
    return client.post("/v1/console/shadow", json={"rules": rules, "description": "trial"},
                       headers={"Authorization": f"Bearer {token}"})


def test_a_permissive_shadow_never_changes_a_live_decision(client, session):
    assert start(client, ALLOW_ALL).status_code == 201
    gated = call(client, "issue-refund", {"ticket_id": "T-1001", "amount": 900})
    denied = call(client, "issue-refund", {"ticket_id": "T-1001", "amount": 10}, environment="prod")
    read = call(client)

    assert gated["decision"] == "approval_required" and gated["outcome"] == "not_attempted"
    assert denied["decision"] == "deny"
    assert read["decision"] == "allow"
    recorded = session.get(Execution, gated["execution_id"])
    assert (recorded.decision, recorded.shadow_decision) == ("approval_required", "allow")
    # Pre-policy denials never reach policy, so the shadow isn't asked (policy can't change them).
    assert session.get(Execution, denied["execution_id"]).shadow_decision is None


def test_report_counts_agreement_and_both_kinds_of_difference(client):
    start(client, DENY_REFUNDS)
    call(client)  # allow / allow
    call(client, "issue-refund", {"ticket_id": "T-1001", "amount": 20})  # allow / deny: stricter
    start(client, ALLOW_ALL)  # replaces the first shadow
    call(client, "issue-refund", {"ticket_id": "T-1001", "amount": 900})  # approval_required / allow: looser

    report = client.get("/v1/console/shadow", headers=OPS).json()
    assert report["running"] is True and report["shadow"]["rules"][0]["decision"] == "allow"
    assert (report["compared"], report["looser"], report["stricter"]) == (1, 1, 0)
    assert report["differences"][0]["direction"] == "looser"
    assert report["differences"][0]["shadow_decision"] == "allow"

    assert client.delete("/v1/console/shadow", headers=OPS).status_code == 200
    assert client.get("/v1/console/shadow", headers=OPS).json()["running"] is False
    assert client.delete("/v1/console/shadow", headers=OPS).status_code == 404
    assert call(client).get("decision") == "allow"  # stopped: nothing recorded any more


def test_stricter_differences_are_reported(client):
    start(client, DENY_REFUNDS)
    call(client)
    call(client, "issue-refund", {"ticket_id": "T-1001", "amount": 20})
    report = client.get("/v1/console/shadow", headers=OPS).json()
    assert (report["compared"], report["agreement"], report["stricter"]) == (2, 0.5, 1)


def test_a_broken_shadow_cannot_fail_or_alter_a_live_call(client, session):
    # Rules that would raise inside the engine, written straight to the table.
    session.add(ShadowPolicy(rules=[{"id": "x", "match": {}, "decision": "maybe", "priority": 1}], started_by="ops"))
    session.commit()
    response = call(client)
    assert response["decision"] == "allow" and response["outcome"] == "success"
    assert session.get(Execution, response["execution_id"]).shadow_decision is None


def test_only_all_tenant_operators_run_shadows_and_rules_are_validated(client):
    assert start(client, ALLOW_ALL, token="lead-a").status_code == 403
    assert start(client, ALLOW_ALL, token="viewer").status_code == 403
    typo = [{"match": {"tool_name": "issue-refund"}, "decision": "allow", "priority": 1}]
    assert start(client, typo).status_code == 422
    assert client.get("/v1/console/shadow", headers={"Authorization": "Bearer lead-a"}).status_code == 403
