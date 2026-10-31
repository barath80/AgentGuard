"""Load test: many concurrent agents driving real tool calls through a running stack.

    python -m agentguard.evaluation.loadtest --base-url http://127.0.0.1:9000 \\
        --operator-token dev-only-operator-token --agents 20 --concurrency 32 --duration 60 --json load.json

Each virtual client sends tool calls as one of the load agents (registered through the
console API, with a high rate limit and no spend budget so the gateway's own throttles
don't cap the measurement). The mix is mostly ticket reads plus small refunds, on the
full path: identity, controls, policy, audit, broker, target. It reports throughput,
latency percentiles, the decision mix and errors.

Point it at a dev or staging stack, never production: the refunds it sends are real
calls against the target.
"""

import argparse
import asyncio
import json
import random
import statistics
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field

import httpx

TICKETS = [f"T-{n}" for n in range(1001, 1046)]  # tenant-a in the seeded helpdesk


@dataclass
class LoadReport:
    label: str
    concurrency: int
    agents: int
    duration_s: float
    requests: int = 0
    throughput_rps: float = 0.0
    latency_ms: dict[str, float] = field(default_factory=dict)
    decisions: dict[str, int] = field(default_factory=dict)
    outcomes: dict[str, int] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)


def percentiles(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {}
    ordered = sorted(samples)

    def at(p: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(p * len(ordered)))], 1)

    return {"p50": at(0.50), "p90": at(0.90), "p95": at(0.95), "p99": at(0.99),
            "max": round(ordered[-1], 1), "mean": round(statistics.fmean(ordered), 1)}


def register_agents(base_url: str, operator_token: str, count: int, run: str) -> list[str]:
    refs = []
    with httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {operator_token}"}, timeout=30) as http:
        for i in range(count):
            ref = f"load-{run}-{i}@load-test-pool"
            response = http.post("/v1/console/agents", json={
                "workload_identity_ref": ref, "owner": "load-test", "tenant_id": "tenant-a",
                "approved_environments": ["dev"], "risk_category": "low",
                "rate_limit_per_minute": 10_000, "daily_spend_limit": None,
            })
            if response.status_code not in (201, 409):
                raise SystemExit(f"could not register load agents: {response.status_code} {response.text}")
            refs.append(ref)
    return refs


def _body(rng: random.Random, refund_share: float) -> dict:
    ticket = rng.choice(TICKETS)
    if rng.random() < refund_share:
        tool, args = "issue-refund", {"ticket_id": ticket, "amount": round(rng.uniform(0.5, 2.0), 2), "note": "load test"}
    else:
        tool, args = "read-ticket", {"ticket_id": ticket}
    return {
        "tool_name": tool, "environment": "dev", "resource_tenant_id": "tenant-a",
        "target_resource": f"ticket:{ticket}", "args": args, "idempotency_key": str(uuid.uuid4()),
    }


async def _client(http: httpx.AsyncClient, agents: list[str], deadline: float, rng: random.Random,
                  refund_share: float, latencies: list[float], decisions: Counter, outcomes: Counter,
                  errors: Counter, record: bool) -> None:
    while time.monotonic() < deadline:
        agent = rng.choice(agents)
        started = time.perf_counter()
        try:
            response = await http.post("/v1/tool-calls", json=_body(rng, refund_share),
                                       headers={"x-workload-identity": agent})
        except httpx.HTTPError as exc:
            if record:
                errors[type(exc).__name__] += 1
            continue
        elapsed = (time.perf_counter() - started) * 1000
        if not record:
            continue
        if response.status_code != 200:
            errors[f"http_{response.status_code}"] += 1
            continue
        data = response.json()
        latencies.append(elapsed)
        decisions[data["decision"]] += 1
        outcomes[data["outcome"]] += 1


async def run(base_url: str, agents: list[str], *, concurrency: int, duration: float, warmup: float,
              refund_share: float, label: str, seed: int = 0) -> LoadReport:
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=30) as http:
        noop: list[float] = []
        if warmup:
            deadline = time.monotonic() + warmup
            await asyncio.gather(*(_client(http, agents, deadline, random.Random(seed + i), refund_share,
                                           noop, Counter(), Counter(), Counter(), record=False)
                                   for i in range(concurrency)))
        latencies: list[float] = []
        decisions, outcomes, errors = Counter(), Counter(), Counter()
        started = time.monotonic()
        deadline = started + duration
        await asyncio.gather(*(_client(http, agents, deadline, random.Random(seed + 1000 + i), refund_share,
                                       latencies, decisions, outcomes, errors, record=True)
                               for i in range(concurrency)))
        elapsed = time.monotonic() - started
    return LoadReport(
        label=label, concurrency=concurrency, agents=len(agents), duration_s=round(elapsed, 1),
        requests=len(latencies), throughput_rps=round(len(latencies) / elapsed, 1),
        latency_ms=percentiles(latencies), decisions=dict(decisions), outcomes=dict(outcomes), errors=dict(errors),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load-test a running AgentGuard stack")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--operator-token", required=True, help="registers the load agents via the console API")
    parser.add_argument("--agents", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--duration", type=float, default=60, help="measured seconds")
    parser.add_argument("--warmup", type=float, default=10, help="unmeasured seconds first")
    parser.add_argument("--refund-share", type=float, default=0.2)
    parser.add_argument("--label", default="run")
    parser.add_argument("--run-id", default=uuid.uuid4().hex[:6], help="reuse to reuse the same load agents")
    parser.add_argument("--json", dest="json_path")
    args = parser.parse_args(argv)

    agents = register_agents(args.base_url, args.operator_token, args.agents, args.run_id)
    report = asyncio.run(run(args.base_url, agents, concurrency=args.concurrency, duration=args.duration,
                             warmup=args.warmup, refund_share=args.refund_share, label=args.label))
    lat = report.latency_ms
    print(f"{report.label}: {report.requests} calls in {report.duration_s}s = {report.throughput_rps} calls/s "
          f"at concurrency {report.concurrency}")
    if lat:
        print(f"latency ms  p50 {lat['p50']}  p90 {lat['p90']}  p95 {lat['p95']}  p99 {lat['p99']}  max {lat['max']}")
    print(f"decisions {report.decisions}  outcomes {report.outcomes}  errors {report.errors or 'none'}")
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as f:
            json.dump(asdict(report), f, indent=2)
    return 0 if not report.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
