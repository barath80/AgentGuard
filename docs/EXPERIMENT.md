# Experiment: direct access vs. gateway-enforced

Spec step 30: compare calling tools directly against going through AgentGuard, and report both the latency cost and the security difference plainly, as a trade-off — not a result to win.

Run it yourself: `python -m agentguard.evaluation.comparison_cli` (source: `src/agentguard/evaluation/comparison.py`). Verified by `tests/test_comparison.py`.

## What "direct access" means here

Not "no credential at all" — that's the gateway-bypass case in `docs/EVALUATION.md`, already covered. This models the more common real situation: an agent that has simply been handed the target system's API credential directly, the way most tool integrations work without a product like this in front of them. It calls the target with no identity resolution, no policy evaluation, no approval gating, no rate limiting, and no schema check.

## Security result (actual run)

| Scenario | Direct access | Gateway |
|---|---|---|
| Cross-tenant read | Succeeds — the target system has no tenant concept at all | Denied |
| Refund in a prod environment | Succeeds — the target has no environment concept | Denied |
| Large refund (5,500, over the 500 threshold) | Executes immediately, no approval | Held for human approval, not executed unattended |
| Call-rate spam (10 calls, limit 5/min) | 10/10 succeed | 5/10 allowed, 5 denied |
| Unexpected `admin_override` argument on a high-risk tool | Silently accepted by the executor | Denied by schema check |

**Security delta: 5/5.** Every scenario that direct access would have allowed through, the gateway blocked or gated. This isn't surprising — these are exactly the protections AgentGuard adds — but it's the point: none of the five controls above exist at the target system. Remove the gateway and they're gone, not degraded.

## Latency result (actual run, in-process, SQLite, single process)

| Tool | Direct p50 / p95 | Gateway p50 / p95 | Overhead (p95) |
|---|---|---|---|
| read-ticket | 0.02 / 0.03 ms | 22.5 / 41.2 ms | +41.2 ms |
| issue-refund | 0.02 / 0.05 ms | 26.4 / 30.8 ms | +30.7 ms |

### Same comparison on Postgres 16 (actual run, 2026-10-30)

`python -m agentguard.evaluation.comparison_cli --database-url postgresql+psycopg://…/agentguard_compare`, against a local Postgres 16 server (`pgserver`, same Windows machine, TCP on localhost). It ran twice, back to back with the SQLite run, while the live stack and the demo fleet were also running on the machine:

| Tool | SQLite gateway p50 / p95 | Postgres gateway p50 / p95 |
|---|---|---|
| read-ticket | 35.2 / 53.3 ms · 34.4 / 43.1 ms | 15.7 / 29.0 ms · 13.5 / 29.3 ms |
| issue-refund | 33.6 / 68.6 ms · 35.8 / 173.6 ms | 14.3 / 25.4 ms · 13.6 / 18.5 ms |

On the same logic, Postgres roughly halves the median overhead (about 14 ms against about 34 ms) and is far steadier at p95. That supports the reading below: the cost is the sequence of commits, not policy evaluation. The security result was identical (5/5) on both databases. This is still one machine with no network between gateway and database; a managed Postgres across a network adds its round-trip time to each commit.

These exact figures move around run to run (±20-30 ms on this machine, mostly disk/scheduler noise) — treat them as "tens of milliseconds," not as precise numbers to cite.

**Read this carefully — it overstates the real cost:**

- These numbers are in-process (no network hop for either path) and use SQLite, which commits each transaction to disk synchronously. The gateway does several sequential commits per call (decision, approval/execution state, audit event); each one pays SQLite's fsync cost. The Postgres run below confirms that's where most of the cost is.
- As a rough cross-check: the Phase 1 live-server test (`tests/test_app.py`, and the earlier manual `/v1/stats` run against a real `uvicorn` process) showed p50 ≈ 11 ms / p95 ≈ 31 ms for mixed decision latency *including* the HTTP round trip — lower than the in-process number above, which only confirms that the in-process SQLite commit pattern here, not network overhead, is the dominant cost in this specific measurement.
- "Direct access" here does zero work — one in-memory dict lookup — so it is close to a latency floor, not a realistic baseline (a real external API call would itself take single-digit-to-double-digit milliseconds).

**Honest takeaway:** the gateway adds real latency, on the order of tens of milliseconds per call in this setup, driven mostly by the number of DB commits in the decision path rather than by policy evaluation itself (the policy engine is a pure in-memory function — see `policy/engine.py`). Reducing round trips to the DB (e.g., batching the decision + audit write into one transaction) is the next concrete optimization if this becomes a bottleneck; it hasn't been done here since it wasn't needed to get Phase 1–4 working correctly, and `docs/ROADMAP.md` tracks it as a follow-up rather than a claim that latency is already optimized.

## Task-success framing

Separate from this experiment, `docs/EVALUATION.md`'s routine cases cover the other side of this trade-off: the gateway isn't just measured on what it blocks, but also on whether legitimate work (refund under threshold, routine reads) completes without friction. As of the last evaluation run, false-deny rate was 0% — no legitimate routine case was blocked. If that ever stops being true, it means the controls above have gotten too aggressive, and that's a regression worth treating as seriously as a false allow.
