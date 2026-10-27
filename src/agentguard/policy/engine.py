"""Deterministic, versioned policy evaluation.

Policy is plain data (see docs/DATA_MODEL.md: `policies` + `policy_rules`),
not a separate policy-language runtime. Rules within a policy version are
evaluated in ascending `priority` order; the first matching rule wins.
There is always an implicit catch-all deny — a request that matches
nothing is denied, never allowed, per the fail-closed invariant in
CLAUDE.md.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from agentguard.models import Policy, PolicyRule

DECISIONS = {"allow", "deny", "approval_required"}


@dataclass(frozen=True)
class PolicyContext:
    tool_name: str
    risk_class: str
    environment: str
    tenant_id: str
    agent_risk_category: str
    args: dict[str, Any]


@dataclass(frozen=True)
class PolicyDecision:
    decision: str
    policy_version: int
    matched_rule_id: str | None
    required_role: str | None = None


def _arg_condition_matches(condition: dict[str, Any], args: dict[str, Any]) -> bool:
    field = condition.get("field")
    op = condition.get("op")
    value = condition.get("value")
    if field not in args:
        return False
    actual = args[field]
    if op == "gt":
        return actual > value
    if op == "gte":
        return actual >= value
    if op == "lt":
        return actual < value
    if op == "lte":
        return actual <= value
    if op == "eq":
        return actual == value
    raise ValueError(f"unknown arg condition operator: {op}")


def _rule_matches(match: dict[str, Any], ctx: PolicyContext) -> bool:
    tool = match.get("tool", "*")
    if tool != "*" and ctx.tool_name not in _as_list(tool):
        return False

    risk_classes = match.get("risk_class", "*")
    if risk_classes != "*" and ctx.risk_class not in _as_list(risk_classes):
        return False

    environments = match.get("environment", "*")
    if environments != "*" and ctx.environment not in _as_list(environments):
        return False

    tenants = match.get("tenant", "*")
    if tenants != "*" and ctx.tenant_id not in _as_list(tenants):
        return False

    agent_risk_categories = match.get("agent_risk_category", "*")
    if agent_risk_categories != "*" and ctx.agent_risk_category not in _as_list(
        agent_risk_categories
    ):
        return False

    arg_condition = match.get("arg")
    if arg_condition is not None and not _arg_condition_matches(arg_condition, ctx.args):  # noqa: SIM103
        return False

    return True


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


@dataclass(frozen=True)
class RuleSpec:
    """Storage-independent rule, so proposed (unpublished) policies can be
    evaluated with exactly the same code path as the live one."""

    id: str
    match: dict[str, Any]
    decision: str
    priority: int
    approval_role: str | None = None

    @classmethod
    def from_orm(cls, rule: PolicyRule) -> "RuleSpec":
        return cls(rule.id, rule.match or {}, rule.decision, rule.priority, rule.approval_role)


def evaluate_rules(rules: list[RuleSpec], policy_version: int, ctx: PolicyContext) -> PolicyDecision:
    """Pure function: no DB access, no side effects."""
    for rule in sorted(rules, key=lambda r: r.priority):
        if _rule_matches(rule.match, ctx):
            if rule.decision not in DECISIONS:
                raise ValueError(f"policy rule {rule.id} has invalid decision {rule.decision!r}")
            return PolicyDecision(
                decision=rule.decision,
                policy_version=policy_version,
                matched_rule_id=rule.id,
                required_role=rule.approval_role,
            )

    # Implicit catch-all: fail closed.
    return PolicyDecision(decision="deny", policy_version=policy_version, matched_rule_id=None)


def load_rules(session: Session, version: int, *, scope: str = "global") -> list[RuleSpec] | None:
    policy = (
        session.query(Policy)
        .filter(Policy.scope == scope, Policy.version == version)
        .one_or_none()
    )
    return None if policy is None else [RuleSpec.from_orm(r) for r in policy.rules]


def evaluate(session: Session, ctx: PolicyContext, *, scope: str = "global") -> PolicyDecision:
    policy = (
        session.query(Policy)
        .filter(Policy.scope == scope, Policy.is_active.is_(True))
        .one_or_none()
    )
    if policy is None:
        # Fail closed: no active policy means we cannot evaluate at all.
        return PolicyDecision(decision="deny", policy_version=-1, matched_rule_id=None)

    return evaluate_rules([RuleSpec.from_orm(r) for r in policy.rules], policy.version, ctx)
