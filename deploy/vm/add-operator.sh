#!/usr/bin/env bash
# Add a person who signs in to the console.
#
#   bash deploy/vm/add-operator.sh <username> <email> <role> <tenants>
#
# role: operator (reads all tenants, suspends agents), support-lead (approves gated refunds for
# its tenants) or viewer (read-only). tenants: comma-separated, or '*' for all (operators).
# Prints a one-time password; Keycloak makes the person choose their own at first sign-in.
set -euo pipefail

[ $# -eq 4 ] || { sed -n 2,9p "$0"; exit 2; }
user=$1 email=$2 role=$3 tenants=$4
case "$role" in operator|support-lead|viewer) ;; *) echo "unknown role $role" >&2; exit 2;; esac
tenants_json=$(python3 -c 'import json,sys; print(json.dumps([t.strip() for t in sys.argv[1].split(",") if t.strip()]))' "$tenants")
password=$(openssl rand -base64 18)

# kcadm runs inside the Keycloak pod, using the bootstrap admin password from the pod's own env.
kubectl -n keycloak exec -i deploy/keycloak -- bash -s -- "$user" "$email" "$role" "$tenants_json" "$password" <<'EOF'
set -euo pipefail
kc() { /opt/keycloak/bin/kcadm.sh "$@" --config /tmp/kcadm.config; }
kc config credentials --server http://localhost:8080 --realm master --user admin \
  --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null
kc create users -r agentguard -s username="$1" -s email="$2" -s enabled=true -s emailVerified=true \
  -s "attributes.tenants=$4" >/dev/null
kc set-password -r agentguard --username "$1" --new-password "$5" --temporary
kc add-roles -r agentguard --uusername "$1" --rolename "$3"
rm -f /tmp/kcadm.config
EOF

echo "added $user ($role, tenants $tenants_json)"
echo "one-time password: $password"
echo "give it to them over a private channel; they choose a new one at first sign-in"
