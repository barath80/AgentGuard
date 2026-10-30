# Roadmap

Phase checklist for AgentGuard. Keep this in sync with actual implementation state — check items off as they land, don't batch-update at the end.

## Phase 0 — Scaffolding

- [x] `CLAUDE.md` with no-auto-commit, fail-closed, and identity-boundary rules
- [x] `docs/ARCHITECTURE.md`
- [x] `docs/DATA_MODEL.md`
- [x] `docs/THREAT_MODEL.md`
- [x] `docs/EVALUATION.md`
- [x] `docs/ROADMAP.md` (this file)
- [x] Repo skeleton: `src/agentguard/{gateway,identity,policy,approval,broker,audit,sdk}`, `tests/`
- [x] `docker-compose.yml` (Postgres + Redis + app)
- [x] `.env.example`, `.gitignore`, `pyproject.toml`/`requirements.txt`

## Phase 1 — Core authorization loop (MVP)

- [x] Agent registry (model + seed fixture; CRUD endpoints deferred — not needed until a second agent exists)
- [x] Tool registry (model + seed fixture), 3 mock tools: `read-ticket`, `create-ticket`, `issue-refund`
- [x] Policy model + versioning, first rule set (`src/agentguard/policy/seed.py`)
- [x] Gateway endpoint: intake → schema validation → identity derivation (mocked header) → policy evaluation → decision
- [x] Approval state machine: create, bind (action hash + resource + policy version), expire, one-time atomic consumption
- [x] Execution broker: scoped mock credentials, timeout, idempotency key
- [x] Append-only audit log of every decision + outcome (`audit_events` table)
- [x] One mock support agent exercising all 3 tools end-to-end (`src/agentguard/sdk/mock_agent.py`, verified live against a running gateway)
- [x] Alembic migrations for Phase 1 schema (`migrations/versions/db9b0c2cd2d2_initial_schema.py`)

Verified: `pytest` (14/14 passing, routine + adversarial cases), `ruff check .` (clean), `alembic upgrade head` against SQLite, and a live `uvicorn` run exercising the full allow / approval-required / approve / execute HTTP flow.

