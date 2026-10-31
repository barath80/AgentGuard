#!/usr/bin/env bash
# Deploy (or upgrade) AgentGuard on a VM prepared by bootstrap.sh. Run from the repository root:
#
#   ACME_EMAIL=you@example.com bash deploy/vm/deploy.sh
#
# Optional: BASE_DOMAIN (default <public-ip>.sslip.io, which resolves to the VM with no DNS setup),
# PUBLIC_IP (default: detected).
#
# Secrets are generated once into ~/.agentguard/secrets.env (mode 600, outside the repository) and
# loaded into Kubernetes Secrets; re-runs reuse them. Back that file up somewhere safe: it holds the
# database passwords and the broker's target credentials.
set -euo pipefail

: "${ACME_EMAIL:?set ACME_EMAIL, the email for certificate expiry notices}"
cd "$(git rev-parse --show-toplevel)"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

PUBLIC_IP="${PUBLIC_IP:-$(curl -sf https://api.ipify.org)}"
BASE_DOMAIN="${BASE_DOMAIN:-${PUBLIC_IP//./-}.sslip.io}"
export CONSOLE_HOST="agentguard.${BASE_DOMAIN}" SSO_HOST="sso.${BASE_DOMAIN}" AGENTS_HOST="agents.${BASE_DOMAIN}"
export ACME_EMAIL
ISSUER="https://${SSO_HOST}/realms/agentguard"

echo "== secrets"
SECRETS="$HOME/.agentguard/secrets.env"
if [ ! -f "$SECRETS" ]; then
  mkdir -p "$(dirname "$SECRETS")" && chmod 700 "$(dirname "$SECRETS")"
  umask 077
  {
    for name in POSTGRES_PASSWORD AGENTGUARD_OWNER_PASSWORD AGENTGUARD_APP_PASSWORD HELPDESK_DB_PASSWORD \
                KEYCLOAK_DB_PASSWORD KC_ADMIN_PASSWORD BROKER_SERVICE_TOKEN \
                CRED_TICKETS_READ CRED_TICKETS_WRITE CRED_BILLING_REFUND; do
      echo "$name=$(openssl rand -hex 32)"
    done
  } > "$SECRETS"
  echo "generated $SECRETS"
fi
# shellcheck disable=SC1090
source "$SECRETS"

apply_secret() {  # namespace name key=value...
  local ns=$1 name=$2; shift 2
  local args=()
  for kv in "$@"; do args+=(--from-literal="$kv"); done
  kubectl -n "$ns" create secret generic "$name" "${args[@]}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}
sha() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }

for ns in data keycloak agentguard; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done
PG="postgres.data.svc:5432"
apply_secret data postgres-credentials \
  POSTGRES_PASSWORD="$POSTGRES_PASSWORD" AGENTGUARD_OWNER_PASSWORD="$AGENTGUARD_OWNER_PASSWORD" \
  AGENTGUARD_APP_PASSWORD="$AGENTGUARD_APP_PASSWORD" HELPDESK_DB_PASSWORD="$HELPDESK_DB_PASSWORD" \
  KEYCLOAK_DB_PASSWORD="$KEYCLOAK_DB_PASSWORD"
apply_secret keycloak keycloak-credentials KC_DB_PASSWORD="$KEYCLOAK_DB_PASSWORD" KC_ADMIN_PASSWORD="$KC_ADMIN_PASSWORD"
apply_secret agentguard agentguard-database \
  DATABASE_URL="postgresql+psycopg://agentguard_app:${AGENTGUARD_APP_PASSWORD}@${PG}/agentguard" \
  MIGRATION_DATABASE_URL="postgresql+psycopg://agentguard:${AGENTGUARD_OWNER_PASSWORD}@${PG}/agentguard"
apply_secret agentguard agentguard-broker \
  BROKER_SERVICE_TOKEN="$BROKER_SERVICE_TOKEN" AGENTGUARD_CRED_TICKETS_READ="$CRED_TICKETS_READ" \
  AGENTGUARD_CRED_TICKETS_WRITE="$CRED_TICKETS_WRITE" AGENTGUARD_CRED_BILLING_REFUND="$CRED_BILLING_REFUND"
