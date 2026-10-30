"""Adversarial cases for approving through the operator console (THREAT_MODEL #4, #12).

The console derives the approver from the bearer token's principal. These
check that nothing in the request can borrow someone else's authority.
"""

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
from agentguard.models import Approval, OperatorAction, Policy
from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF

AGENT = {"x-workload-identity": MOCK_AGENT_WORKLOAD_REF}


@pytest.fixture()
def client(session: Session, monkeypatch):
    monkeypatch.setattr(settings, "operator_token", "ops-token")
    monkeypatch.setattr(settings, "console_principals", json.dumps([
        {"name": "dana", "roles": ["support-lead"], "tenants": ["tenant-a"], "token_sha256": hash_token("lead-a")},
        {"name": "lee", "roles": ["support-lead"], "tenants": ["tenant-b"], "token_sha256": hash_token("lead-b")},
        {"name": "ari", "roles": ["support-agent"], "tenants": ["tenant-a"], "token_sha256": hash_token("agent-a")},
        {"name": "sam", "roles": ["viewer", "support-lead"], "tenants": ["*"], "token_sha256": hash_token("wild")},
    ]))
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_read_only_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _pending_refund(client: TestClient) -> str:
    response = client.post("/v1/tool-calls", headers=AGENT, json={
        "tool_name": "issue-refund", "environment": "dev", "resource_tenant_id": "tenant-a",
        "target_resource": "ticket:T-1001", "args": {"ticket_id": "T-1001", "amount": 900},
        "idempotency_key": str(uuid.uuid4()),
    }).json()
    return response["approval_id"]


def _decide(client: TestClient, approval_id: str, token: str, **headers) -> int:
    return client.post(
        f"/v1/console/approvals/{approval_id}/decide", json={"approve": True},
        headers={"Authorization": f"Bearer {token}", **headers},
    ).status_code


def test_approver_headers_are_ignored_by_the_console(client, session):
    approval_id = _pending_refund(client)
    spoofed = {"x-approver-name": "dana", "x-approver-role": "support-lead", "x-approver-tenant": "tenant-a"}
    assert _decide(client, approval_id, "agent-a", **spoofed) == 403
    assert session.get(Approval, approval_id).status == "pending"


def test_header_based_approval_route_is_gone(client, session):
    # Approver identity once came from x-approver-* headers on this route; only the console route remains.
    approval_id = _pending_refund(client)
    spoofed = {"x-approver-name": "dana", "x-approver-role": "support-lead", "x-approver-tenant": "tenant-a"}
    response = client.post(f"/v1/approvals/{approval_id}/decide", json={"approve": True}, headers=spoofed)
    assert response.status_code in (404, 405)
    assert session.get(Approval, approval_id).status == "pending"


def test_approver_from_another_tenant_cannot_see_or_decide(client, session):
    approval_id = _pending_refund(client)
    assert _decide(client, approval_id, "lead-b") == 404
    assert session.get(Approval, approval_id).status == "pending"


def test_wildcard_read_access_does_not_grant_approval_authority(client, session):
    approval_id = _pending_refund(client)
    assert _decide(client, approval_id, "wild") == 403
    assert _decide(client, approval_id, "ops-token") == 403
    assert session.get(Approval, approval_id).status == "pending"


def test_console_approval_is_still_single_use(client):
    approval_id = _pending_refund(client)
    assert _decide(client, approval_id, "lead-a") == 200
    assert _decide(client, approval_id, "lead-a") == 409


# --- Publishing policy from the console ---------------------------------------------

ALLOW_EVERYTHING = [{"match": {}, "decision": "allow", "priority": 1}]


def _publish(client, token, rules, **extra):
    return client.post("/v1/console/policies", json={"rules": rules, **extra},
                       headers={"Authorization": f"Bearer {token}"})


def test_only_operators_publish_policy(client):
    assert _publish(client, "lead-a", ALLOW_EVERYTHING).status_code == 403
    assert _publish(client, "wild", ALLOW_EVERYTHING).status_code == 403


def test_loosening_policy_needs_an_acknowledged_reason(client, session):
    _pending_refund(client)  # recorded as approval_required; allow-everything would newly allow it
    assert _publish(client, "ops-token", ALLOW_EVERYTHING).status_code == 409
    assert _publish(client, "ops-token", ALLOW_EVERYTHING, acknowledge_loosening=True).status_code == 422
    assert session.query(Policy).filter(Policy.is_active.is_(True)).one().version == 1  # nothing changed

    ok = _publish(client, "ops-token", ALLOW_EVERYTHING, acknowledge_loosening=True,
                  reason="incident drill: temporarily open, reverted after")
    assert ok.status_code == 201 and ok.json()["newly_allowed"] == 1
    action = session.query(OperatorAction).filter_by(action="policy.published").one()
    assert action.detail["loosening_acknowledged"] is True and action.detail["reason"].startswith("incident drill")


def test_a_typo_in_a_match_key_is_rejected_not_silently_widened(client):
    # "tool_name" isn't a match key; ignored, it would turn this deny into "deny everything"
    # and, on an allow rule, "allow everything".
    typo = [{"match": {"tool_name": "issue-refund"}, "decision": "allow", "priority": 1}]
    assert _publish(client, "ops-token", typo).status_code == 422
    sim = client.post("/v1/console/simulations", json={"version": 9, "rules": typo},
                      headers={"Authorization": "Bearer ops-token"})
    assert sim.status_code == 422


def test_pending_approval_cannot_execute_after_a_policy_is_published(client):
    response = client.post("/v1/tool-calls", headers=AGENT, json={
        "tool_name": "issue-refund", "environment": "dev", "resource_tenant_id": "tenant-a",
        "target_resource": "ticket:T-1001", "args": {"ticket_id": "T-1001", "amount": 900},
        "idempotency_key": str(uuid.uuid4()),
    }).json()
    same_rules_again = [
        {"match": {"tool": "issue-refund", "arg": {"field": "amount", "op": "gt", "value": 500}},
         "decision": "approval_required", "approval_role": "support-lead", "priority": 20},
        {"match": {}, "decision": "allow", "priority": 100},
    ]
    assert _publish(client, "ops-token", same_rules_again).status_code == 201
    # Approved after the publish, but bound to the version it was issued under: it can't run.
    assert _decide(client, response["approval_id"], "lead-a") == 200
    executed = client.post(f"/v1/tool-calls/{response['execution_id']}/execute", headers=AGENT)
    assert executed.status_code == 409 and "policy has changed" in executed.json()["detail"]
