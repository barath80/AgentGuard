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

- [ ] Routine test cases (see `docs/EVALUATION.md`)
- [ ] Adversarial test cases (prompt injection, spoofed identity, duplicate call, replayed approval, gateway bypass, prohibited prod access, wrong-tenant approver)
- [ ] Metrics collection: false-allow/false-deny rate, audit completeness, duplicate side-effect rate
- [ ] CI wiring to run the suite on policy/tool/prompt changes

## Phase 3 — Replay & counterfactual simulation

- [ ] Deterministic replay against mocks/read-only systems
- [ ] Counterfactual policy simulation (proposed policy vs. recorded traces, no execution)
- [ ] Task-success measurement alongside safety metrics

## Phase 4 — Resilience & observability

- [ ] Fail-closed behavior on policy-service unreachability (documented fallback scope)
- [ ] Rate limits/budgets/circuit breaker/kill switch
- [ ] Anomaly detection (repeated denials, call spikes, unusual sequences)
- [ ] OpenTelemetry instrumentation + dashboard
- [ ] Broker timeout/state reconciliation

## Phase 5 — Experiment & writeup

- [ ] Direct-access vs. gateway-enforced comparison (latency + security delta)
- [ ] Final threat-model pass: every row in `docs/THREAT_MODEL.md` has a corresponding test case

## Status

Phase 0 and Phase 1 complete. Next up: Phase 2 — adversarial & regression test suite wired into CI (the test cases themselves already exist under `tests/adversarial/`; CI wiring and the metrics dashboard are what's left).
