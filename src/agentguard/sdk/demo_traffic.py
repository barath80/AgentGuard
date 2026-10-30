"""Scripted traffic for the operator console: routine support work driven by the
helpdesk's real ticket data, plus attacks the gateway should stop.

    python -m agentguard.sdk.demo_traffic                  # one round
    python -m agentguard.sdk.demo_traffic --rounds 5
    python -m agentguard.sdk.demo_traffic --rounds 0 --interval 60 --attack-rate 0.05   # run as a fleet
    python -m agentguard.sdk.demo_traffic --rounds 0 --interval 15 --fleet-size 3 \\
        --attack-rate 0.02 --stealth-rate 0.15 --labels attacks.jsonl                    # training data

The fleet forms keep agents working until stopped, which is the history the
anomaly model (agentguard.ml) learns from. Two kinds of attack:

- **blocked**: a hijacked intake agent and an ops agent try cross-tenant reads,
  unexpected arguments and prod writes; the gateway denies them.
- **stealth**: a randomly chosen fleet agent, for one round, does something each
  call of which policy *allows*: enumerating tickets, draining small refunds
  without reading the ticket, or a couple of probing calls. These are what a
  behavioural model is for, since no single call breaks a rule.

With `--labels`, every attack call is written to a JSONL file (execution id,
agent, attack type, time). That's ground truth for evaluating detectors; it
never goes to the gateway, which must not know.

Every call goes through the real gateway path (identity, controls, policy,
broker, target), so what the console shows is what actually happened. The
only direct database write is registering the synthetic agents if they're
missing (same as `controls.cli register`).
"""

import argparse
import itertools
import json
import random
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from agentguard.config import settings
from agentguard.db import SessionLocal
from agentguard.models import Agent
from agentguard.policy.seed import MOCK_AGENT_WORKLOAD_REF, REFUND_THRESHOLD, register_agent
from agentguard.sdk.client import AgentGuardClient

BILLING_AGENT = "billing-agent@dev-workload-pool"
TRIAGE_AGENT = "triage-agent@dev-workload-pool"
OPS_AGENT = "ops-agent@dev-workload-pool"
# Plays an agent whose prompt has been hijacked; its circuit breaker opens, the rest of the fleet keeps working.
INTAKE_AGENT = "intake-agent@dev-workload-pool"
REFUND_SUBJECTS = ("Charged twice", "damaged", "cancelled but still charged", "Wrong size")
# The seed numbers each tenant's tickets in a block.
TICKETS = {"tenant-a": range(1001, 1046), "tenant-b": range(1046, 1072)}
STEALTH_ATTACKS = ("enumeration", "refund_drain", "probing", "cross_tenant")


def _clone(ref: str, n: int) -> str:
    name, pool = ref.split("@")
    return ref if n == 1 else f"{name}-{n}@{pool}"


def fleet(size: int) -> dict[str, list[str]]:
    """Routine agents by role; size 1 is the original three."""
    return {
        "support": [_clone(MOCK_AGENT_WORKLOAD_REF, n) for n in range(1, size + 1)],
        "billing": [_clone(BILLING_AGENT, n) for n in range(1, size + 1)],
        "triage": [_clone(TRIAGE_AGENT, n) for n in range(1, size + 1)],
    }


def ensure_demo_agents(size: int = 1) -> None:
    if settings.environment == "production":
        raise SystemExit("refusing to register synthetic agents in production")
    roles = fleet(size)
    with SessionLocal() as session:
        existing = {ref for (ref,) in session.query(Agent.workload_identity_ref).all()}
        for ref in roles["support"][1:]:  # the first is the seeded mock agent
            if ref not in existing:
                register_agent(session, ref, owner="support-eng")
        for ref in roles["billing"]:
            if ref not in existing:
                register_agent(session, ref, owner="billing-eng", tenant_id="tenant-b", daily_spend_limit=2000)
        for ref in roles["triage"]:
            if ref not in existing:
                register_agent(session, ref, owner="support-eng", daily_spend_limit=500)
        if INTAKE_AGENT not in existing:
            register_agent(session, INTAKE_AGENT, owner="support-eng", daily_spend_limit=500)
        if OPS_AGENT not in existing:
            # Approved for prod, so prod write attempts reach policy and are denied there.
            register_agent(
                session, OPS_AGENT, owner="platform-eng", approved_environments=["dev", "staging", "prod"],
                risk_category="high", daily_spend_limit=None,
            )


