#!/usr/bin/env bash
# Generate demo secrets into .secrets/ (gitignored, mode 600) and mirror them into
# Kubernetes Secrets (REP-09). Existing values are reused, so re-runs change nothing.
# Both targets get the same values from .secrets/; on OpenShift Argo CD never manages
# Secrets, so this runs before the first sync (OS-09).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DIR="$ROOT/.secrets"
mkdir -p "$DIR"; chmod 700 "$DIR"

rand_hex() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# secret NAME BYTES: print the stored value, generating it on first use
secret() {
  local f="$DIR/$1"
  if [[ ! -s "$f" ]]; then (umask 077; rand_hex "$2" >"$f"); fi
  chmod 600 "$f"
  cat "$f"
}

secret apisix-admin-key 24 >/dev/null
secret apisix-viewer-key 24 >/dev/null
for c in demo-user researcher executor probe; do secret "consumer-$c" 24 >/dev/null; done
for t in operator guard alertmanager; do secret "killswitch-$t-token" 24 >/dev/null; done
secret grafana-admin-password 16 >/dev/null
secret oauth-proxy-cookie 16 >/dev/null   # OpenShift OAuth proxies (OS-04): 32 characters

# apply_secret NAMESPACE NAME KEY=FILE...: create-or-update without printing values
apply_secret() {
  local ns="$1" name="$2"; shift 2
  local args=()
  for kv in "$@"; do args+=("--from-file=${kv%%=*}=$DIR/${kv#*=}"); done
  kubectl -n "$ns" create secret generic "$name" "${args[@]}" --dry-run=client -o yaml \
    | kubectl apply -f - >/dev/null
}

log "applying Kubernetes Secrets"
apply_secret "$NS_GATEWAY" apisix-admin admin=apisix-admin-key viewer=apisix-viewer-key
apply_secret "$NS_GATEWAY" consumer-keys \
  demo-user=consumer-demo-user researcher=consumer-researcher executor=consumer-executor probe=consumer-probe
apply_secret "$NS_GATEWAY" killswitch-tokens \
  operator=killswitch-operator-token guard=killswitch-guard-token alertmanager=killswitch-alertmanager-token
# Callers of the killswitch get only their own token (KS-09)
apply_secret "$NS_AI" killswitch-guard token=killswitch-guard-token
apply_secret "$NS_AI" killswitch-operator token=killswitch-operator-token   # presenter console (D-043)
apply_secret "$NS_OBS" killswitch-alertmanager token=killswitch-alertmanager-token
# Agents and the console carry their own consumer key (7.7, DEMO-01)
apply_secret "$NS_AI" agent-keys researcher=consumer-researcher executor=consumer-executor demo-user=consumer-demo-user
kubectl -n "$NS_OBS" create secret generic grafana-admin \
  --from-literal=admin-user=admin --from-file=admin-password="$DIR/grafana-admin-password" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
if [[ "$TARGET" == openshift ]]; then
  apply_secret "$NS_GATEWAY" oauth-proxy-cookie cookie-secret=oauth-proxy-cookie
fi
