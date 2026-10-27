# Setup

How to get AgentGuard running locally, either natively with Python or fully in Docker.

## Prerequisites

- Python 3.11+ (developed against 3.12)
- Docker Desktop (only needed for the Postgres/Redis/app Compose stack — the native setup below can run on SQLite without it)

## Option A — Native (Python venv + SQLite)

Fastest way to run the app and tests without Docker.

1. Create and activate a virtualenv:
   ```
   python -m venv .venv
   source .venv/Scripts/activate   # Windows Git Bash / PowerShell: .venv\Scripts\Activate.ps1
   ```
2. Install the project with dev dependencies:
   ```
   pip install -e ".[dev]"
   ```
3. Run the test suite:
   ```
   pytest
   ```
4. Start the gateway (SQLite file `agentguard.db` is created automatically, seeded with the mock agent/tools/policy on startup):
   ```
   uvicorn agentguard.gateway.app:app --port 8000
   ```
5. In another terminal (venv activated), run the mock agent end-to-end against it:
   ```
   python -m agentguard.sdk.mock_agent
   ```
   You should see `read-ticket`, `create-ticket`, and both `issue-refund` calls print results, with the over-threshold refund coming back as `approval_required`.
6. API docs (Swagger UI) are at `http://localhost:8000/docs` while the server is running.

## Option B — Docker Compose (Postgres + Redis + app)

Matches the target architecture in `docs/ARCHITECTURE.md`.

1. Make sure Docker Desktop is running. If a command below fails with something like
   `error during connect ... dockerDesktopLinuxEngine ... cannot find the file specified`,
   that means the Docker daemon itself isn't up yet — start Docker Desktop and retry.
2. From the repo root:
   ```
   docker compose up -d --build
   ```
   This starts Postgres, Redis, and the app (which runs `alembic upgrade head` then `uvicorn` on container start — see `Dockerfile`).
3. Check it's up: `http://localhost:8000/docs`.
4. Tear down: `docker compose down` (add `-v` to also drop the Postgres data volume).

## Environment variables

Copy `.env.example` to `.env` and adjust if needed — defaults work for both options above:

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://agentguard:agentguard@localhost:5432/agentguard` in `.env.example`; falls back to `sqlite:///./agentguard.db` if unset | Native/SQLite runs can just leave `.env` absent or unset this |
| `REDIS_URL` | `redis://localhost:6379/0` | Not consumed by any code path yet — runtime controls derive their state from the trace in the database |
| `ENVIRONMENT` | `development` | |
| `APPROVAL_DEFAULT_EXPIRY_SECONDS` | `900` | How long a pending approval stays valid before it expires |
| `KILL_SWITCH` | `false` | `true` denies every tool call for every agent (needs a restart; to stop one agent immediately, suspend it instead — see below) |
| `ALLOW_READS_WHEN_POLICY_UNAVAILABLE` | `false` | The only fail-open: if policy evaluation errors, still allow `read`-class tools. Writes are always denied |
| `BROKER_TIMEOUT_SECONDS` | `5` | Per-call deadline for tool execution |
| `BREAKER_THRESHOLD` / `BREAKER_WINDOW_SECONDS` | `5` / `300` | Denials within the window that open an agent's circuit |
| `OPERATOR_TOKEN` | unset (`dev-only-operator-token` in `.env.example`) | Required for `/v1/stats`; unset disables the endpoint |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | Enables OpenTelemetry trace export over OTLP/HTTP, e.g. `http://localhost:4318` |

Per-agent limits (`rate_limit_per_minute`, `daily_spend_limit`) are columns on the agent record, set when the agent is registered (see `register_agent` in `src/agentguard/policy/seed.py`).

## Database migrations

Schema is managed with Alembic (`migrations/`), driven off the SQLAlchemy models in `src/agentguard/models.py`.

- Apply migrations: `alembic upgrade head`
- After changing a model, generate a new migration: `alembic revision --autogenerate -m "describe the change"`, then review the generated file before applying it — autogenerate does not always get nullable/FK details right.

Note: running the app directly with `uvicorn` (Option A) also creates tables itself on startup via `Base.metadata.create_all` as a dev convenience and seeds the mock fixtures (`src/agentguard/policy/seed.py`). That path is for local iteration only — real deployments should rely on Alembic migrations, not on this auto-create behavior.

## Evaluation suite

Runs every routine and adversarial case from `docs/EVALUATION.md` against a throwaway database and prints the metrics (false-allow rate, false-deny rate, audit completeness, duplicate side-effect rate, latency):

```
python -m agentguard.evaluation.runner
python -m agentguard.evaluation.runner --json evaluation-report.json   # also save as JSON
```

Exits with code 1 if any attack succeeds or any routine case regresses. CI (`.github/workflows/ci.yml`) runs this on every push and PR, after lint and `pytest`.

## Replay and policy simulation

Both commands read recorded traces from `DATABASE_URL` (override with `--database-url`) over a read-only connection, and never execute a real tool.

1. Generate some traces first, e.g. start the gateway and run `python -m agentguard.sdk.mock_agent` (Option A above).
2. See how a proposed policy would change past decisions:
   ```
   python -m agentguard.replay.cli simulate --policy policies/proposed/v2-stricter-refunds.json
   ```
   Add `--fail-on-loosening` to exit with code 1 if the proposal would allow anything the current policy blocked or gated.
3. Check that recorded decisions reproduce under the policy versions they were made with:
   ```
   python -m agentguard.replay.cli replay
   ```

Proposed policies are JSON files in `policies/proposed/` — see the example file for the format (`version`, plus `rules` with `match`, `decision`, `priority`, and optional `approval_role`).

## Hardening the audit log (Postgres, production)

`agentguard.audit.service` only ever inserts into `audit_events` — there's no update/delete code path (checked by `tests/test_audit.py`). To make that DB-enforced too, not just a code convention, run `sql/postgres_audit_role.sql` against your production database after migrations. It revokes UPDATE/DELETE/TRUNCATE on `audit_events` from the app's own role. Not applicable to SQLite dev/test runs.

## Operator controls and monitoring

```
python -m agentguard.controls.cli anomalies                                     # read-only report
python -m agentguard.controls.cli suspend mock-support-agent@dev-workload-pool  # blocks its next call
python -m agentguard.controls.cli resume  mock-support-agent@dev-workload-pool
```

Stats for operators (needs `OPERATOR_TOKEN` set on the server):

```
curl -H "x-operator-token: dev-only-operator-token" http://localhost:8000/v1/stats
```

Tracing with Jaeger (Docker):

```
OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318 docker compose --profile observability up -d --build
```

Then open `http://localhost:16686` and pick the `agentguard-gateway` service. Each tool call is one trace: `agentguard.tool_call` → `agentguard.policy.evaluate` → `agentguard.broker.execute`. For a native run, start Jaeger the same way and set `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` before `uvicorn`.

## Linting

```
ruff check .
```

## Troubleshooting

- **"Cannot connect to the Docker daemon" / pipe not found errors**: Docker Desktop isn't running. Start it and retry — this isn't a problem with `docker-compose.yml` itself (`docker compose config` validates the file without needing the daemon up, and can be used to sanity-check syntax independent of whether Docker Desktop is running).
- **Port already in use (5432, 6379, or 8000)**: something else on your machine is using that port. Stop it, or change the published port in `docker-compose.yml` (left side of `"host:container"` in each service's `ports:`).
- **Stale SQLite state while iterating locally**: delete `agentguard.db` and restart the app — it gets recreated and reseeded automatically.
