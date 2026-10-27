# Roadmap

Phase checklist for AgentGuard. Keep this in sync with actual implementation state — check items off as they land, don't batch-update at the end.

## Phase 0 — Scaffolding

- [x] `CLAUDE.md` with no-auto-commit, fail-closed, and identity-boundary rules
- [x] `docs/ARCHITECTURE.md`
- [x] `docs/DATA_MODEL.md`
- [x] `docs/THREAT_MODEL.md`
- [x] `docs/EVALUATION.md`
- [x] `docs/ROADMAP.md` (this file)
- [ ] Repo skeleton: `src/agentguard/{gateway,identity,policy,approval,broker,audit,sdk}`, `tests/`
- [ ] `docker-compose.yml` (Postgres + Redis)
- [ ] `.env.example`, `.gitignore`, `pyproject.toml`/`requirements.txt`

## Phase 1 — Core authorization loop (MVP)

- [ ] Agent registry (model + CRUD)
- [ ] Tool registry (model + CRUD), 3 mock tools: `read-ticket`, `create-ticket`, `issue-refund`
- [ ] Policy model + versioning, first rule set
- [ ] Gateway endpoint: intake → schema validation → identity derivation (mocked) → policy evaluation → decision
- [ ] Approval state machine: create, bind (action hash + resource + policy version), expire, one-time atomic consumption
- [ ] Execution broker: scoped mock credentials, timeout, idempotency key
- [ ] Append-only audit log of every decision + outcome
- [ ] One mock support agent exercising all 3 tools end-to-end
- [ ] Alembic migrations for Phase 1 schema

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

Currently in **Phase 0**. Next up: repo skeleton + Docker Compose, then Phase 1 core loop.
