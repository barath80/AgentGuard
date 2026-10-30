"""Console OIDC sign-in: only correctly signed, unexpired tokens for this audience and issuer get in."""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from agentguard.config import settings
from agentguard.console import auth
from agentguard.db import get_session
from agentguard.gateway.app import app
from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF

ISSUER = "https://idp.example.test/realms/agentguard"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJWKS:
    def get_signing_key_from_jwt(self, _token):
        return jwt.PyJWK.from_dict({**jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True), "kid": "k1"})


def token(key=KEY, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER, "aud": "agentguard-console", "sub": "user-1", "iat": now, "exp": now + 300,
        "preferred_username": "dana", "roles": ["support-lead", "offline_access"], "tenants": ["tenant-a"],
        **overrides,
    }
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture()
def client(session: Session, monkeypatch):
    monkeypatch.setattr(settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(settings, "operator_token", "ops-token")
    monkeypatch.setattr(auth, "_jwks_client", lambda _url: FakeJWKS())
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def me(client, t):
    return client.get("/v1/console/me", headers={"Authorization": f"Bearer {t}"})


def test_valid_token_maps_claims_to_a_principal(client):
    body = me(client, token()).json()
    assert body["name"] == "dana" and body["tenants"] == ["tenant-a"]
    assert body["roles"] == ["support-lead"]  # IdP default roles are dropped


@pytest.mark.parametrize("bad", [
    {"exp": int(time.time()) - 10},
    {"aud": "some-other-app"},
    {"iss": "https://evil.example.test/realms/agentguard"},
])
def test_expired_wrong_audience_or_issuer_is_rejected(client, bad):
    assert me(client, token(**bad)).status_code == 401


def test_token_signed_by_another_key_is_rejected(client):
    assert me(client, token(key=OTHER_KEY)).status_code == 401


def test_unsigned_token_is_rejected(client):
    now = int(time.time())
    forged = jwt.encode({"iss": ISSUER, "aud": "agentguard-console", "sub": "x", "iat": now, "exp": now + 60,
                         "roles": ["operator"], "tenants": ["*"]}, key=None, algorithm="none")
    assert me(client, forged).status_code == 401


def test_viewer_with_idp_default_roles_still_cant_see_payloads(client):
    body = me(client, token(roles=["viewer", "offline_access", "default-roles-agentguard"], tenants=["*"])).json()
    assert body["roles"] == ["viewer"] and body["can_view_payloads"] is False


def test_token_without_tenants_reads_nothing(client):
    client.post("/v1/tool-calls", headers={"x-workload-identity": MOCK_AGENT_WORKLOAD_REF}, json={
        "tool_name": "read-ticket", "environment": "dev", "resource_tenant_id": "tenant-a",
        "target_resource": "ticket:T-1001", "args": {"ticket_id": "T-1001"}, "idempotency_key": "o-1",
    })
    response = client.get("/v1/console/executions", headers={"Authorization": f"Bearer {token(tenants=None)}"})
    assert response.json()["total"] == 0


def test_static_tokens_can_be_turned_off(client, monkeypatch):
    assert me(client, "ops-token").status_code == 200
    monkeypatch.setattr(settings, "console_static_tokens", False)
    assert me(client, "ops-token").status_code == 401
    assert me(client, token()).status_code == 200


def test_auth_config_is_public_and_secret_free(client):
    config = client.get("/v1/console/auth-config").json()
    assert config == {"oidc": {"authority": ISSUER, "client_id": "agentguard-console"}, "static_tokens": True}
