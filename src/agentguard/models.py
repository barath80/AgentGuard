"""SQLAlchemy ORM models matching docs/DATA_MODEL.md.

Risk class, decision, status etc. are plain strings validated at the
Pydantic layer (see gateway/schemas.py) rather than DB-level enums, to
keep the schema portable between SQLite (tests) and Postgres (runtime).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentguard.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    owner: Mapped[str] = mapped_column(String, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    workload_identity_ref: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    approved_environments: Mapped[list] = mapped_column(JSON, default=list)
    risk_category: Mapped[str] = mapped_column(String, nullable=False, default="medium")
    rate_limit_per_minute: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default="60"
    )
    # Max total of tools' `spend_arg` values per rolling 24h; None means no budget.
    daily_spend_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Tool(Base):
    __tablename__ = "tools"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    schema_version: Mapped[str] = mapped_column(String, nullable=False, default="1.0")
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    risk_class: Mapped[str] = mapped_column(String, nullable=False)
    target_scope: Mapped[str] = mapped_column(String, nullable=False)
    executor_ref: Mapped[str] = mapped_column(String, nullable=False)
    # Argument that counts against an agent's spend budget (e.g. "amount"), if any.
    spend_arg: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    scope: Mapped[str] = mapped_column(String, nullable=False, default="global")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)

    rules: Mapped[list["PolicyRule"]] = relationship(
        back_populates="policy", order_by="PolicyRule.priority"
    )


class PolicyRule(Base):
    __tablename__ = "policy_rules"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    policy_id: Mapped[str] = mapped_column(ForeignKey("policies.id"), nullable=False)
    match: Mapped[dict] = mapped_column(JSON, default=dict)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    approval_role: Mapped[str | None] = mapped_column(String, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    policy: Mapped[Policy] = relationship(back_populates="rules")


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id"), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    action_hash: Mapped[str] = mapped_column(String, nullable=False)
    resource: Mapped[str] = mapped_column(String, nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    required_role: Mapped[str | None] = mapped_column(String, nullable=True)
    approver: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Execution(Base):
    __tablename__ = "executions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    agent_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    tool_id: Mapped[str] = mapped_column(String, nullable=False)
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    environment: Mapped[str] = mapped_column(String, nullable=False)
    target_resource: Mapped[str] = mapped_column(String, nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    # Not a DB-level FK on purpose: approvals.execution_id already references this
    # table, and a FK in both directions creates an unresolvable creation-order
    # cycle. Validated at the application layer instead (gateway/service.py).
    approval_id: Mapped[str | None] = mapped_column(String, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String, nullable=False, index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str] = mapped_column(String, nullable=False, default="not_attempted")
    # Why a call was denied or didn't complete normally. Runtime-control denials are
    # prefixed "control:" so the circuit breaker doesn't count its own denials and
    # lock an agent out forever.
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    safe_request_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    safe_result_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    trace_complete: Mapped[bool] = mapped_column(Boolean, default=False)


class RequestPayload(Base):
    """Holds the actual tool-call arguments, separate from `executions`.

    `executions.safe_request_ref` stores only a hash of this payload.
    Keeping the two apart means the audit trail (which must stay
    readable broadly for trace completeness) never carries raw request
    data; access to this table can be restricted independently. In a
    real deployment this would likely be an external encrypted store
    rather than another table in the same database.
    """

    __tablename__ = "request_payloads"

    execution_id: Mapped[str] = mapped_column(
        ForeignKey("executions.id"), primary_key=True
    )
    args: Mapped[dict] = mapped_column(JSON, default=dict)


class AuditEvent(Base):
    """Append-only, step-level log. Never updated or deleted once written."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EvaluationCase(Base):
    __tablename__ = "evaluation_cases"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    scenario: Mapped[dict] = mapped_column(JSON, default=dict)
    expected_decision: Mapped[str] = mapped_column(String, nullable=False)
    expected_outcome: Mapped[str] = mapped_column(String, nullable=False, default="")
