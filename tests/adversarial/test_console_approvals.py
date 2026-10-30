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
from agentguard.db import get_session
from agentguard.gateway.app import app
from agentguard.models import Approval
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
