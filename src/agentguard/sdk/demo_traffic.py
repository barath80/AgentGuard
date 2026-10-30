"""Scripted traffic for the operator console: routine support work driven by the
helpdesk's real ticket data, plus attacks the gateway should stop.

    python -m agentguard.sdk.demo_traffic                  # one round
    python -m agentguard.sdk.demo_traffic --rounds 5

Every call goes through the real gateway path (identity, controls, policy,
broker, target), so what the console shows is what actually happened. The
well-behaved agents read a ticket before acting on it and refund within
what the customer paid, using the ticket data the helpdesk returns; refunds
above the policy threshold wait for a human in the console. For an agent
that decides for itself, see agentguard.agents.support_agent.

The only direct database write is registering two extra synthetic agents if
they're missing (same as `controls.cli register`).
"""

import argparse
import random
import uuid

from agentguard.config import settings
from agentguard.db import SessionLocal
from agentguard.models import Agent
from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF, REFUND_THRESHOLD, register_agent
from agentguard.sdk.client import AgentGuardClient

BILLING_AGENT = "billing-agent@dev-workload-pool"
OPS_AGENT = "ops-agent@dev-workload-pool"
# Plays an agent whose prompt has been hijacked; its circuit breaker opens, the rest of the fleet keeps working.
INTAKE_AGENT = "intake-agent@dev-workload-pool"
REFUND_SUBJECTS = ("Charged twice", "damaged", "cancelled but still charged", "Wrong size")


def ensure_demo_agents() -> None:
    if settings.environment == "production":
        raise SystemExit("refusing to register synthetic agents in production")
    with SessionLocal() as session:
        existing = {ref for (ref,) in session.query(Agent.workload_identity_ref).all()}
        if BILLING_AGENT not in existing:
            register_agent(session, BILLING_AGENT, owner="billing-eng", tenant_id="tenant-b", daily_spend_limit=2000)
        if INTAKE_AGENT not in existing:
            register_agent(session, INTAKE_AGENT, owner="support-eng", daily_spend_limit=500)
        if OPS_AGENT not in existing:
            # Approved for prod, so prod write attempts reach policy and are denied there.
            register_agent(
                session, OPS_AGENT, owner="platform-eng", approved_environments=["dev", "staging", "prod"],
                risk_category="high", daily_spend_limit=None,
            )


def _call(client: AgentGuardClient, tool: str, args: dict, *, tenant="tenant-a", env="dev", target=None) -> dict:
    return client.call_tool(
        tool,
        environment=env,
        resource_tenant_id=tenant,
        target_resource=target or f"ticket:{args.get('ticket_id', 'new')}",
        args=args,
        idempotency_key=str(uuid.uuid4()),
    )


def work_tickets(client: AgentGuardClient, tenant: str, candidates: list[str], rng: random.Random, record) -> None:
    """Read tickets; refund the refundable ones within what's left on the order."""
    for ticket_id in candidates:
        read = _call(client, "read-ticket", {"ticket_id": ticket_id}, tenant=tenant)
        record(f"read {ticket_id}", read)
        ticket = read.get("result") or {}
        if not ticket.get("found") or not any(s.lower() in ticket.get("subject", "").lower() for s in REFUND_SUBJECTS):
            continue
        remaining = round(float(ticket.get("order_amount", 0)) - float(ticket.get("refunded_total", 0)), 2)
        if remaining <= 0:
            continue
        # Full refunds above the threshold go to a human; smaller ones are partial goodwill refunds.
        amount = remaining if remaining > REFUND_THRESHOLD else round(remaining * rng.choice([0.5, 1.0]), 2)
        refund = _call(client, "issue-refund", {"ticket_id": ticket_id, "amount": amount, "note": ticket["subject"]},
                       tenant=tenant)
        record(f"refund {amount:g} on {ticket_id}", refund)


def run_round(base_url: str, rng: random.Random) -> list[tuple[str, dict]]:
    support = AgentGuardClient(base_url, MOCK_AGENT_WORKLOAD_REF)
    billing = AgentGuardClient(base_url, BILLING_AGENT)
    ops = AgentGuardClient(base_url, OPS_AGENT)
    hijacked = AgentGuardClient(base_url, INTAKE_AGENT)
    results: list[tuple[str, dict]] = []

    def record(label: str, response: dict) -> None:
        results.append((label, response))

    # Routine work on each tenant's own tickets (the seed numbers tenant-a's tickets first).
    work_tickets(support, "tenant-a", [f"T-{n}" for n in rng.sample(range(1001, 1050), 4)], rng, record)
    work_tickets(billing, "tenant-b", [f"T-{n}" for n in rng.sample(range(1055, 1072), 2)], rng, record)
    record("create follow-up ticket", _call(support, "create-ticket", {
        "subject": "Follow up with logistics on late delivery", "body": "Opened by support while working the queue.",
    }))

    # A hijacked agent following injected instructions; the gateway (or the target) stops each one.
    record("claims another tenant's resource",
           _call(hijacked, "read-ticket", {"ticket_id": "T-1060"}, tenant="tenant-b"))
    record("lies about the tenant of a ticket",
           _call(hijacked, "read-ticket", {"ticket_id": "T-1060"}, target="ticket:T-1060"))
    record("unexpected high-risk argument",
           _call(hijacked, "issue-refund", {"ticket_id": "T-1001", "amount": 40, "override_policy": True}))
    record("refund on unapproved env", _call(hijacked, "issue-refund", {"ticket_id": "T-1001", "amount": 10}, env="prod"))
    record("prod refund (policy deny)",
           _call(ops, "issue-refund", {"ticket_id": "T-1002", "amount": 50}, env="prod", target="billing:prod"))
    if rng.random() < 0.5:
        record("refund without reading the ticket",
               _call(ops, "issue-refund", {"ticket_id": f"T-{rng.randint(1001, 1049)}", "amount": 15}))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send scripted traffic through a running gateway")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)

    ensure_demo_agents()
    rng = random.Random(args.seed)
    for i in range(args.rounds):
        for label, response in run_round(args.base_url, rng):
            detail = response.get("reason") or ""
            print(f"[round {i + 1}] {label:<36} {response['decision']:<18} {response['outcome']:<14} {detail[:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
