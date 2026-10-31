# AgentGuard
AI agents may read records, create tickets, call APIs or alter infrastructure. Teams need to know which authenticated agent requested an action, what policy applied, whether approval was required, and what actually happened. AgentGuard sits at the tool-execution boundary; it does not rely on the model's own promise to behave.

## What's in the box

- **Gateway** — intake, schema validation, identity from verified context, runtime controls, versioned policy, bound single-use approvals, a least-privilege execution broker, append-only audit.
- **Replay & simulation** — re-derive past decisions under the policy version that made them; test a proposed policy against recorded traffic and see security loosening and task-success regressions separately, before release.
- **Real target and broker** — a separate ticketing/billing service with its own database; a broker service that alone holds its credentials and re-verifies every execution; Kubernetes network policies so nothing else can reach it.
- **LLM support agent (Gemini or Claude)** — a real LLM agent working real tickets (some carrying prompt injections) through the gateway, with a live red-team evaluation that counts what the model attempted separately from what executed.
- **Behavioural risk model** — an advisory model that flags behaviour policy can't see (enumeration, refund draining), trained and evaluated in a Jupyter notebook (`notebooks/anomaly_model.ipynb`) that compares eight detectors, neural networks included, on labelled traffic with a time-based held-out test.
- **Policy shadow mode** — run a proposed policy beside the live one on real traffic and see every call it would have decided differently, before publishing; it can't change a live decision.
- **Scale-out** — a stateless gateway behind a load balancer, with concurrency-safe idempotency, budgets and rate limits; load-tested on Postgres, with results and sizing in [docs/PERFORMANCE.md](docs/PERFORMANCE.md).
- **Operator console** (`web/`) — live overview, activity log with each call's full decision path, approvals queue, agents and tools, policy simulation lab, anomaly findings and an operator action log.

## Quick start

```
docker compose up -d --build                      # Postgres, Redis, gateway (:8000), console (:8080)
python -m agentguard.sdk.demo_traffic --rounds 3  # synthetic traffic through the gateway
```

Open http://localhost:8080 and choose **Continue with single sign-on**. Keycloak runs a synthetic dev realm: `olivia` (operator), `dana` (support lead for tenant-a, can approve refunds) or `sam` (read-only viewer), password `dev-only-password`. Dev tokens (`dev-only-operator-token`, `dev-only-console-dana`, `dev-only-console-sam`) also work.

Production: [deploy/vm/README.md](deploy/vm/README.md) runs it on one free cloud VM (k3s, HTTPS, Postgres, Keycloak, token-authenticated agents, no demo data). For an existing cluster, see [deploy/helm/README.md](deploy/helm/README.md).

## Measured results

Every number below comes from a run in this repository, with its method and caveats in the linked document.

- **Enforcement:** the evaluation gate requires zero false allows across the routine and adversarial suites on every push. Against an agent holding the target credential directly, the gateway blocked or gated 5 of 5 attack scenarios ([EXPERIMENT.md](docs/EXPERIMENT.md)).
- **Behavioural model:** on held-out traffic, precision 0.92, recall 1.00 and 1.1% false alarms. It caught 97% of attack types left out of its training, against 17% for slow scraping before retraining ([EVALUATION.md](docs/EVALUATION.md)).
- **Concurrency:** load testing found seven defects that only appear under concurrent load, including duplicate execution, budget overspend and a deadlock above 40 concurrent requests. Each is fixed and pinned by a test that fails on the old code. Hot-path work took one instance from 25 to 72 calls/s at the same load, and a second gateway replica adds 1.7× ([PERFORMANCE.md](docs/PERFORMANCE.md)).

## Documentation

| | |
|---|---|
| [SETUP.md](SETUP.md) | Running it: native, Compose, the LLM agent, the model notebook, load tests |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layers, deployed topology, shadow mode, drift |
| [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) | Each threat, its mitigation, and the test that proves it |
| [docs/EVALUATION.md](docs/EVALUATION.md) | Test suites, metrics, the behavioural model's evaluation |
| [docs/PERFORMANCE.md](docs/PERFORMANCE.md) | Load testing, the defects it found, scale-out results, sizing |
| [docs/EXPERIMENT.md](docs/EXPERIMENT.md) | Direct access against gateway-enforced access |
| [docs/ROADMAP.md](docs/ROADMAP.md) | What's built, phase by phase, and what's left |
