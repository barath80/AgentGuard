from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolCallRequest(BaseModel):
    """What an agent sends. Note what's deliberately absent: no
    `agent_id` or `tenant_id` field. Those come only from the verified
    `x-workload-identity` header — see identity/service.py.
    """

    tool_name: str
    environment: str
    resource_tenant_id: str = Field(
        description="Tenant the target resource belongs to, used for a tenant-isolation check "
        "against the caller's own verified tenant."
    )
    target_resource: str
    args: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    workflow_id: str | None = Field(
        default=None, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$",
        description="Optional label grouping this call with the agent's other steps of one task. "
        "Grouping only: it grants nothing, and it's scoped to the calling agent.",
    )


class ToolCallResponse(BaseModel):
    execution_id: str
    decision: Literal["allow", "deny", "approval_required"]
    outcome: str
    policy_version: int
    approval_id: str | None = None
    result: dict[str, Any] | None = None
    reason: str | None = None


class ExecuteApprovedRequest(BaseModel):
    pass


class ExecutionStatusResponse(BaseModel):
    execution_id: str
    decision: str
    outcome: str
    trace_complete: bool
    result: dict[str, Any] | None = None
