# Data model

Schema sketch for Phase 1. All tables live in Postgres. Field lists are the starting point for Alembic migrations, not a final DDL.

## `agents`

Agent registry — identity, ownership, and what environments it's allowed into.

| Field | Type | Notes |
|---|---|---|
| `id` | uuid, PK | |
| `name` | text | |
| `owner` | text | human/team accountable for this agent |
| `tenant_id` | uuid | |
| `workload_identity_ref` | text | reference to the external identity (OIDC subject, mTLS SAN) — never a secret itself |
| `status` | enum(`active`, `suspended`, `retired`) | |
| `approved_environments` | text[] | e.g. `{dev, staging}` — never `prod` for a brand-new agent by default |
| `risk_category` | enum(`low`, `medium`, `high`) | default risk posture, can be overridden per-tool in policy |
| `created_at`, `updated_at` | timestamptz | |

## `tools`

Tool registry — what an agent can call, and how risky it is.

| Field | Type | Notes |
|---|---|---|
| `id` | uuid, PK | |
| `name` | text | e.g. `issue-refund` |
| `schema_version` | text | semver; unknown/newer versions are rejected by default |
| `input_schema` | jsonb | JSON Schema; unknown high-risk fields are rejected, not ignored |
| `risk_class` | enum(`read`, `write-low`, `write-high`, `destructive`) | |
| `target_scope` | text | which system/resource class this touches, e.g. `tickets`, `billing`, `prod-db` |
| `executor_ref` | text | which broker executor handles this tool |
| `created_at`, `updated_at` | timestamptz | |

## `policies` + `policy_rules`

Versioned policy corpus.

**`policies`**

| Field | Type | Notes |
|---|---|---|
| `id` | uuid, PK | |
| `version` | int | monotonically increasing; immutable once published |
| `published_at` | timestamptz | |
| `scope` | text | e.g. `global`, or a specific tenant |
| `is_active` | boolean | exactly one active version per scope at a time |

**`policy_rules`**

| Field | Type | Notes |
|---|---|---|
| `id` | uuid, PK | |
| `policy_id` | uuid, FK → `policies.id` | |
| `match` | jsonb | predicate: tool, risk class, environment, tenant, agent risk category |
| `decision` | enum(`allow`, `deny`, `approval_required`) | |
| `approval_role` | text, nullable | required approver role when `decision = approval_required` |
| `priority` | int | first-match-wins ordering within a policy version |

Each policy version also has associated test cases (see [EVALUATION.md](EVALUATION.md)) that must pass before `is_active` is set.

## `executions` (traces)

One row per tool-call attempt, from intake to outcome.

| Field | Type | Notes |
|---|---|---|
| `id` (request ID) | uuid, PK | |
| `agent_id` | uuid, FK | derived from verified identity, not request body |
| `tenant_id` | uuid | |
| `tool_id` | uuid, FK | |
| `target_resource` | text | redacted/hashed if sensitive |
| `policy_version` | int | the version actually evaluated against |
| `decision` | enum(`allow`, `deny`, `approval_required`) | |
| `approval_id` | uuid, nullable, FK → `approvals.id` | |
| `idempotency_key` | text | |
| `requested_at`, `decided_at`, `executed_at`, `completed_at` | timestamptz | nullable until reached |
| `outcome` | enum(`success`, `failure`, `timeout`, `denied`, `not_attempted`) | |
| `safe_request_ref`, `safe_result_ref` | text, nullable | pointers/hashes to full payloads stored elsewhere (if at all) — never the raw sensitive payload inline |
| `trace_complete` | boolean | false if any expected segment (decision, execution, outcome) is missing — used for the audit-completeness metric |

## `approvals`

| Field | Type | Notes |
|---|---|---|
| `id` | uuid, PK | |
| `execution_id` | uuid, FK → `executions.id` | |
| `action_hash` | text | hash of (tool, resource, arguments, policy_version) — approval is void if any of these change |
| `resource` | text | |
| `policy_version` | int | |
| `approver` | text | must be verified server-side as having authority for this tenant/role |
| `status` | enum(`pending`, `approved`, `denied`, `expired`, `consumed`) | |
| `expires_at` | timestamptz | |
| `decided_at`, `consumed_at` | timestamptz, nullable | `consumed_at` set exactly once, atomically, at execution time |

## `evaluation_cases`

Used by the adversarial/regression suite (Phase 2), not by runtime decisions.

| Field | Type | Notes |
|---|---|---|
| `id` | uuid, PK | |
| `name` | text | |
| `category` | enum(`routine`, `adversarial`) | |
| `scenario` | jsonb | the input to replay through the gateway |
| `expected_decision` | enum(`allow`, `deny`, `approval_required`) | |
| `expected_outcome` | text | e.g. `no duplicate side effect`, `identity rejected` |

## Redaction policy

- Full ticket/customer payloads are never stored in `executions`; only `safe_request_ref`/`safe_result_ref` pointers or hashes.
- Synthetic data only in dev/test fixtures — no real customer data, ever, per [CLAUDE.md](../CLAUDE.md).
- Secrets (API keys, credentials) never appear in any table — only references to a secrets manager.