Deferred to later phases on purpose: agent/tool CRUD endpoints (only one seeded agent needed so far), real OIDC/mTLS identity verification (header-based mock stands in, isolated behind `identity/service.py`), and broker state reconciliation on timeout (stubbed as a documented gap, see `docs/THREAT_MODEL.md` #5).

## Phase 2 — Adversarial & regression test suite

- [x] Routine test cases (see `docs/EVALUATION.md`) — `src/agentguard/evaluation/cases.py`
- [x] Adversarial test cases (prompt injection, spoofed identity, duplicate call, replayed approval, prohibited prod access, wrong-tenant/wrong-role approver, policy change during pending approval)
- [x] Gateway bypass case — moved to Phase 4, done there
- [x] Metrics collection: false-allow/false-deny rate, audit completeness, duplicate side-effect rate, per-case p95 latency — `python -m agentguard.evaluation.runner`
- [x] CI wiring (`.github/workflows/ci.yml`): lint + pytest + evaluation gate on every push/PR; fails if false-allow > 0 or any routine case regresses; uploads the JSON report as an artifact

## Phase 3 — Replay & counterfactual simulation

- [x] Deterministic replay against mocks/read-only systems — re-derives each recorded decision under the policy version it was made with (not the active one) and runs tool steps through dry-run adapters (`src/agentguard/replay/`)
- [x] Counterfactual policy simulation (proposed policy vs. recorded traces, no execution) — `python -m agentguard.replay.cli simulate --policy <file>`; `--fail-on-loosening` makes it usable as a pre-release gate
- [x] Task-success measurement alongside safety metrics — simulation reports newly-allowed (security loosening) and newly-restricted (task-success risk) calls separately, plus unattended completion rate before → after
- [x] Snapshot agent risk category (and tool risk class) into the trace, so re-categorising an agent or tool doesn't stop old decisions reproducing; pre-snapshot rows fall back to the registry

## Phase 4 — Resilience & observability

- [x] Fail closed when policy evaluation errors: deny everything by default; the only fallback (`ALLOW_READS_WHEN_POLICY_UNAVAILABLE`, off by default) allows `read`-class tools and never writes. Approved calls aren't executed during an outage either.
- [x] Runtime controls (`src/agentguard/controls/`), derived from the trace rather than separate counters: global kill switch (`KILL_SWITCH`), per-agent suspend/resume (effective on the next call), per-agent rate limit, rolling 24h spend budget (re-checked when approved calls execute), and a circuit breaker on repeated denials that ignores its own denials
- [x] Anomaly report (`python -m agentguard.controls.cli anomalies`): denial bursts, control denials (spikes, budget overruns, open circuits), refunds without a prior read of the ticket, unresolved timeouts
- [x] OpenTelemetry tracing (tool call → policy → broker spans; no payloads in attributes), Jaeger under the `observability` Compose profile, and an operator-only `/v1/stats` endpoint (decision latency p50/p95, trace completeness, denials by reason, pending approvals)
- [x] Broker timeouts return immediately (previously the worker thread blocked the response — a Phase 1 bug), and timed-out writes are reconciled against the target system's idempotency record, including when the agent retries with the same key; never re-executed
- [x] Least-privilege credentials: one scope per executor, enforced by the target system; only `agentguard.broker` can obtain them (static inventory test), and the agent SDK never loads the broker
- [x] Gateway-bypass evaluation case (moved from Phase 2)
- [x] Real deployment isolation for bypass prevention: broker as a separate service holding the secrets (`agentguard.broker.server`), re-verifying each execution, with network policy so only it can reach targets (`deploy/helm`, Phase 8)
- [x] Duplicate refunds after a timeout retried with a *new* key: the helpdesk now enforces a business-level rule — total refunds on a ticket can never exceed the order amount — whatever idempotency keys are used. Ticket creation can still duplicate in that case (low risk; surfaced as unresolved timeouts)

## Phase 5 — Experiment & writeup

- [x] Direct-access vs. gateway-enforced comparison (latency + security delta) — `python -m agentguard.evaluation.comparison_cli`, written up in [docs/EXPERIMENT.md](EXPERIMENT.md). Result: 5/5 security scenarios blocked, with an honestly-caveated latency cost (dominated by SQLite commit overhead in-process, not policy evaluation — confirmed by a Postgres 16 run: p50 about 14 ms against about 34 ms on SQLite)
- [x] Schema validation against `input_schema`: unexpected arguments rejected on `write-high`/`destructive` tools (THREAT_MODEL #13) — `src/agentguard/gateway/validation.py`
- [x] Audit log hardening (THREAT_MODEL #8): separate schema-owner and runtime roles; the runtime role gets SELECT/INSERT only on the append-only tables and DELETE nowhere (`agentguard.audit.grants`, run after each migration). Replaces the earlier REVOKE script, which didn't hold when the app connected as the table owner (it could re-grant itself). Not applicable to SQLite dev/test
- [x] Multi-step workflow grouping and partial-completion reporting (THREAT_MODEL #18): agent-scoped `workflow_id`, console Workflows view listing partial tasks and the writes that took effect; the Claude agent and demo fleet label each ticket's handling as one workflow. Compensation stays a human decision (Phase 9)
- [x] Final threat-model pass: every row has a test case, including #17 (broker clock cross-checked against the gateway's, Phase 9) and #18

## Phase 6 — Operator console

- [x] Console API (`src/agentguard/console/`, mounted at `/v1/console`): overview metrics and decision time series, filterable activity log, per-call decision path with audit timeline, approvals queue, agent/tool registry views, policy versions, counterfactual simulation and replay, anomalies
- [x] Human sign-in separate from agent identity: bearer token → configured principal (roles + tenants; only token hashes configured). Approver identity comes from the principal, never headers; `*` read access never grants approval authority (adversarial tests in `tests/adversarial/test_console_approvals.py`)
- [x] Tenant-scoped reads (404 for other tenants' objects), request payloads hidden from `viewer` principals, simulation/replay on a database-enforced read-only session and limited to all-tenant readers
- [x] Append-only `operator_actions` table for console actions (approval decisions, suspend/resume), covered by the runtime-role grants
- [x] `/healthz` (liveness) and `/readyz` (database reachable) probes; the container healthcheck gates the console in Compose
- [x] Web console (`web/`): React + TypeScript + Vite, light/dark, responsive to phone width, live polling; nginx container with CSP and security headers that exposes only `/v1/console/*` (agent endpoints aren't reachable through it)
- [x] Synthetic traffic generator (`python -m agentguard.sdk.demo_traffic`) and a CI job for console typecheck, unit tests and build
- [x] OIDC sign-in for the console: JWTs verified against the IdP's keys (issuer, audience, expiry), roles filtered to an allowlist, tenants from a claim; Keycloak realm in `deploy/keycloak/`; PKCE public client in the web app. Static tokens remain for dev/break-glass (`CONSOLE_STATIC_TOKENS=false` disables them). Verified with signed test tokens; the Keycloak realm itself hasn't been run in this environment (needs Docker)
- [x] Retired the header-based `/v1/approvals/{id}/decide`; approvals go through the console route only (adversarial test keeps it gone)
- [x] Live updates over SSE (`/v1/console/stream`), read with fetch so the token never goes in a URL; tenant-scoped change events; polling drops to a one-minute fallback
- [x] Overview aggregates in SQL (GROUP BY counts, time buckets and completeness in the database; latency percentiles via `percentile_cont` on Postgres). CI now also runs the console, gateway, replay and adversarial suites against Postgres 16
- [x] Kubernetes Helm chart (`deploy/helm/agentguard`) — see Phase 8
- [x] Registry actions in the console: operators register agents, and publish a policy version from the simulation lab. Publishing re-simulates server-side, refuses loosening unless acknowledged with a reason, picks the version number itself (unique in the database), and logs to `operator_actions`. Unknown rule match keys are now rejected (a typo used to widen a rule silently). Tool registration stays code-reviewed: a tool is only as safe as its executor

## Phase 7 — Real agent, real target, learned risk

- [x] Helpdesk target service (`src/helpdesk/`): its own database and API, per-scope credentials (hashes only), tenant boundary enforced on real records, idempotency records for reconciliation, refund cap per order. Never imports `agentguard`
- [x] Executors with logical refs (`billing.refund`) and two backends: the real helpdesk over HTTP, or the in-process test double for tests and the evaluation gate (`TARGET_BACKEND`)
- [x] Claude-powered support agent (`agentguard.agents.support_agent`): tools map to gateway calls; tenant and environment come from the agent's own configuration; refusal fallback and prompt caching on; can wait for a human approval and then execute
- [x] Live red-team evaluation (`agentguard.evaluation.live_agent`): model attempts vs. executed violations vs. routine task success. Costs API tokens, so not in CI
- [x] Behavioural anomaly model (`agentguard.ml`): Isolation Forest + robust per-feature deviation over 15-minute windows, explained by top deviating features, advisory only (enforcement modules may not import it — tested); nightly retraining CronJob; console "Behavioural risk" panel
- [x] Agent registration from the CLI (`controls.cli register`)
- [ ] Run the live red-team evaluation with a real `ANTHROPIC_API_KEY` and record the results in `docs/EXPERIMENT.md`
- [ ] Train the anomaly model on enough real history (it refuses below `--min-windows`); today's traces span too short a period to mean anything

## Phase 8 — Deployment

- [x] Compose topology: gateway, separate broker, helpdesk with its own Postgres, Keycloak, console
- [x] Helm chart: per-role Deployments, pre-upgrade migration Job, HPA and PDBs, default-deny NetworkPolicies, non-root read-only containers, secrets only by reference to externally managed Secrets
- [x] CI renders and schema-validates the chart and builds both images
- [ ] Render the chart and run it on a real cluster (Helm wasn't available in this environment; CI covers lint and schema)
- [ ] In-cluster mTLS (service mesh) between gateway, broker and target

## Phase 9 — Closing the threat-model gaps

- [x] Two clocks: the broker reports its own timing on every execution; skew beyond tolerance is an audit event and an anomaly finding (THREAT_MODEL #17)
- [x] Workflow grouping and partial-completion reporting in the console (THREAT_MODEL #18)
- [x] Runtime database role separated from the schema owner; audit tables append-only for the runtime role, verified on Postgres (THREAT_MODEL #8)

## Status

Phases 0–9 are implemented and tested, including on Postgres 16. What's left needs something this environment doesn't have, and none of it blocks the system working correctly:

- **Live Claude red-team run** (Phase 7): needs an `ANTHROPIC_API_KEY` and costs API tokens; the harness is ready (`agentguard.evaluation.live_agent`).
- **Running on a real Kubernetes cluster** (Phase 8): CI lints and schema-validates the chart; no cluster was available.
- **In-cluster mTLS** (Phase 8): needs a service mesh in that cluster.

Each is called out in `docs/THREAT_MODEL.md` or above rather than left out silently.
