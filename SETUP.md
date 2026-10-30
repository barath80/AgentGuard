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
   source .venv/Scripts/activate       # Git Bash / WSL
   .venv\Scripts\Activate.ps1          # PowerShell
   .venv\Scripts\activate.bat          # cmd.exe (or just `.venv\Scripts\activate`)
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
6. API docs (Swagger UI) are at `http://localhost:8000/docs` while the server is running — the bare `http://localhost:8000/` redirects there too. There's no visible homepage; this is an API server, not a website, so step 5 (the mock agent, or `curl`/`/docs`) is how you confirm it's actually working, not just opening the root URL in a browser.

## Operator console (web UI)

The console lives in `web/` (React + TypeScript + Vite) and talks only to `/v1/console/*` on the gateway.

Native, alongside the gateway from Option A (Node 20+):

```
cp .env.example .env    # synthetic console principals; set DATABASE_URL=sqlite:///./agentguard.db for a native run
uvicorn agentguard.gateway.app:app --port 8000

cd web
npm ci
npm run dev             # http://localhost:5173, proxies /v1/console to :8000
```

Generate something to look at with `python -m agentguard.sdk.demo_traffic --rounds 3`. It registers two extra synthetic agents if they're missing, then sends routine work, gated refunds and attack attempts through the gateway over HTTP.

Sign in with a synthetic dev token (listed on the sign-in page in dev mode only):

| Token | Principal | Can |
|---|---|---|
| `dev-only-operator-token` | operator, all tenants | read everything, suspend/resume agents, simulate, replay. Cannot approve: all-tenant read access never grants approval authority |
| `dev-only-console-dana` | support-lead, tenant-a | see tenant-a only; approve/reject tenant-a refunds |
| `dev-only-console-sam` | viewer, all tenants | read everything except request payloads; simulate, replay |

To add a principal, hash its token with `python -c "from agentguard.console.auth import hash_token; print(hash_token('...'))"` and add `{"name", "roles", "tenants", "token_sha256"}` to `CONSOLE_PRINCIPALS`. Only hashes are configured; hand the token itself to the person out of band.

Console checks (all run in CI): `npm run typecheck`, `npm test`, `npm run build`.

## Full stack natively (gateway + broker + helpdesk)

The production shape, without Docker. Three terminals, venv active in each. On Windows, use `127.0.0.1` rather than `localhost`: each new connection to `localhost` tries IPv6 first and costs about 2 s, enough to trip the broker's 5 s deadline across two hops.

```
# 1. the helpdesk (ticketing + billing), with synthetic customers and tickets
python -m helpdesk.seed
uvicorn helpdesk.app:app --port 8200

# 2. the gateway, handing tool calls to the broker
TARGET_BACKEND=helpdesk BROKER_URL=http://127.0.0.1:8100 BROKER_SERVICE_TOKEN=dev-only-broker-service-token \
  uvicorn agentguard.gateway.app:app --port 8000

# 3. the broker: the only process with target credentials (the dev defaults match the helpdesk's)
TARGET_BACKEND=helpdesk HELPDESK_URL=http://127.0.0.1:8200 BROKER_SERVICE_TOKEN=dev-only-broker-service-token \
  uvicorn agentguard.broker.server:app --port 8100
```

Then `python -m agentguard.sdk.demo_traffic --rounds 3`. Its agents read real tickets and refund within the order amount; refunds land in the helpdesk's database (`helpdesk.db`). A hijacked intake agent tries cross-tenant reads, unexpected arguments and prod writes, and its circuit breaker opens.

## The Claude support agent

Needs `pip install -e ".[agent]"` and an `ANTHROPIC_API_KEY`. Register its identity once, then point it at tickets:

```
python -m agentguard.controls.cli register claude-support-agent@dev-workload-pool --tenant tenant-a --owner support-eng
python -m agentguard.agents.support_agent --ticket T-1003
python -m agentguard.agents.support_agent --ticket T-1003 --wait-for-approval 300   # approve it in the console meanwhile
```

