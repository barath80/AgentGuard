# Deploying AgentGuard on Kubernetes

The chart in `agentguard/` runs the same topology as `docker-compose.yml`, hardened for a cluster:

| Workload | Role | Holds | Can reach |
|---|---|---|---|
| `gateway` (Deployment + HPA + PDB) | Agent API and console API | DB URL, broker service token | broker, database, IdP keys |
| `broker` (Deployment + PDB) | Runs authorized tool calls, re-verifying each one | Target credentials, broker service token | helpdesk (or your target), database |
| `helpdesk` (optional) | The ticketing/billing target | Its own DB URL, credential hashes | its database |
| `console` | Web UI (nginx) | nothing secret | gateway |
| `migrate` Job | `alembic upgrade head`, pre-install/pre-upgrade hook | DB URL | database |
| `train-anomaly` CronJob | Retrains the advisory anomaly model | DB URL (read-only connection) | database |

Network policies start from default-deny. The result: only the broker can reach the target system, and only the gateway can reach the broker. So "only the broker holds target credentials" is enforced at the network layer as well as in code (THREAT_MODEL #7).

## Secrets to create first

Create these from your secrets manager (External Secrets Operator, Secrets Store CSI, or `kubectl create secret` for a trial). None of them belong in values files or in git.

| Secret (values key) | Keys |
|---|---|
| `agentguard-database` (`database.existingSecret`) | `DATABASE_URL` |
| `agentguard-broker` (`broker.existingSecret`) | `BROKER_SERVICE_TOKEN`, `AGENTGUARD_CRED_TICKETS_READ`, `AGENTGUARD_CRED_TICKETS_WRITE`, `AGENTGUARD_CRED_BILLING_REFUND` |
| `agentguard-helpdesk` (`helpdesk.existingSecret`) | `HELPDESK_DATABASE_URL`, `HELPDESK_SCOPE_TOKENS` (JSON `{scope: sha256(credential)}` matching the broker's credentials) |
| `agentguard-console-tls` (`console.ingress.tls.secretName`) | TLS certificate, e.g. from cert-manager |

After the first migration, apply `sql/postgres_audit_role.sql` so the app's own database role can't update or delete audit rows.

## Install

```
helm upgrade --install agentguard deploy/helm/agentguard \
  --namespace agentguard --create-namespace \
  --set image.repository=<registry>/agentguard --set image.tag=<sha> \
  --set consoleImage.repository=<registry>/agentguard-console --set consoleImage.tag=<sha> \
  --set console.ingress.host=agentguard.example.com \
  --set auth.oidc.issuer=https://sso.example.com/realms/agentguard \
  --set auth.oidc.browserOrigin=https://sso.example.com
```

Label the namespaces where agents run so they can reach the gateway: `kubectl label namespace <ns> agentguard.io/agents=true`.

## Checks

```
helm lint deploy/helm/agentguard
helm template agentguard deploy/helm/agentguard | kubeconform -strict -summary -ignore-missing-schemas
```

CI runs both on every change (`.github/workflows/ci.yml`, `helm` job).

## Notes and limits

- **Traffic inside the cluster is plain HTTP.** Run a service mesh with mTLS (Istio, Linkerd) or terminate TLS per service if your threat model includes in-cluster sniffing. The broker's service token and its re-check of each execution still hold without a mesh.
- **NetworkPolicies need a CNI that enforces them** (Calico, Cilium, most managed clusters). On one that doesn't, they are silently ignored.
- **The model volume is `ReadWriteMany`.** Pick a storage class that supports it (EFS, Filestore, Azure Files), or set `anomalyModel.training.enabled=false` and the console shows "no model yet".
- **`database.egressCIDR` and `networkPolicies.idpEgressCIDR` default wide.** Narrow them to your managed Postgres subnet and your IdP.
