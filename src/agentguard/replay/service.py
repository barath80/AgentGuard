"""Deterministic replay and counterfactual policy simulation over recorded traces.

Both operations are read-only: they never add, update, or commit rows,
and they never import the broker (see replay/dry_run.py).

- `simulate`: evaluate a *proposed* policy against recorded requests and
  report which decisions would change, including what that does to task
  success (legitimate work newly blocked) and to security (previously
  blocked work newly allowed).
- `replay`: re-evaluate each recorded request under the policy version it
  was originally decided by, confirm the decision is reproduced, and run
  the tool step through a dry-run executor.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from agentguard.models import Agent, Execution, RequestPayload, Tool
from agentguard.policy.engine import (
    DECISIONS,
    PolicyContext,
    RuleSpec,
    evaluate_rules,
    load_rules,
    validate_match,
)
from agentguard.replay.dry_run import DRY_RUN_EXECUTORS

PRE_POLICY_VERSION = -1  # identity-scope denials made before any policy ran


@dataclass(frozen=True)
class RecordedCall:
    execution_id: str
    tool_name: str
    executor_ref: str
    ctx: PolicyContext
    recorded_decision: str
    recorded_outcome: str
    policy_version: int

    @property
    def pre_policy(self) -> bool:
        return self.policy_version == PRE_POLICY_VERSION


def load_recorded_calls(session: Session) -> list[RecordedCall]:
    """Rebuild each recorded request's policy context from the trace.

    The agent's risk category and the tool's risk class come from the
    snapshot taken at request time; only rows recorded before snapshots
    existed fall back to the registry as it is now.
    """
    calls = []
    for execution in session.query(Execution).order_by(Execution.requested_at).all():
        tool = session.get(Tool, execution.tool_id)
        agent = session.get(Agent, execution.agent_id)
        payload = session.get(RequestPayload, execution.id)
        calls.append(
            RecordedCall(
                execution_id=execution.id,
                tool_name=execution.tool_name,
                executor_ref=tool.executor_ref if tool else "",
                ctx=PolicyContext(
                    tool_name=execution.tool_name,
                    risk_class=execution.tool_risk_class or (tool.risk_class if tool else "unknown"),
                    environment=execution.environment,
                    tenant_id=execution.tenant_id,
                    agent_risk_category=execution.agent_risk_category or (agent.risk_category if agent else "unknown"),
                    args=payload.args if payload else {},
                ),
                recorded_decision=execution.decision,
                recorded_outcome=execution.outcome,
                policy_version=execution.policy_version,
            )
        )
    return calls


# --- Counterfactual simulation ----------------------------------------------


@dataclass(frozen=True)
class DecisionChange:
    execution_id: str
    tool_name: str
    recorded: str
    simulated: str


@dataclass
class SimulationReport:
    proposed_version: int
    evaluated: int = 0  # recorded calls that went through policy
    skipped_pre_policy: int = 0  # identity-scope denials; a policy change can't affect them
    recorded_allows: int = 0
    simulated_allows: int = 0
    changes: list[DecisionChange] = field(default_factory=list)

    @property
    def transitions(self) -> dict[str, int]:
        return dict(Counter(f"{c.recorded}->{c.simulated}" for c in self.changes))

    @property
    def newly_allowed(self) -> list[DecisionChange]:
        """Security loosening: something that was blocked or gated would now run unattended."""
        return [c for c in self.changes if c.simulated == "allow"]

    @property
    def newly_restricted(self) -> list[DecisionChange]:
        """Task-success regression: work that ran unattended would now be denied or gated."""
        return [c for c in self.changes if c.recorded == "allow"]

    @property
    def unattended_rate_before(self) -> float:
        """Share of calls that completed without a human — the task-success proxy."""
        return self.recorded_allows / self.evaluated if self.evaluated else 0.0

    @property
    def unattended_rate_after(self) -> float:
        return self.simulated_allows / self.evaluated if self.evaluated else 0.0


def simulate(session: Session, proposed_rules: list[RuleSpec], proposed_version: int) -> SimulationReport:
    report = SimulationReport(proposed_version=proposed_version)
    for call in load_recorded_calls(session):
        if call.pre_policy:
            report.skipped_pre_policy += 1
            continue
        report.evaluated += 1
        simulated = evaluate_rules(proposed_rules, proposed_version, call.ctx).decision
        report.recorded_allows += call.recorded_decision == "allow"
        report.simulated_allows += simulated == "allow"
        if simulated != call.recorded_decision:
            report.changes.append(
                DecisionChange(call.execution_id, call.tool_name, call.recorded_decision, simulated)
            )
    return report


def load_policy_file(path: Path) -> tuple[int, list[RuleSpec]]:
    return parse_policy(json.loads(path.read_text()))


def parse_policy(data: dict[str, Any]) -> tuple[int, list[RuleSpec]]:
    """Parse a proposed policy: {"version": int, "rules": [{match, decision, priority, approval_role?}]}."""
    version = int(data["version"])
    rules = []
    for i, raw in enumerate(data["rules"]):
        try:
            validate_match(raw.get("match", {}))
        except (TypeError, ValueError) as exc:
            raise type(exc)(f"rule {i}: {exc}") from exc
        if raw["decision"] not in DECISIONS:
            raise ValueError(f"rule {i}: invalid decision {raw['decision']!r}")
        rules.append(
            RuleSpec(
                id=raw.get("id", f"proposed-{version}-{i}"),
                match=raw.get("match", {}),
                decision=raw["decision"],
                priority=int(raw.get("priority", 100)),
                approval_role=raw.get("approval_role"),
            )
        )
    return version, rules


# --- Deterministic replay ----------------------------------------------------


@dataclass(frozen=True)
class ReplayResult:
    execution_id: str
    tool_name: str
    recorded_decision: str
    replayed_decision: str | None  # None if the recorded policy version no longer exists
    dry_run_output: dict[str, Any] | None

    @property
    def reproduced(self) -> bool:
        return self.replayed_decision == self.recorded_decision


@dataclass
class ReplayReport:
    results: list[ReplayResult] = field(default_factory=list)
    skipped_pre_policy: int = 0

    @property
    def mismatches(self) -> list[ReplayResult]:
        return [r for r in self.results if not r.reproduced]


def replay(session: Session) -> ReplayReport:
    report = ReplayReport()
    rules_by_version: dict[int, list[RuleSpec] | None] = {}

    for call in load_recorded_calls(session):
        if call.pre_policy:
            report.skipped_pre_policy += 1
            continue
        if call.policy_version not in rules_by_version:
            rules_by_version[call.policy_version] = load_rules(session, call.policy_version)
        rules = rules_by_version[call.policy_version]

        replayed = None if rules is None else evaluate_rules(rules, call.policy_version, call.ctx).decision
        dry_run_output = None
        if replayed == "allow" or call.recorded_outcome == "success":
            executor = DRY_RUN_EXECUTORS.get(call.executor_ref)
            dry_run_output = executor(call.ctx.args) if executor else {"dry_run": True, "error": "no dry-run adapter"}

        report.results.append(
            ReplayResult(call.execution_id, call.tool_name, call.recorded_decision, replayed, dry_run_output)
        )
    return report
