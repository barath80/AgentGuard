# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## Git: no automatic commits

**Never run `git commit` or `git push` unless the user explicitly asks for it in that turn.** Completing a task, finishing a phase, or the user saying "looks good" is not consent to commit, however, once done at checkpoints, suggest commit messages to the user without mentioning the co-author. Routine file edits, scaffolding, running tests, and iterating on code are not commit triggers. If you think a commit makes sense, say so and wait — don't act on it.

## What this project is

AgentGuard is a runtime authorization, audit, and replay gateway for AI agent tool calls. The core claim it proves: **authorization must be enforced outside the model**, at the point a tool actually executes — not by trusting the agent's prompt or self-reported identity. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the layered design, [docs/DATA_MODEL.md](docs/DATA_MODEL.md) for schemas, [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) for the edge cases this system defends against, [docs/EVALUATION.md](docs/EVALUATION.md) for how it's measured, [docs/EXPERIMENT.md](docs/EXPERIMENT.md) for the direct-access-vs-gateway comparison, and [docs/ROADMAP.md](docs/ROADMAP.md) for build phases and current status.

## Non-negotiable invariants

- **Identity comes from authenticated context, never from model/agent-supplied arguments.** Any code path that reads `agent_id`/`tenant_id` out of a request body or prompt instead of the verified caller context is a bug, not a feature.
- **Fail closed.** If the policy service, database, or approval check is unreachable or ambiguous, the default decision is deny for any action above the lowest risk class. Never default-allow to keep a demo running.
- **Approvals are single-use and bound.** An approval is valid only for the exact action hash + resource + policy version it was issued against, and is consumed atomically exactly once. Don't add a "just check the token exists" shortcut.
- **No real credentials, ever.** Only synthetic data and mock credentials belong in this repo — in code, fixtures, `.env.example`, and docs. Real secrets go in a secrets manager outside the repo, never committed.
- **Policy enforcement is server-side only.** Nothing in the SDK/adapter layer should be trusted to self-enforce; the gateway and broker are the only places decisions are final.

## Running things locally

See [SETUP.md](SETUP.md) for full steps (native venv+SQLite or Docker Compose). Quick reference:

- `docker compose up -d --build` — starts Postgres + Redis + app.
- `pytest` — unit + integration tests.
- `ruff check .` — lint.
- `alembic upgrade head` — apply DB migrations.

## Code layout (as it's built out)

- `src/agentguard/gateway/` — tool-call intake, schema validation, identity resolution.
- `src/agentguard/identity/` — workload identity verification (OIDC/OAuth in later phases; mocked claims in Phase 1).
- `src/agentguard/policy/` — versioned rule evaluation (allow/deny/approval-required).
- `src/agentguard/approval/` — approval state machine (Postgres-backed).
- `src/agentguard/broker/` — scoped execution of approved tool calls, idempotency, timeout handling and reconciliation. The only package allowed to import `broker/credentials.py` (enforced by `tests/test_broker.py`).
- `src/agentguard/controls/` — runtime controls (kill switch, suspend, rate limit, budget, circuit breaker), anomaly report, operator CLI.
- `src/agentguard/mock_systems.py` — the fake target systems; they enforce credential scopes and idempotency like a real API would.
- `src/agentguard/audit/` — append-only decision/outcome log.
- `src/agentguard/sdk/` — client adapter agents use to call through the gateway; `demo_traffic.py` sends synthetic traffic for the console.
- `src/agentguard/agents/` — the LLM support agent: shared prompt/tools/gateway mapping in `common.py`, model loops for Gemini (`gemini_agent.py`) and Claude (`support_agent.py`). Acts only through the gateway; must never import the broker.
- `src/agentguard/ml/` — advisory behavioural anomaly model. No enforcement module (gateway, policy, approval, broker, controls, identity) may import it (enforced by `tests/test_ml.py`).
- `src/helpdesk/` — the ticketing/billing target service. A separate system: must never import `agentguard` (enforced by `tests/test_helpdesk.py`).
- `deploy/` — Helm chart, Keycloak realm, and `deploy/vm/` (single-VM production install: k3s, Postgres, Keycloak, scripts).
- `src/agentguard/console/` — operator console API (`/v1/console/*`). Human sign-in maps a token to a configured principal; approver identity comes from that principal, never from headers. Read models are tenant-scoped.
- `web/` — operator console UI (React + TypeScript + Vite), served by nginx in production, which exposes only `/v1/console/*`.
- `src/agentguard/evaluation/` — routine/adversarial cases and the metrics runner that gates CI.
- `src/agentguard/replay/` — read-only replay and counterfactual policy simulation. Must never import `agentguard.broker` or `agentguard.gateway.service` (enforced by `tests/test_replay.py`).
- `tests/` — unit tests mirror `src/agentguard/`; `tests/adversarial/` holds the attack-case suite (prompt injection, spoofed identity, replay, bypass, etc. — see [docs/EVALUATION.md](docs/EVALUATION.md)).

## Working style for this repo

- Don't add abstractions ahead of the phase that needs them (e.g., don't build Kafka plumbing in Phase 1 — Redis is the queue until throughput actually requires otherwise, per [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).
- When touching policy or approval logic, add/update the corresponding case in `tests/adversarial/` or the routine-case suite rather than only testing the happy path.
- Keep [docs/ROADMAP.md](docs/ROADMAP.md) checklist state in sync with what's actually implemented.
