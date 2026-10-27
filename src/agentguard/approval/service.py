"""Approval state machine.

An approval is bound to the exact (tool, resource, arguments, policy
version) it was issued for via `action_hash`, expires, and can be
consumed exactly once via an atomic, status-guarded UPDATE — not a
read-then-write, which would be racy and replayable.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from agentguard.config import settings
from agentguard.models import Approval


class ApprovalError(Exception):
    pass


def compute_action_hash(
    tool_name: str, resource: str, args: dict[str, Any], policy_version: int
) -> str:
    canonical = json.dumps(
        {"tool": tool_name, "resource": resource, "args": args, "policy_version": policy_version},
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_pending_approval(
    session: Session,
    *,
    execution_id: str,
    tenant_id: str,
    action_hash: str,
    resource: str,
    policy_version: int,
    required_role: str | None,
    expiry_seconds: int | None = None,
) -> Approval:
    ttl = expiry_seconds or settings.approval_default_expiry_seconds
    approval = Approval(
        execution_id=execution_id,
        tenant_id=tenant_id,
        action_hash=action_hash,
        resource=resource,
        policy_version=policy_version,
        required_role=required_role,
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
    )
    session.add(approval)
    session.commit()
    return approval


@dataclass(frozen=True)
class ApproverIdentity:
    name: str
    role: str
    tenant_id: str


def decide(
    session: Session,
    approval_id: str,
    *,
    approve: bool,
    approver: ApproverIdentity,
) -> Approval:
    approval = session.get(Approval, approval_id)
    if approval is None:
        raise ApprovalError("approval not found")
    if approval.status != "pending":
        raise ApprovalError(f"approval is not pending (status={approval.status})")
    if _is_expired(approval):
        approval.status = "expired"
        session.commit()
        raise ApprovalError("approval has expired")
    if approval.required_role and approver.role != approval.required_role:
        raise ApprovalError("approver does not hold the required role")
    if approver.tenant_id != approval.tenant_id:
        raise ApprovalError("approver does not have authority over this tenant")

    approval.status = "approved" if approve else "denied"
    approval.approver = approver.name
    approval.decided_at = datetime.now(UTC)
    session.commit()
    return approval


def _is_expired(approval: Approval) -> bool:
    expires_at = approval.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return datetime.now(UTC) > expires_at


def validate_for_execution(
    session: Session, approval: Approval, *, action_hash: str, current_policy_version: int
) -> None:
    """Re-validate an approval right before it is consumed.

    Binds execution to the *current* active policy, not just whatever
    was active when the approval was issued (edge case #3 in
    docs/THREAT_MODEL.md).
    """
    if approval.status != "approved":
        raise ApprovalError(f"approval is not approved (status={approval.status})")
    if _is_expired(approval):
        raise ApprovalError("approval has expired")
    if approval.action_hash != action_hash:
        raise ApprovalError("action hash mismatch — request changed since approval was granted")
    if approval.policy_version != current_policy_version:
        raise ApprovalError("policy has changed since approval was granted — re-approval required")


def consume(session: Session, approval_id: str) -> bool:
    """Atomically mark an approval consumed exactly once.

    Returns False (never raises) if the approval was already consumed
    or isn't in the `approved` state — callers must treat that as a
    hard failure, not retry the execution.
    """
    result = session.execute(
        update(Approval)
        .where(Approval.id == approval_id, Approval.status == "approved")
        .values(status="consumed", consumed_at=datetime.now(UTC))
    )
    session.commit()
    return result.rowcount == 1
