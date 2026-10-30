#!/usr/bin/env bash
# Allow the pods that call the Kubernetes API to reach it (workstream 2): the console
# (guard-policy ConfigMap, D-043), the guard's policy-sync sidecar (OS-05) and the OAuth
# proxies (OS-04, D-057). On OVN-Kubernetes that is an ipBlock per control-plane address
# on 6443, and the addresses differ per cluster, so
# this policy is applied here from the live `kubernetes` endpoints rather than kept in git
# (Argo CD leaves it alone: it carries no Argo tracking label). Idempotent.
set -euo pipefail
TARGET=openshift
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
require_openshift

ips="$(oc -n default get endpoints kubernetes -o jsonpath='{.subsets[*].addresses[*].ip}')"
port="$(oc -n default get endpoints kubernetes -o jsonpath='{.subsets[0].ports[0].port}')"
[[ -n "$ips" && -n "$port" ]] || die "cannot read the API server endpoints"
to="$(for ip in $ips; do printf '{"ipBlock":{"cidr":"%s/32"}},' "$ip"; done | sed 's/,$//')"

policy() { # policy NAMESPACE POD_SELECTOR_JSON
  cat <<EOF
{"apiVersion":"networking.k8s.io/v1","kind":"NetworkPolicy",
 "metadata":{"name":"apiserver-egress","namespace":"$1","labels":{"app.kubernetes.io/part-of":"ai-gateway-demo"}},
 "spec":{"podSelector":$2,"policyTypes":["Egress"],
         "egress":[{"to":[$to],"ports":[{"port":$port,"protocol":"TCP"}]}]}}
EOF
}
policy aigw-ai '{"matchExpressions":[{"key":"app","operator":"In","values":["console","guard"]}]}' | oc apply -f - >/dev/null
policy aigw-gateway '{"matchExpressions":[{"key":"app","operator":"In","values":["killswitch-oauth","apisix-oauth"]}]}' \
  | oc apply -f - >/dev/null
log "API server egress allowed to $ips:$port"
