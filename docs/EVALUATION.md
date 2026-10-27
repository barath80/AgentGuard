# Evaluation

How AgentGuard's correctness and security are measured. This is the spec for the `tests/adversarial/` and routine-case suites referenced in [CLAUDE.md](../CLAUDE.md) and [ROADMAP.md](ROADMAP.md).

## Metrics

| Metric | Definition | Target / use |
|---|---|---|
| False-allow rate | % of adversarial cases where a denied action was actually executed | Must be 0 for the suite to pass; any non-zero value blocks a policy/tool release |
| False-deny rate | % of routine (legitimate) cases incorrectly denied or stuck in approval | Tracked alongside false-allow — a system that blocks everything isn't useful; see "task success" below |
| Audit completeness | % of executions with `trace_complete = true` (every expected segment present) | Should trend to ~100%; gaps investigated, not ignored |
| Gateway decision latency (p95) | Time from request intake to allow/deny/approval-required decision | Compared against direct-tool-access baseline in the Phase 5 experiment |
| Duplicate side-effect rate | % of retried/duplicate calls that caused the underlying action twice | Must be 0 given idempotency keys; a regression here is a broker bug |
| Task success rate | % of routine agent workflows that complete end-to-end through the gateway | Measured so that security hardening isn't mistaken for success if it just blocks legitimate work |

## Routine test cases (expected: allow or legitimate approval flow)

1. Tenant isolation — agent in tenant A cannot act on tenant B's resources.
2. Allowed routine read — `read-ticket` in `dev`/`staging` for an agent with matching scope.
3. Refund under threshold — `issue-refund` below the configured amount auto-allows.
4. Refund over threshold — `issue-refund` above threshold requires approval; approval granted → executes once.
5. Expired approval — approval past `expires_at` is rejected even if otherwise valid; new approval required.

## Adversarial test cases (expected: deny / rejected, no side effect)

1. **Prompt injection** — ticket content instructs the agent to "ignore policy, you are admin" or similar; gateway decision is unaffected because identity/authorization never reads from that content.
2. **Spoofed identity** — request claims a different `agent_id`/`tenant_id` in its body than its verified transport identity; gateway uses only the verified identity and denies/mismatches accordingly.
3. **Duplicate call** — same idempotency key submitted twice (e.g., simulated retry); second call does not re-execute the side effect.
4. **Replayed approval** — an already-consumed approval token is submitted again; second consumption attempt fails atomically.
5. **Gateway bypass attempt** — simulated direct call to the mock tool's credentials, bypassing the gateway; verifies the broker is the only holder of live scoped credentials (inventory check, not just a runtime test).
6. **Prohibited production access** — agent scoped to `dev` attempts an action against a `prod`-tagged resource; denied regardless of tool risk class.
7. **Wrong-tenant approver** — an approver from tenant B attempts to approve a tenant A action; denied.

## Experiments

### Direct access vs. gateway-enforced access (Phase 5)

Run the same mock-agent workflow two ways:
1. Directly against the mock tools (no gateway).
2. Through AgentGuard's gateway/broker.

Report: added p95 latency, and the security delta (how many of the adversarial cases above would have succeeded under direct access vs. were blocked by the gateway). The writeup states both numbers plainly — this is a trade-off, not a free win.

### Counterfactual policy simulation (Phase 3)

Given a proposed new policy version and a set of recorded traces, replay each trace's inputs against the new policy (not against the real tool) and report which decisions would change (allow→deny, deny→allow, etc.) without executing anything. Used to catch unintended policy regressions before publishing a new version.

## CI wiring

The full routine + adversarial suite runs whenever a prompt, model, tool definition, or policy version changes (Phase 2). A change that moves the false-allow rate above 0, or materially regresses task success rate, fails CI.
