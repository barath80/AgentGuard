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
- The console's anomaly report batches its lookups instead of running one per call (24 s down to 0.5 s), and reads plain rows rather than ORM objects.
- On a day of fleet traffic (32,000 calls), the console's behavioural-risk view took 54 s. Three changes brought it to 2.4 s cold and 0.15 s cached:
  - Window building selects only the columns the features use, with arguments joined in: 9 s down to 0.9 s, verified to produce identical windows.
  - Model libraries import in the background at startup instead of on the first request.
  - The report is shared by every viewer for a few seconds, keyed by the model file so a newly saved model shows up at once.

## Results

All runs below were measured on AC power with fresh databases. Calls/s are for the full path, gateway → broker → helpdesk.

**Hot-path optimisation** (1 worker per service, 16 clients, connection pools 4 + 4):

| | Throughput | p50 | p95 |
|---|---|---|---|
| Before | 25.0 calls/s | 561 ms | 1,221 ms |
| After (fewer commits and statements, cached rules, pooled clients) | **72.2 calls/s** | **151 ms** | **568 ms** |

**After the concurrency fixes, one instance per service** (pools 3 + 2), measured in two separate sessions:

| Clients | Throughput | p50 |
|---|---|---|
| 32 | 140, 139 calls/s (session 1); 143, 146 calls/s (session 2) | 215–220 ms |
| 64 | 137, 137 calls/s (session 1); 143, 138 calls/s (session 2) | 435–455 ms |

Before defect 5 was fixed, this configuration completed **0 calls** at 64 clients (deadlock). Now it holds its throughput at saturation, and latency grows with the queue (Little's law), which is correct behaviour.

**Adding gateway replicas.** These are independent single-process replicas, the production shape, with the load tester spreading clients across them (`--base-url` takes a comma-separated list). All four measured in one session:

| Replicas (gateway / broker / helpdesk) | 32 clients |
|---|---|
| 1 / 1 / 1 | 42.5, 42.5 calls/s |
| 2 / 1 / 1 | 69.7, 71.8 calls/s (**1.7×**) |
| 2 / 2 / 1 | 67.4, 67.9 calls/s |
| 4 / 2 / 2 | 71.5, 70.4 calls/s |

A second gateway adds 1.7×. Beyond that, extra replicas add nothing **on this machine**, because the load client, every service and Postgres share one laptop's 16 hardware threads.

### What this laptop can and can't tell you

Treat the numbers above as *relative*, within a session. Across sessions on this one laptop, identical code measured anywhere from 24 to 146 calls/s for one instance. The variation tracked:
- **battery power:** about 45% slower CPU, and results swinging 10× between repeats;
- **sleep and resume;**
- **background load** from the desktop applications sharing the machine.

Uvicorn's `--workers` mode on Windows was also erratic: it shares one listening socket across spawned processes, and a keep-alive connection stays pinned to whichever worker accepted it. That's why the replica runs use independent processes, as Kubernetes pods would.

Measure absolute capacity on separate hosts or a real cluster: `python -m agentguard.evaluation.loadtest --base-url <gateway or load balancer>`, with fresh databases and at least two repeats.

## Load balancing

- **Compose:** `gateway-lb` (nginx, least-connections) is the only way in to `app` replicas (`GATEWAY_REPLICAS`, default 2); see `deploy/nginx/gateway-lb.conf`. It retries a failed request on another replica, **including POSTs**. That's safe only because every tool call carries an idempotency key the database enforces, so a retried call returns the first execution and never makes a second side effect.
- **Kubernetes:** the gateway Service balances across pods, and the HPA scales them (`gateway.autoscaling`). Correctness doesn't depend on which replica serves a call, because the per-agent checks lock in Postgres, not in memory.

## Sizing

- **Connections:** replicas × workers × (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`) for the gateway and broker, plus the helpdesk's, must stay under Postgres `max_connections`. With an autoscaler, size for `maxReplicas` or put PgBouncer in front.
- **Threads:** synchronous handlers share a thread pool (`REQUEST_THREADS`, anyio default 40). After the fixes above, a request never holds a connection while waiting for a thread, so a small pool queues instead of deadlocking. `integration/test_pool_starvation.py` proves it with 1 connection, 3 threads and 12 concurrent requests.
- **Where it saturates:** a single gateway's threads queue for its pooled connections, and a second replica adds 1.7×. On a single laptop the machine itself is the next limit; separate hosts or pods scale further.
