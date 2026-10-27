"""Thin client adapter agents use to call through the gateway.

Deliberately dumb: it does not evaluate policy, cache decisions, or
retry on its own. Nothing here is trusted to self-enforce anything —
the gateway and broker are the only places a decision is final (see
CLAUDE.md).
"""

from typing import Any

import httpx


class AgentGuardClient:
    def __init__(self, base_url: str, workload_identity: str, transport: httpx.BaseTransport | None = None):
        self._workload_identity = workload_identity
        self._http = httpx.Client(
            base_url=base_url,
            headers={"x-workload-identity": workload_identity},
            transport=transport,
        )

    def call_tool(
        self,
        tool_name: str,
        *,
        environment: str,
        resource_tenant_id: str,
        target_resource: str,
        args: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        response = self._http.post(
            "/v1/tool-calls",
            json={
                "tool_name": tool_name,
                "environment": environment,
                "resource_tenant_id": resource_tenant_id,
                "target_resource": target_resource,
                "args": args,
                "idempotency_key": idempotency_key,
            },
        )
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
