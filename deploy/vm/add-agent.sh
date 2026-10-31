#!/usr/bin/env bash
# Give an AI agent an identity and register it with AgentGuard.
#
#   bash deploy/vm/add-agent.sh <client-id> <tenant> <owner> [daily-spend-limit]
#
# Creates a confidential Keycloak client with a service account whose tokens carry the audience
# agentguard-gateway, then registers <client-id> as an agent approved for the prod environment.
# The client secret is printed once: put it in the agent's secrets manager as
# AGENTGUARD_CLIENT_SECRET, never in code or a repository.
set -euo pipefail

[ $# -ge 3 ] || { sed -n 2,10p "$0"; exit 2; }
client=$1 tenant=$2 owner=$3 limit=${4:-}
[[ "$client" =~ ^[a-z0-9][a-z0-9._-]{2,62}$ ]] || { echo "client id: lowercase letters, digits, . _ -" >&2; exit 2; }

secret=$(kubectl -n keycloak exec -i deploy/keycloak -- bash -s -- "$client" <<'EOF'
set -euo pipefail
kc() { /opt/keycloak/bin/kcadm.sh "$@" --config /tmp/kcadm.config; }
kc config credentials --server http://localhost:8080 --realm master --user admin \
  --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null
id=$(kc create clients -r agentguard -i -s clientId="$1" -s enabled=true -s protocol=openid-connect \
  -s publicClient=false -s serviceAccountsEnabled=true -s standardFlowEnabled=false \
  -s directAccessGrantsEnabled=false -s implicitFlowEnabled=false \
  -s 'protocolMappers=[{"name":"gateway-audience","protocol":"openid-connect","protocolMapper":"oidc-audience-mapper","config":{"included.custom.audience":"agentguard-gateway","access.token.claim":"true","id.token.claim":"false"}}]')
kc get "clients/$id/client-secret" -r agentguard --fields value --format csv --noquotes
rm -f /tmp/kcadm.config
EOF
)

args=(register "$client" --tenant "$tenant" --owner "$owner" --environments prod)
[ -n "$limit" ] && args+=(--daily-spend-limit "$limit")
kubectl -n agentguard exec deploy/agentguard-gateway -- python -m agentguard.controls.cli "${args[@]}"

cat <<EOF

Agent environment (the secret is shown only now):
  AGENTGUARD_URL=https://$(kubectl -n agentguard get ingress agentguard-gateway -o jsonpath='{.spec.rules[0].host}')
  AGENTGUARD_TOKEN_URL=$(kubectl -n agentguard get deploy agentguard-gateway -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="OIDC_ISSUER")].value}')/protocol/openid-connect/token
  AGENTGUARD_CLIENT_ID=$client
  AGENTGUARD_CLIENT_SECRET=$secret
  AGENT_TENANT=$tenant
  AGENT_ENVIRONMENT=prod
For the bundled Gemini support agent, also set GEMINI_API_KEY, then:
  python -m agentguard.agents.gemini_agent --ticket <ticket id from helpdesk.admin>
EOF
