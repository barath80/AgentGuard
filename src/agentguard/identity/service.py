"""Identity resolution.

Phase 1 stand-in for real OIDC/mTLS verification: the caller's workload
identity arrives as a header set by a trusted transport layer (e.g. a
sidecar or API gateway that terminates mTLS / verifies a short-lived
token upstream of this service). This module NEVER reads agent_id or
tenant_id out of the request body — only out of that verified header.

Replacing the header check with real OIDC/mTLS verification in a later
phase should not require touching any caller of `resolve_identity`.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from agentguard.models import Agent

WORKLOAD_IDENTITY_HEADER = "x-workload-identity"


class IdentityError(Exception):
    pass


@dataclass(frozen=True)
class ResolvedIdentity:
    agent_id: str
    tenant_id: str
    risk_category: str
    approved_environments: list[str]
    status: str


def resolve_identity(session: Session, workload_identity_ref: str | None) -> ResolvedIdentity:
    if not workload_identity_ref:
        raise IdentityError("missing verified workload identity")

    agent = (
        session.query(Agent)
        .filter(Agent.workload_identity_ref == workload_identity_ref)
        .one_or_none()
    )
    if agent is None:
        raise IdentityError("unknown workload identity")
    if agent.status != "active":
        raise IdentityError(f"agent is not active (status={agent.status})")

    return ResolvedIdentity(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        risk_category=agent.risk_category,
        approved_environments=list(agent.approved_environments or []),
        status=agent.status,
    )
