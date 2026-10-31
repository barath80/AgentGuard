"""Thin client adapter agents use to call through the gateway.

Deliberately dumb: it does not evaluate policy, cache decisions, or
retry on its own. Nothing here is trusted to self-enforce anything —
the gateway and broker are the only places a decision is final (see
CLAUDE.md).
"""

import os
import threading
import time
from typing import Any

import httpx


class ClientCredentialsAuth(httpx.Auth):
    """OAuth 2.0 client credentials: fetch a short-lived token from the identity provider, reuse
    it until shortly before it expires, and refresh once if the gateway rejects it early.
    The client secret only ever goes to the token endpoint, never to the gateway."""

    REFRESH_MARGIN_SECONDS = 30

    def __init__(self, token_url: str, client_id: str, client_secret: str,
                 transport: httpx.BaseTransport | None = None):
        self._token_url, self._client_id, self._client_secret = token_url, client_id, client_secret
        self._transport = transport
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def token(self, *, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or time.monotonic() >= self._expires_at - self.REFRESH_MARGIN_SECONDS:
                with httpx.Client(transport=self._transport, timeout=10) as http:
                    response = http.post(self._token_url, data={"grant_type": "client_credentials"},
                                         auth=(self._client_id, self._client_secret))
                response.raise_for_status()
                body = response.json()
                self._token = body["access_token"]
                self._expires_at = time.monotonic() + float(body.get("expires_in", 60))
            return self._token

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {self.token()}"
        response = yield request
        if response.status_code == 401:
            request.headers["Authorization"] = f"Bearer {self.token(force=True)}"
            yield request


class AgentGuardClient:
    def __init__(self, base_url: str, workload_identity: str, transport: httpx.BaseTransport | None = None,
                 auth: httpx.Auth | None = None):
        """With `auth` (production), the agent proves who it is with a token; without it (dev),
        a trusted proxy is assumed and the identity travels in x-workload-identity."""
        self._workload_identity = workload_identity
        self._http = httpx.Client(
            base_url=base_url,
            headers={} if auth else {"x-workload-identity": workload_identity},
            transport=transport,
            auth=auth,
        )

    @classmethod
    def from_env(cls, base_url: str, workload_identity: str) -> "AgentGuardClient":
        """Token authentication when AGENTGUARD_TOKEN_URL and AGENTGUARD_CLIENT_SECRET are set
        (the client id defaults to the workload identity); otherwise the dev header."""
        token_url, secret = os.environ.get("AGENTGUARD_TOKEN_URL"), os.environ.get("AGENTGUARD_CLIENT_SECRET")
        if token_url and secret:
            client_id = os.environ.get("AGENTGUARD_CLIENT_ID", workload_identity)
            return cls(base_url, client_id, auth=ClientCredentialsAuth(token_url, client_id, secret))
        return cls(base_url, workload_identity)

    def call_tool(
        self,
        tool_name: str,
        *,
        environment: str,
        resource_tenant_id: str,
        target_resource: str,
        args: dict[str, Any],
        idempotency_key: str,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "tool_name": tool_name,
            "environment": environment,
            "resource_tenant_id": resource_tenant_id,
            "target_resource": target_resource,
            "args": args,
            "idempotency_key": idempotency_key,
        }
        if workflow_id:
            body["workflow_id"] = workflow_id
        response = self._http.post("/v1/tool-calls", json=body)
        response.raise_for_status()
        return response.json()

    def execute_approved(self, execution_id: str) -> dict[str, Any]:
        response = self._http.post(f"/v1/tool-calls/{execution_id}/execute")
        response.raise_for_status()
        return response.json()

    def get_execution(self, execution_id: str) -> dict[str, Any]:
        response = self._http.get(f"/v1/executions/{execution_id}")
        response.raise_for_status()
        return response.json()