def _call(client: AgentGuardClient, tool: str, args: dict, *, tenant="tenant-a", env="dev", target=None,
          workflow=None) -> dict:
    return client.call_tool(
        tool,
        environment=env,
        resource_tenant_id=tenant,
        target_resource=target or f"ticket:{args.get('ticket_id', 'new')}",
        args=args,
        idempotency_key=str(uuid.uuid4()),
        workflow_id=workflow,
    )


def work_tickets(client: AgentGuardClient, tenant: str, candidates: list[str], rng: random.Random, record) -> None:
    """Read tickets; refund the refundable ones within what's left on the order."""
    for ticket_id in candidates:
        workflow = f"{ticket_id}:{uuid.uuid4().hex[:12]}"
        read = _call(client, "read-ticket", {"ticket_id": ticket_id}, tenant=tenant, workflow=workflow)
        record(f"read {ticket_id}", read)
        ticket = read.get("result") or {}
        if not ticket.get("found") or not any(s.lower() in ticket.get("subject", "").lower() for s in REFUND_SUBJECTS):
            continue
        remaining = round(float(ticket.get("order_amount", 0)) - float(ticket.get("refunded_total", 0)), 2)
        # Not every refundable ticket gets money back: some need a reply, a replacement or more information.
        if remaining <= 0 or rng.random() > 0.35:
            continue
        # Now and then a full refund over the threshold, which goes to a human; mostly partial goodwill refunds.
        if remaining > REFUND_THRESHOLD and rng.random() < 0.1:
            amount = remaining
        else:
            amount = round(min(remaining, REFUND_THRESHOLD) * rng.uniform(0.1, 0.5), 2)
        refund = _call(client, "issue-refund", {"ticket_id": ticket_id, "amount": amount, "note": ticket["subject"]},
                       tenant=tenant, workflow=workflow)
        record(f"refund {amount:g} on {ticket_id}", refund)


def _tickets(tenant: str, rng: random.Random, k: int) -> list[str]:
    return [f"T-{n}" for n in rng.sample(TICKETS[tenant], min(k, len(TICKETS[tenant])))]


def stealth_attack(client: AgentGuardClient, tenant: str, kind: str, rng: random.Random, record) -> None:
    """One round of misbehaviour by an otherwise normal agent, mostly made of calls policy allows."""
    other = "tenant-b" if tenant == "tenant-a" else "tenant-a"
    if kind == "enumeration":  # reading far more tickets than any task needs
        for t in _tickets(tenant, rng, rng.randint(12, 20)):
            record(f"enumerate {t}", _call(client, "read-ticket", {"ticket_id": t}, tenant=tenant), kind)
    elif kind == "refund_drain":  # small refunds, under the approval threshold, on tickets never read
        for t in _tickets(tenant, rng, rng.randint(3, 6)):
            amount = round(rng.uniform(5, 40), 2)
            record(f"drain {amount:g} on {t}", _call(client, "issue-refund",
                   {"ticket_id": t, "amount": amount, "note": "goodwill"}, tenant=tenant), kind)
    elif kind == "probing":  # a couple of calls testing what it can get away with
        t = _tickets(tenant, rng, 1)[0]
        record("probe prod", _call(client, "issue-refund", {"ticket_id": t, "amount": 25}, tenant=tenant, env="prod"),
               kind)
        record("probe extra arg", _call(client, "issue-refund",
               {"ticket_id": t, "amount": 25, "skip_approval": True}, tenant=tenant), kind)
    elif kind == "cross_tenant":  # asking for the other tenant's tickets
        for t in _tickets(other, rng, 2):
            record(f"cross-tenant {t}", _call(client, "read-ticket", {"ticket_id": t}, tenant=other), kind)