# The helpdesk stores only hashes of the broker's credentials.
apply_secret agentguard agentguard-helpdesk \
  HELPDESK_DATABASE_URL="postgresql+psycopg://helpdesk:${HELPDESK_DB_PASSWORD}@${PG}/helpdesk" \
  HELPDESK_SCOPE_TOKENS="{\"tickets:read\":\"$(sha "$CRED_TICKETS_READ")\",\"tickets:write\":\"$(sha "$CRED_TICKETS_WRITE")\",\"billing:refund\":\"$(sha "$CRED_BILLING_REFUND")\"}"

echo "== postgres"
kubectl apply -f deploy/vm/manifests/postgres.yaml >/dev/null
kubectl -n data rollout status statefulset/postgres --timeout=300s

echo "== certificates and keycloak ($SSO_HOST)"
envsubst '${ACME_EMAIL}' < deploy/vm/manifests/cluster-issuer.yaml | kubectl apply -f - >/dev/null
python3 deploy/vm/make_realm.py --console-origin "https://${CONSOLE_HOST}" > /tmp/agentguard-realm.json
kubectl -n keycloak create configmap agentguard-realm --from-file=agentguard-realm.json=/tmp/agentguard-realm.json \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
rm -f /tmp/agentguard-realm.json
envsubst '${SSO_HOST}' < deploy/vm/manifests/keycloak.yaml | kubectl apply -f - >/dev/null
kubectl -n keycloak rollout status deploy/keycloak --timeout=600s

echo "== images"
TAG="$(git rev-parse --short HEAD)-$(date -u +%Y%m%d%H%M%S)"
docker build -q -t "agentguard:${TAG}" .
docker build -q -t "agentguard-console:${TAG}" web
docker save "agentguard:${TAG}" "agentguard-console:${TAG}" | sudo k3s ctr images import - >/dev/null

echo "== agentguard ($CONSOLE_HOST, agents at $AGENTS_HOST)"
kubectl label namespace agentguard agentguard.io/agents=true --overwrite >/dev/null
helm upgrade --install agentguard deploy/helm/agentguard --namespace agentguard \
  -f deploy/vm/values-vm.yaml \
  --set image.tag="$TAG" --set consoleImage.tag="$TAG" \
  --set console.ingress.host="$CONSOLE_HOST" --set gateway.ingress.host="$AGENTS_HOST" \
  --set auth.oidc.issuer="$ISSUER" --set auth.oidc.browserOrigin="https://${SSO_HOST}" \
  --wait --timeout 10m

if [ -f models/anomaly.joblib ]; then
  echo "== behavioural model"
  # The model evaluated in notebooks/anomaly_model.ipynb goes on the models volume; the gateway
  # picks it up on the next read (it watches the file's modification time).
  kubectl -n agentguard delete pod model-loader --ignore-not-found >/dev/null
  kubectl -n agentguard run model-loader --image="agentguard:${TAG}" --image-pull-policy=Never --restart=Never \
    --overrides='{"spec":{"securityContext":{"runAsUser":10001,"runAsGroup":10001,"fsGroup":10001},
      "containers":[{"name":"model-loader","image":"agentguard:'"$TAG"'","imagePullPolicy":"Never",
      "command":["sleep","300"],"volumeMounts":[{"name":"models","mountPath":"/models"}]}],
      "volumes":[{"name":"models","persistentVolumeClaim":{"claimName":"agentguard-models"}}]}}' >/dev/null
  kubectl -n agentguard wait --for=condition=Ready pod/model-loader --timeout=120s >/dev/null
  kubectl -n agentguard cp models/anomaly.joblib model-loader:/models/anomaly.joblib.new
  kubectl -n agentguard exec model-loader -- mv /models/anomaly.joblib.new /models/anomaly.joblib
  kubectl -n agentguard delete pod model-loader --wait=false >/dev/null
else
  echo "(no models/anomaly.joblib: the console shows 'no model yet' until one is trained and copied)"
fi

cat <<EOF

Deployed.
  Console        https://${CONSOLE_HOST}
  Agent API      https://${AGENTS_HOST}/v1/tool-calls
  Token endpoint ${ISSUER}/protocol/openid-connect/token

Next:
  bash deploy/vm/add-operator.sh <username> <email> operator '*'      # first console user
  bash deploy/vm/add-agent.sh <client-id> <tenant> <owner>            # each agent
  kubectl -n agentguard exec deploy/agentguard-helpdesk -- python -m helpdesk.admin --help
Certificates take a minute or two to issue: kubectl get certificate -A
EOF
