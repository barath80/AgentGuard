# Performance and scale-out

How AgentGuard behaves under concurrent load, what load testing found, and how to size it.

The short version: the gateway is stateless. Decisions, approvals, rate limits, budgets and the audit trail all live in Postgres, so it scales horizontally behind a load balancer. Getting there safely took fixing seven defects that only appear under concurrency. Four of them were correctness bugs, not just slowness. All seven are pinned by tests.

## How it's measured

`python -m agentguard.evaluation.loadtest` drives concurrent virtual clients through the full path: identity, controls, policy, audit, broker, helpdesk. It registers its own load agents through the console API, with high rate limits and no budget, so the gateway's throttles don't cap the measurement. It reports throughput, latency percentiles, the decision mix and errors as JSON.

The benchmark setup is deliberately production-shaped:

- Postgres 16 (a local `pgserver` instance on the same Windows laptop; Intel Core 7 240H, 16 threads).
- Separate AgentGuard and helpdesk databases. Migrations run as the owner role; services connect as the restricted runtime role (`agentguard.audit.grants`).
- Fresh databases for every configuration.
- **Small connection pools on purpose** (3 + 2 overflow per process), so pool behaviour is stressed rather than hidden.
- Each configuration is checked for leftover processes before it runs. An early run was invalidated by stale stacks: on Windows, uvicorn's `SO_REUSEADDR` lets a new server bind a port an old one still holds.
- 20-second measured runs after a 5-second warm-up, each repeated twice.

Caveat: client, services and database all share one machine, so absolute numbers are lower than separate hosts would give. The comparisons between configurations are what's meaningful.

## What load testing found

| # | Defect | Effect | Fix | Test |
|---|---|---|---|---|
| 1 | Idempotency was read-then-insert | Concurrent duplicates of one call could all execute | Unique constraint on (agent, key); the loser returns the winner's execution | `test_concurrency.py` |
| 2 | Spend budget counted only finished calls | A burst of refunds all saw the old total and all passed | In-flight allowed spend is reserved; checks serialised per agent | `test_concurrency.py` |
| 3 | Rate limit was check-then-act | Concurrent calls overshot the limit | Per-agent row lock (Postgres, all replicas) around checks + decision | `test_concurrency.py` |
| 4 | DB connection held during the broker call | Each in-flight call pinned a pooled connection; the pool capped throughput | End the transaction before calling the broker | `test_concurrency.py` |
| 5 | Thread-pool / connection-pool deadlock (three paths: sync session cleanup, expire-on-commit reloads, identity transaction left open) | Above ~40 concurrent requests, zero throughput until the 30 s pool timeout | Async session dependency, `expire_on_commit=False`, identity transaction ended before the endpoint's thread hop | `integration/test_pool_starvation.py` |
| 6 | HTTP client cache without a lock | A cold burst built one TLS context per request (~15 s stall), tripping broker deadlines and a reconciliation storm | Locked single creation, warmed at startup | `test_concurrency.py` |
| 7 | Reconciliation held a DB connection during the target call, and its errors became HTTP 500s | Storms exhausted the broker's pool | Release before calling out; a failed reconciliation leaves an unresolved timeout | `test_concurrency.py` |

Smaller wins along the way:

- Commits per allowed call fell from 5 to 2. The decision and its audit event commit together, which is also more correct.
- Statements per call fell from 21 to 16.
- A published policy's rules are cached by id. A version is never edited, only replaced, and the active version is still read on every call.
- Pooled HTTP clients replace a client per call, which cost about 230 ms each on Windows.
- An (agent, time) index serves the per-call controls.
- The console's anomaly report batches its lookups instead of running one per call (24 s down to 0.5 s).

## Results

All runs below were measured on AC power with fresh databases. Calls/s are for the full path, gateway → broker → helpdesk.

**Hot-path optimisation** (1 worker per service, 16 clients, connection pools 4 + 4):

| | Throughput | p50 | p95 |
|---|---|---|---|
| Before | 25.0 calls/s | 561 ms | 1,221 ms |
| After (fewer commits and statements, cached rules, pooled clients) | **72.2 calls/s** | **151 ms** | **568 ms** |

**Scale-out after the concurrency fixes** (pools 3 + 2 per process, two repeats each):

| Workers (gateway / broker / helpdesk) | 32 clients | 64 clients |
|---|---|---|
| 1 / 1 / 1 | 140, 139 calls/s · p50 220 ms | 137, 137 calls/s · p50 455 ms |
| 2 / 1 / 1 | 212, 192 calls/s · p50 147–158 ms | 169, 179 calls/s · p50 320–325 ms |
| 4 / 2 / 2 | 246, 241 calls/s · p50 109–113 ms | collapsed: found defect 6 |
| 8 / 4 / 4 | collapsed: found defect 6 | collapsed: found defect 6 |

For comparison, before defect 5 was fixed, one worker at 64 clients completed **0 calls** (deadlock). After it, the same configuration holds 137 calls/s, with latency growing as the queue grows (Little's law), which is the expected behaviour at saturation.

**Not yet re-measured:** 4/2/2 and 8/4/4 since fixing defects 6 and 7. The re-run happened on battery power, which slowed this laptop's CPU by about 45% and made results swing by 10× between repeats, so it was discarded. To reproduce on AC power:
1. Start the stack.
2. Run `python -m agentguard.evaluation.loadtest` at each configuration, with fresh databases.
3. Repeat each run at least twice.

## Load balancing

- **Compose:** `gateway-lb` (nginx, least-connections) is the only way in to `app` replicas (`GATEWAY_REPLICAS`, default 2); see `deploy/nginx/gateway-lb.conf`. It retries a failed request on another replica, **including POSTs**. That's safe only because every tool call carries an idempotency key the database enforces, so a retried call returns the first execution and never makes a second side effect.
- **Kubernetes:** the gateway Service balances across pods, and the HPA scales them (`gateway.autoscaling`). Correctness doesn't depend on which replica serves a call, because the per-agent checks lock in Postgres, not in memory.

## Sizing

- **Connections:** replicas × workers × (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`) for the gateway and broker, plus the helpdesk's, must stay under Postgres `max_connections`. With an autoscaler, size for `maxReplicas` or put PgBouncer in front.
- **Threads:** synchronous handlers share a thread pool (`REQUEST_THREADS`, anyio default 40). After the fixes above, a request never holds a connection while waiting for a thread, so a small pool queues instead of deadlocking. `integration/test_pool_starvation.py` proves it with 1 connection, 3 threads and 12 concurrent requests.
- **Where it saturates:** with one or two gateway workers the gateway process is the limit: its threads queue for the five pooled connections, and a second worker adds 45% at 32 clients. On this single laptop, client, three services and Postgres share 16 hardware threads, so the machine itself saturates above about 250 calls/s. Separate hosts, or pods, scale further.
