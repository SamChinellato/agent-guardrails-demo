#!/usr/bin/env bash
# Prove the OpenShift NetworkPolicies (workstream 2) from inside the pods: each flow the
# demo needs is open, and the flows they exist to stop are blocked. Uses the pods' own
# Python (no extra images, nothing changed). Fails on any mismatch.
set -euo pipefail
TARGET=openshift
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
require_openshift

# FROM_DEPLOYMENT HOST PORT EXPECTED (OPEN|blocked)
checks=(
  "researcher apisix-gateway.aigw-gateway.svc 80 OPEN"            # agents through the gateway (3)
  "researcher guard.aigw-ai.svc 8000 blocked"                     # only APISIX reaches the guard (7.3)
  "researcher mcp-docs.aigw-ai.svc 8000 blocked"                  # tools only through the guard (F3)
  "researcher apisix-admin.aigw-gateway.svc 9180 blocked"         # Admin API (GW-07)
  "researcher killswitch.aigw-gateway.svc 8000 blocked"
  "researcher etcd.aigw-gateway.svc 2379 blocked"
  "researcher 1.1.1.1 443 blocked"                                # no internet (NFR-08)
  "guard killswitch.aigw-gateway.svc 8000 OPEN"                   # auto-trip (F5)
  "guard mcp-docs.aigw-ai.svc 8000 OPEN"                          # inspecting proxy (F3)
  "guard apisix-admin.aigw-gateway.svc 9180 blocked"
  "guard 1.1.1.1 443 blocked"
  "guard kubernetes.default.svc 443 OPEN"                         # policy-sync sidecar (D-059), apiserver-egress
  "console kubernetes.default.svc 443 OPEN"                       # guard-policy (D-043), apiserver-egress
  "console aigw-prometheus.aigw-observability.svc 9090 OPEN"
  "console killswitch.aigw-gateway.svc 8000 OPEN"
  "console guard.aigw-ai.svc 8000 blocked"
  "mcp-actions 1.1.1.1 443 blocked"                               # the tool that sends email stays inside
)
failed=0
for c in "${checks[@]}"; do
  read -r from host port want <<<"$c"
  got="$(oc -n aigw-ai exec "deploy/$from" -- python -c "
import socket
try: socket.create_connection(('$host', $port), timeout=4).close(); print('OPEN')
except Exception: print('blocked')" 2>/dev/null | tail -1)"
  if [[ "$got" == "$want" ]]; then log "netpol: $from -> $host:$port $got"
  else echo "netpol: $from -> $host:$port is ${got:-unknown}, expected $want" >&2; failed=1; fi
done
(( failed == 0 )) || die "network policy check failed"
log "network policies verified (${#checks[@]} flows)"
