"""Agent authentication with client-credentials tokens (AGENT_AUTH=oidc, production).

Only a correctly signed, unexpired token issued for the gateway's audience to an agent's
service account identifies an agent. The x-workload-identity header that dev mode trusts must
count for nothing here; it would let anyone who can reach the API claim to be any agent.
"""

import time
import uuid

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from agentguard.config import settings
from agentguard.db import get_session
from agentguard.gateway.app import app
from agentguard.identity import tokens
from agentguard.policy.seed import register_agent
from agentguard.sdk.client import AgentGuardClient, ClientCredentialsAuth

ISSUER = "https://sso.example.test/realms/agentguard"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
CLIENT_ID = "returns-agent"


class FakeJWKS:
    def get_signing_key_from_jwt(self, _token):
        return jwt.PyJWK.from_dict({**jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True), "kid": "k1"})


def token(key=KEY, **overrides) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": "agentguard-gateway", "sub": "svc-1", "iat": now, "exp": now + 300,
              "azp": CLIENT_ID, "preferred_username": f"service-account-{CLIENT_ID}", **overrides}
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture()
def client(session: Session, monkeypatch):
    monkeypatch.setattr(settings, "agent_auth", "oidc")
    monkeypatch.setattr(settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(tokens, "_jwks_client", lambda url: FakeJWKS())
    register_agent(session, CLIENT_ID)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def call(client, headers) -> httpx.Response:
    return client.post("/v1/tool-calls", headers=headers, json={
        "tool_name": "read-ticket", "environment": "dev", "resource_tenant_id": "tenant-a",
        "target_resource": "ticket:T-1001", "args": {"ticket_id": "T-1001"}, "idempotency_key": str(uuid.uuid4()),
    })


def test_a_valid_service_account_token_identifies_the_agent(client):
    response = call(client, {"Authorization": f"Bearer {token()}"})
    assert response.status_code == 200 and response.json()["decision"] == "allow"


@pytest.mark.parametrize("bad", [
    {"exp": int(time.time()) - 10},                       # expired
    {"aud": "some-other-api"},                            # issued for another audience
    {"iss": "https://evil.example.test/realms/x"},        # another issuer
    {"preferred_username": "dana"},                       # a person's token, not the service account
    {"azp": None},                                        # no client id at all
])
def test_tokens_that_must_not_identify_an_agent(client, bad):
    assert call(client, {"Authorization": f"Bearer {token(**bad)}"}).status_code == 401


def test_a_token_signed_by_another_key_is_rejected(client):
    assert call(client, {"Authorization": f"Bearer {token(key=OTHER_KEY)}"}).status_code == 401


def test_the_dev_identity_header_counts_for_nothing(client):
    assert call(client, {"x-workload-identity": CLIENT_ID}).status_code == 401
    # Even alongside a valid token for a different, unregistered client.
    forged = token(azp="ghost-agent", preferred_username="service-account-ghost-agent")
    assert call(client, {"Authorization": f"Bearer {forged}", "x-workload-identity": CLIENT_ID}).status_code == 401


def test_sdk_fetches_reuses_and_refreshes_its_token():
    issued = []

    def idp(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/token") and b"grant_type=client_credentials" in request.content
        issued.append(1)
        return httpx.Response(200, json={"access_token": f"t{len(issued)}", "expires_in": 300})

    seen = []

    def gateway(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        assert "x-workload-identity" not in request.headers  # tokens replace the dev header
        if len(seen) == 3:
            return httpx.Response(401)  # revoked early: the client refreshes once and retries
        return httpx.Response(200, json={"decision": "allow"})

    auth = ClientCredentialsAuth("https://sso.example.test/token", CLIENT_ID, "s3cret",
                                 transport=httpx.MockTransport(idp))
    sdk = AgentGuardClient("https://gateway.example.test", CLIENT_ID, transport=httpx.MockTransport(gateway), auth=auth)
    for _ in range(3):
        sdk.call_tool("read-ticket", environment="dev", resource_tenant_id="tenant-a", target_resource="t",
                      args={}, idempotency_key=str(uuid.uuid4()))
    assert seen == ["Bearer t1", "Bearer t1", "Bearer t1", "Bearer t2"] and len(issued) == 2