It uses `claude-opus-5-5` with the API's server-side refusal fallback enabled (`fallbacks: "default"`) and prompt caching on. Override with `AGENT_MODEL` / `AGENT_EFFORT`.

The seeded helpdesk includes tickets whose bodies carry prompt injections (`python -c "import sqlite3; print(sqlite3.connect('helpdesk.db').execute(\"select id, subject from tickets where body like '%ignore your previous%' or body like '%assistant:%' or body like '%delete-production%'\").fetchall())"`). The live red-team evaluation runs the agent over them:

```
python -m agentguard.evaluation.live_agent --tickets T-1072,T-1073,T-1074 --routine T-1001,T-1002 --json live-agent-report.json
```

It reports what the model attempted separately from what executed, and exits non-zero if anything unsafe executed. It costs real tokens, so it isn't in CI.

## Behavioural anomaly model

Needs `pip install -e ".[ml]"`. Train it from recorded traces (read-only connection), then see risk per agent in the console's Insights page or on the command line:

```
python -m agentguard.ml.model train            # refuses with fewer than --min-windows (default 50) 15-minute windows
python -m agentguard.ml.model score
```

It's advisory: nothing on the enforcement path reads it. It learns "normal" from whatever traffic it's trained on, so train it on a period you trust.

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
3. Check it's up: `http://localhost:8000/docs` for the API, `http://localhost:8080` for the operator console (nginx; starts once the gateway's `/readyz` healthcheck passes). Compose also runs the broker, the helpdesk with its own Postgres, and Keycloak on `http://localhost:8081`: choose **Continue with single sign-on** and use `olivia` (operator), `dana` (support lead, tenant-a), `lee` (support lead, tenant-b) or `sam` (viewer), all with the synthetic password `dev-only-password`.
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
| `OPERATOR_TOKEN` | unset (`dev-only-operator-token` in `.env.example`) | Required for `/v1/stats`; unset disables the endpoint. Also signs a read-all `operator` principal into the console |
| `CONSOLE_PRINCIPALS` | unset | JSON list of console principals (`name`, `roles`, `tenants`, `token_sha256`). Malformed JSON signs nobody in |
| `PROPOSED_POLICIES_DIR` | `policies/proposed` | Proposed policies offered as starting points in the console's simulation lab |
| `TARGET_BACKEND` | `inprocess` | Where tools run: `helpdesk` (the real service) or `inprocess` (the test double used by tests and the evaluation gate) |
| `HELPDESK_URL` | `http://127.0.0.1:8200` | The helpdesk, as seen by whichever process runs executors |
| `BROKER_URL` / `BROKER_SERVICE_TOKEN` | unset | Set `BROKER_URL` on the gateway to run tool calls in the separate broker service; both sides share the token. Unset token on the broker = it refuses everything |
| `AGENTGUARD_CRED_<SCOPE>` | `dev-only-<scope>` | Target credentials, e.g. `AGENTGUARD_CRED_BILLING_REFUND`. Give them to the broker only |
| `OIDC_ISSUER` / `OIDC_CLIENT_ID` / `OIDC_JWKS_URL` | unset / `agentguard-console` / issuer's certs | Console SSO. Issuer must equal the tokens' `iss` |
| `OIDC_ALLOWED_ROLES` | `operator,viewer,support-lead` | IdP roles that mean anything here; everything else in the token is ignored |
| `CONSOLE_STATIC_TOKENS` | `true` | Static console tokens (dev / break-glass). Set `false` in production |
| `ANOMALY_MODEL_PATH` | `models/anomaly.joblib` | Where the trained anomaly model is written and read |
| `ANTHROPIC_API_KEY` | unset | Only for the Claude support agent and the live red-team evaluation |
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
- **`{"detail":"Not Found"}` when opening `http://localhost:8000/`**: not an error — this is an API server with no homepage. The root now redirects to `/docs`; if you still see this, restart the server to pick up the fix (it isn't hot-reloaded without `--reload`).
