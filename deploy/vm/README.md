# Deploying AgentGuard on one free cloud VM

This runs the whole system for real on a single virtual machine, at no cost on Oracle Cloud's Always Free tier: an Ampere A1 VM with 4 cores and 24 GB of memory. Any Ubuntu 22.04/24.04 host with at least 4 GB of memory works the same way.

| Piece | How it runs |
|---|---|
| Kubernetes | k3s (one node) with Traefik as the ingress and local-path storage. It enforces network policies. |
| HTTPS | cert-manager with Let's Encrypt certificates for `<name>.<ip>.sslip.io` hosts. These resolve to the VM's IP without any DNS setup. |
| Database | Postgres 16 in the cluster. Separate databases and roles for AgentGuard (owner + runtime role), the helpdesk and Keycloak. Nightly backups. |
| Sign-in | Keycloak in production mode with a realm that has no users. People are added with `add-operator.sh`, agents with `add-agent.sh`. |
| AgentGuard | The Helm chart (`deploy/helm/agentguard`): gateway ×2, broker ×2, helpdesk, console, migrations, drift check. |

Nothing is seeded except the tool registry and the baseline policy. There are no demo agents, customers or tickets: every record comes from real use.

## 1. Create the VM (Oracle Cloud console)

1. Sign up at cloud.oracle.com. Always Free needs a card for identity checks but isn't charged. Keep the account on the free tier: don't upgrade it to Pay As You Go.
2. **Compute → Instances → Create instance**:
   - **Image:** Canonical Ubuntu 24.04.
   - **Shape:** Ampere `VM.Standard.A1.Flex`, 4 OCPUs, 24 GB memory. If it says "out of capacity", try another availability domain or retry later; 2 OCPUs / 12 GB is plenty.
   - **SSH:** paste your public key, e.g. the contents of `~/.ssh/id_ed25519.pub`.
   - **Boot volume:** 100 GB (free up to 200 GB in total).
3. **Networking → the VM's subnet → Security list → Add ingress rules:** TCP 80 and TCP 443 from `0.0.0.0/0`. SSH (22) is open already.
4. Note the VM's **public IP**.

## 2. Bootstrap (once)

From your machine:

```
ssh ubuntu@<public-ip>
git clone <your fork of this repository> agentguard && cd agentguard
sudo bash deploy/vm/bootstrap.sh
exit                                  # log in again so the docker group applies
```

`bootstrap.sh` does the following:
- opens 80/443 in the VM's own firewall (Oracle images block everything but SSH);
- installs k3s, Helm, Docker (for image builds) and cert-manager.

## 3. Deploy

The trained model is a build output and isn't in git, so copy the one evaluated in the notebook to the VM first. From your machine:

```
scp models/anomaly.joblib ubuntu@<public-ip>:agentguard/models/
```

Then on the VM:

```
cd agentguard
ACME_EMAIL=you@example.com bash deploy/vm/deploy.sh
```

On the first run, `deploy.sh`:
1. Generates every password and credential with `openssl rand` into `~/.agentguard/secrets.env` (mode 600, outside the repository) and loads them into Kubernetes Secrets.
2. Starts Postgres and Keycloak.
3. Builds the images on the VM.
4. Installs the chart.
5. Copies the model onto the models volume.

It prints the URLs:

| | |
|---|---|
| Console | `https://agentguard.<ip-with-dashes>.sslip.io` |
| Agent API | `https://agents.<ip-with-dashes>.sslip.io/v1/tool-calls` |
| Sign-in / tokens | `https://sso.<ip-with-dashes>.sslip.io/realms/agentguard` |

Re-run `deploy.sh` to upgrade after `git pull`. It reuses the secrets and runs migrations before new pods start.

**Back up `~/.agentguard/secrets.env`** to a password manager. It holds the database passwords and the broker's target credentials, and it lives only on the VM.

## 4. Add people and agents

```
# First console user: an operator who sees every tenant.
bash deploy/vm/add-operator.sh alice alice@yourco.com operator '*'
# Someone who approves large refunds for one tenant.
bash deploy/vm/add-operator.sh bob bob@yourco.com support-lead acme

# An agent: a Keycloak client with a service account, registered for prod.
bash deploy/vm/add-agent.sh returns-agent acme support-eng 500
```

`add-agent.sh` prints the agent's environment, and the client secret appears only once. With those variables set, the bundled Gemini agent (or your own, through `agentguard.sdk`) authenticates with tokens:

```
export AGENTGUARD_URL=... AGENTGUARD_TOKEN_URL=... AGENTGUARD_CLIENT_ID=... AGENTGUARD_CLIENT_SECRET=...
export AGENT_TENANT=acme AGENT_ENVIRONMENT=prod GEMINI_API_KEY=...
python -m agentguard.agents.gemini_agent --ticket T-XXXXXXXXXX
```

The gateway accepts only tokens that pass every check:
- signed by this Keycloak;
- issued to that client's service account;
- audience `agentguard-gateway`;
- not expired.

The `x-workload-identity` header is ignored.

## 5. Real records

The helpdesk starts empty. Add customers and the tickets they open (or point your own intake at the helpdesk database):

```
kubectl -n agentguard exec deploy/agentguard-helpdesk -- python -m helpdesk.admin add-customer \
  --tenant acme --name "Ada Lovelace" --email ada@example.com
kubectl -n agentguard exec deploy/agentguard-helpdesk -- python -m helpdesk.admin open-ticket \
  --tenant acme --customer C-XXXXXXXX --order-amount 129.99 --subject "Arrived damaged" --body "..."
kubectl -n agentguard exec deploy/agentguard-helpdesk -- python -m helpdesk.admin list --tenant acme
```

Refunds can never exceed a ticket's order amount. Refunds above the policy threshold wait in the console's approvals queue for a `support-lead` of that tenant.

## Operating it

| Task | Command |
|---|---|
| Status | `kubectl get pods -A`, `kubectl get certificate -A` |
| Logs | `kubectl -n agentguard logs deploy/agentguard-gateway -f` |
| Suspend an agent now | `kubectl -n agentguard exec deploy/agentguard-gateway -- python -m agentguard.controls.cli suspend <client-id>` |
| Kill switch (all agents) | `helm upgrade agentguard deploy/helm/agentguard -n agentguard --reuse-values --set controls.killSwitch=true` |
| Backups | Nightly at 02:30 UTC to `/var/backups/agentguard` on the VM, kept 14 days. Copy them off the VM too, e.g. `rsync` from your machine or to free Object Storage. |
| Restore | `kubectl -n data exec -i postgres-0 -- pg_restore -U postgres -d <db> --clean < <file>.dump` |
| Keycloak admin | Not exposed publicly. Use `kubectl -n keycloak exec -it deploy/keycloak -- /opt/keycloak/bin/kcadm.sh ...` (see the add-*.sh scripts). |

## Limits of a one-VM install

- **One node is one failure domain.** Replicas survive a pod crash, not a VM outage. Backups are the recovery path.
- **Secrets live in Kubernetes Secrets on the VM,** generated there and never in git. With more than one operator, move them to a secrets manager (External Secrets Operator) as `deploy/helm/README.md` describes.
- **Let's Encrypt rate limits** apply per sslip.io host. For a lasting install, point your own domain at the VM and pass `BASE_DOMAIN=yourdomain.com` (with `agentguard.`, `sso.` and `agents.` records).
- **Oracle may reclaim idle Always Free VMs.** Real use keeps it active.
