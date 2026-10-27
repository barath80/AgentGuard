"""Direct-access vs. gateway-enforced comparison (spec step 30).

Models the realistic baseline for "no AgentGuard": an agent that has
simply been handed the target system's credential directly, as is
common without a gateway product, and calls it with no identity
resolution, policy evaluation, approval gating, rate limiting, or
schema checking in front of it. Compares that against the same calls
made through the full gateway.

This is a transparency tool, not a benchmark to win: it reports the
latency AgentGuard adds and is just as honest about it as about the
security it buys. Both are real trade-offs, not something to spin.
"""

import statistics
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from agentguard.broker.service import simulate_direct_call
from agentguard.gateway import service as gateway_service
from agentguard.gateway.schemas import ToolCallRequest
from agentguard.identity.service import resolve_identity
from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF, REFUND_THRESHOLD, register_agent


def gateway_call(tool_name: str, args: dict[str, Any], idempotency_key: str, **overrides) -> ToolCallRequest:
    fields = {
        "tool_name": tool_name,
        "environment": "dev",
        "resource_tenant_id": "tenant-a",
        "target_resource": "ticket:T-1001",
        "args": args,
        "idempotency_key": idempotency_key,
        **overrides,
    }
    return ToolCallRequest(**fields)


def direct_call(executor_ref: str, args: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
    """What an agent could do today if it just held the credential, bypassing AgentGuard entirely."""
    return simulate_direct_call(executor_ref, args, idempotency_key)


# --- Latency -------------------------------------------------------------


@dataclass(frozen=True)
class LatencyResult:
    label: str
    direct_ms: dict[str, float]
    gateway_ms: dict[str, float]

    @property
    def overhead_ms_p95(self) -> float:
        return self.gateway_ms["p95"] - self.direct_ms["p95"]


def _percentiles(samples_ms: list[float]) -> dict[str, float]:
    return {
        "p50": statistics.median(samples_ms),
        "p95": statistics.quantiles(samples_ms, n=20)[-1] if len(samples_ms) >= 2 else samples_ms[0],
    }


def measure_latency(session: Session, identity, iterations: int = 30) -> list[LatencyResult]:
    results = []
    scenarios = [
        ("read-ticket", "mock.read_ticket", {"ticket_id": "T-1001"}),
        ("issue-refund", "mock.issue_refund", {"ticket_id": "T-1001", "amount": 1}),
    ]
    for tool_name, executor_ref, args in scenarios:
        direct_samples, gateway_samples = [], []
        for i in range(iterations):
            key = f"lat-direct-{tool_name}-{i}"
            started = time.perf_counter()
            direct_call(executor_ref, args, key)
            direct_samples.append((time.perf_counter() - started) * 1000)

            started = time.perf_counter()
            gateway_service.submit_tool_call(
                session, gateway_call(tool_name, args, idempotency_key=f"lat-gw-{tool_name}-{i}"), identity
            )
            gateway_samples.append((time.perf_counter() - started) * 1000)
        results.append(LatencyResult(tool_name, _percentiles(direct_samples), _percentiles(gateway_samples)))
    return results


# --- Security --------------------------------------------------------------


@dataclass(frozen=True)
class SecurityScenario:
    name: str
    direct_blocked: bool
    gateway_blocked: bool
    detail: str


def run_security_scenarios(session: Session) -> list[SecurityScenario]:
    scenarios = []

    # 1. Tenant isolation: the target system has no concept of tenant at all.
    direct_result = direct_call("mock.read_ticket", {"ticket_id": "T-1001"}, "sec-1-direct")
    agent = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)  # belongs to tenant-a
    gw = gateway_service.submit_tool_call(
        session, gateway_call("read-ticket", {"ticket_id": "T-1001"}, "sec-1-gw", resource_tenant_id="tenant-b"), agent
    )
    scenarios.append(SecurityScenario(
        "cross_tenant_read",
        direct_blocked=False,  # direct access has no tenant concept at all to block on
        gateway_blocked=gw.decision == "deny",
        detail=f"direct read returned ticket {direct_result.get('ticket_id')} with no tenant check at all; "
               "the gateway's tenant check is the only thing stopping cross-tenant access",
    ))

    # 2. Production write-high access without any environment gate at the target.
    direct_result = direct_call("mock.issue_refund", {"ticket_id": "T-1001", "amount": 50}, "sec-2-direct")
    other_agent = register_and_resolve(session, approved_environments=["prod"])
    gw = gateway_service.submit_tool_call(
        session, gateway_call("issue-refund", {"ticket_id": "T-1001", "amount": 50}, "sec-2-gw", environment="prod"),
        other_agent,
    )
    scenarios.append(SecurityScenario(
        "prod_write_high_access",
        direct_blocked=False,
        gateway_blocked=gw.decision == "deny",
        detail="target system executes refunds in any environment; only policy restricts prod",
    ))

    # 3. Large refund executed immediately, with no approval step, at the target.
    direct_result = direct_call(
        "mock.issue_refund", {"ticket_id": "T-1001", "amount": REFUND_THRESHOLD + 5000}, "sec-3-direct"
    )
    gw = gateway_service.submit_tool_call(
        session,
        gateway_call("issue-refund", {"ticket_id": "T-1001", "amount": REFUND_THRESHOLD + 5000}, "sec-3-gw"),
        agent,
    )
    scenarios.append(SecurityScenario(
        "large_refund_without_approval",
        direct_blocked=False,
        gateway_blocked=gw.decision != "allow",  # held for approval, not executed unattended
        detail=f"direct refund of {direct_result['amount']} executed instantly; gateway requires human approval",
    ))

    # 4. Call-rate spam: the target has no concept of a per-caller rate limit.
    spam_agent = register_and_resolve(session, rate_limit_per_minute=5)
    direct_allowed = sum(
        1 for i in range(10)
        if direct_call("mock.read_ticket", {"ticket_id": "T-1001"}, f"sec-4-direct-{i}")
    )
    gateway_allowed = sum(
        1 for i in range(10)
        if gateway_service.submit_tool_call(
            session, gateway_call("read-ticket", {"ticket_id": "T-1001"}, f"sec-4-gw-{i}"), spam_agent
        ).decision == "allow"
    )
    scenarios.append(SecurityScenario(
        "unbounded_call_rate",
        direct_blocked=False,
        gateway_blocked=gateway_allowed < 10,
        detail=f"direct: {direct_allowed}/10 calls succeeded; gateway: {gateway_allowed}/10 (limit 5/min)",
    ))

    # 5. Unexpected argument on a high-risk tool: the target executor doesn't validate it either.
    direct_result = direct_call(
        "mock.issue_refund", {"ticket_id": "T-1001", "amount": 5, "admin_override": True}, "sec-5-direct"
    )
    gw = gateway_service.submit_tool_call(
        session,
        gateway_call("issue-refund", {"ticket_id": "T-1001", "amount": 5, "admin_override": True}, "sec-5-gw"),
        agent,
    )
    scenarios.append(SecurityScenario(
        "schema_drift_extra_argument",
        direct_blocked=False,
        gateway_blocked=gw.decision == "deny",
        detail="executor silently accepts the extra field; the gateway rejects it by schema",
    ))

    return scenarios


def register_and_resolve(session: Session, **overrides):
    ref = f"compare-{uuid.uuid4().hex[:8]}@dev-workload-pool"
    register_agent(session, ref, **overrides)
    return resolve_identity(session, ref)


@dataclass
class ComparisonReport:
    latency: list[LatencyResult] = field(default_factory=list)
    security: list[SecurityScenario] = field(default_factory=list)

    @property
    def security_delta(self) -> int:
        """Count of scenarios where direct access succeeded but the gateway blocked it."""
        return sum(1 for s in self.security if not s.direct_blocked and s.gateway_blocked)


def run(session: Session) -> ComparisonReport:
    identity = resolve_identity(session, MOCK_AGENT_WORKLOAD_REF)
    return ComparisonReport(
        latency=measure_latency(session, identity),
        security=run_security_scenarios(session),
    )