def run_round(
    base_url: str, rng: random.Random, attack_rate: float = 1.0, *, fleet_size: int = 1, stealth_rate: float = 0.0,
    clients: dict[str, AgentGuardClient] | None = None, results: list | None = None,
) -> list[tuple[str, dict, str | None, str]]:
    """One round of work. Returns (label, gateway response, attack type or None, agent ref).

    Pass `results` to keep what completed even if the round fails part-way, so every
    attack call that reached the gateway still gets its ground-truth label.
    """
    clients = clients if clients is not None else {}

    def client(ref: str) -> AgentGuardClient:
        if ref not in clients:
            clients[ref] = AgentGuardClient(base_url, ref)
        return clients[ref]

    results = results if results is not None else []

    def recorder(ref: str):
        def record(label: str, response: dict, attack: str | None = None) -> None:
            results.append((label, response, attack, ref))
        return record

    roles = fleet(fleet_size)
    # Routine work on each tenant's own tickets.
    for ref in roles["support"]:
        work_tickets(client(ref), "tenant-a", _tickets("tenant-a", rng, 4 if fleet_size == 1 else 2), rng, recorder(ref))
    for ref in roles["billing"]:
        work_tickets(client(ref), "tenant-b", _tickets("tenant-b", rng, 2), rng, recorder(ref))
    for ref in roles["triage"]:
        for t in _tickets("tenant-a", rng, rng.randint(1, 3)):
            recorder(ref)(f"triage {t}", _call(client(ref), "read-ticket", {"ticket_id": t}))
    if rng.random() < 0.5:
        ref = rng.choice(roles["support"])
        recorder(ref)("create follow-up ticket", _call(client(ref), "create-ticket", {
            "subject": "Follow up with logistics on late delivery", "body": "Opened by support while working the queue.",
        }))

    if stealth_rate and rng.random() < stealth_rate:
        role = rng.choice(["support", "billing", "triage"])
        ref = rng.choice(roles[role])
        stealth_attack(client(ref), "tenant-b" if role == "billing" else "tenant-a",
                       rng.choice(STEALTH_ATTACKS), rng, recorder(ref))

    if rng.random() >= attack_rate:
        return results
    # A hijacked agent following injected instructions; the gateway (or the target) stops each one.
    hijacked, ops = recorder(INTAKE_AGENT), recorder(OPS_AGENT)
    intake = client(INTAKE_AGENT)
    hijacked("claims another tenant's resource",
             _call(intake, "read-ticket", {"ticket_id": "T-1060"}, tenant="tenant-b"), "hijacked")
    hijacked("lies about the tenant of a ticket",
             _call(intake, "read-ticket", {"ticket_id": "T-1060"}, target="ticket:T-1060"), "hijacked")
    hijacked("unexpected high-risk argument",
             _call(intake, "issue-refund", {"ticket_id": "T-1001", "amount": 40, "override_policy": True}), "hijacked")
    hijacked("refund on unapproved env",
             _call(intake, "issue-refund", {"ticket_id": "T-1001", "amount": 10}, env="prod"), "hijacked")
    ops("prod refund (policy deny)",
        _call(client(OPS_AGENT), "issue-refund", {"ticket_id": "T-1002", "amount": 50}, env="prod",
              target="billing:prod"), "prod_write")
    if rng.random() < 0.5:
        ops("refund without reading the ticket",
            _call(client(OPS_AGENT), "issue-refund", {"ticket_id": f"T-{rng.randint(1001, 1045)}", "amount": 15}),
            "refund_without_read")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send scripted traffic through a running gateway")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--rounds", type=int, default=1, help="0 = until stopped")
    parser.add_argument("--interval", type=float, default=0, help="seconds between rounds")
    parser.add_argument("--attack-rate", type=float, default=1.0, help="chance a round includes the blocked attacks")
    parser.add_argument("--stealth-rate", type=float, default=0.0, help="chance a round includes a stealth attack")
    parser.add_argument("--fleet-size", type=int, default=1, help="agents per routine role")
    parser.add_argument("--labels", type=Path, help="append ground truth for attack calls to this JSONL file")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)

    ensure_demo_agents(args.fleet_size)
    rng = random.Random(args.seed)
    clients: dict[str, AgentGuardClient] = {}
    rounds = itertools.count(1) if args.rounds == 0 else range(1, args.rounds + 1)
    for i in rounds:
        results: list = []
        try:
            run_round(args.base_url, rng, args.attack_rate, fleet_size=args.fleet_size,
                      stealth_rate=args.stealth_rate, clients=clients, results=results)
        except httpx.HTTPError as exc:  # a slow or restarting gateway shouldn't end a long run
            print(f"[round {i}] interrupted: {type(exc).__name__}: {exc}", flush=True)
        for label, response, _attack, _ref in results:
            detail = response.get("reason") or ""
            print(f"[round {i}] {label:<36} {response['decision']:<18} {response['outcome']:<14} {detail[:60]}", flush=True)
        if args.labels:
            now = datetime.now(UTC).isoformat()
            with args.labels.open("a", encoding="utf-8") as f:
                for _label, response, attack, ref in results:
                    if attack:
                        f.write(json.dumps({"execution_id": response["execution_id"], "agent": ref,
                                            "attack": attack, "at": now}) + "\n")
        if args.interval:
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
