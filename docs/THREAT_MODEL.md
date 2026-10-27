# Threat model

One entry per edge case from the project spec. Each entry states the mechanism AgentGuard uses to close it and which phase (see [ROADMAP.md](ROADMAP.md)) implements it. "Mitigated by design, not yet tested" is an honest intermediate state — the goal is for every row to eventually have both a mechanism and a corresponding case in [EVALUATION.md](EVALUATION.md).

| # | Edge case | Mechanism | Phase |
|---|---|---|---|
| 1 | Prompt injection tells the agent to ignore policy | Policy is enforced in the gateway/broker, entirely outside the LLM's context; retrieved/generated content is never treated as a source of identity or authorization | 1 (enforcement), 2 (adversarial test) |
| 2 | Spoofed `agent_id`/`tenant_id` in the request | Identity is derived from authenticated transport-layer context (verified workload identity), never from request body fields | 1 (enforcement), 2 (adversarial test) |
| 3 | Policy changes while an approval is pending | Approval row stores `policy_version` and `action_hash` at creation; execution re-validates both match the *current* active policy before consuming the approval | 1 |
| 4 | Approval token replay | Atomic one-time `consumed_at` write (single UPDATE with a status-guarded WHERE clause); a second consumption attempt fails | 1 (enforcement), 2 (adversarial test) |
| 5 | Tool succeeds but gateway times out | Broker reconciles actual target state before any retry; a timeout is never treated as "no side effect occurred" | 4 |
| 6 | Policy service outage | Fail closed for any action above the lowest risk class; only a narrowly-scoped, explicitly documented low-risk fallback (if any) is permitted when evaluation can't complete | 4 |
| 7 | Gateway bypass (agent calls the real tool directly) | Target credentials live only with the broker, scoped per-tool; direct paths are inventoried and closed off in deployment config, not assumed away | 1 (credential scoping), 4 (inventory/verification) |
| 8 | Audit tampering | Append-only application role (no UPDATE/DELETE grants on `executions`/audit tables); documented as reducing, not eliminating, risk from a privileged DB admin — no "unbreakable log" claim | 1 |
| 9 | Sensitive data in traces | `executions` stores only redacted fields plus `safe_request_ref`/`safe_result_ref` pointers/hashes; retention policy documented, access restricted | 1 |
| 10 | Duplicate tool call | Idempotency key per execution attempt; broker and (where possible) downstream system dedupe on it | 1 (enforcement), 2 (adversarial test) |
| 11 | Confused deputy (broker misuses its own privilege) | Broker only acts on a specific decision ID already produced by the policy engine; it never makes an independent authorization decision | 1 |
| 12 | Approval from wrong tenant/role | Approver identity and authority are checked server-side against the execution's tenant/role requirement, not trusted from the approval request itself | 1 (enforcement), 2 (adversarial test) |
| 13 | Schema drift on tool arguments | Tools are versioned (`schema_version`); unknown or unexpected high-risk fields are rejected by default rather than silently passed through | 1 |
| 14 | Agent loop / tool-call spam | Per-agent rate limits, budgets, deadlines, circuit breaker, and a kill switch | 4 |
| 15 | Model update changes agent behavior | Regression suite (routine + adversarial cases) required to pass before any prompt/model/tool/policy change ships | 2 |
| 16 | Replay accidentally triggers a real action | Replay/simulation runs only against mocks or read-only systems by default; there is no code path where replay calls the live broker with real credentials | 3 |
| 17 | Clock skew / missing trace segments | Correlate by request ID across both event time and ingestion time; `trace_complete` is explicitly computed and false when a segment is missing, rather than assumed true | 1 |
| 18 | Partial failure in a multi-step workflow | Explicit per-step state in `executions`; compensating actions only where they are actually safe; partial completion is reported as such, not rounded up to success | 4 |

## Non-claims

This system does not claim to be "the first AI agent security platform" or to produce an "unbreakable audit log." The audit mechanism reduces tampering risk through restricted write permissions and (optionally, later) external immutable storage — it does not claim to stop every privileged attacker, and that limitation should stay stated plainly in any writeup.
