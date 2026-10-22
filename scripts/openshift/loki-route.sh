#!/usr/bin/env bash
# Close the Loki Operator's public Route to its gateway (D-063). The operator creates
# loki-aigw-observability.<domain> and recreates it if deleted, so the Route stays but the
# router admits no client address. The operator merges annotations, so this survives its
# reconciles; re-run it if the Route is recreated (a fresh LokiStack). The NetworkPolicy
# loki-gateway can't do this alone: the router runs on the host network, and OVN-Kubernetes
# lets a node's host network reach pods on that node. Idempotent.
set -euo pipefail
TARGET=openshift
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
require_openshift

if ! oc -n aigw-observability get route loki >/dev/null 2>&1; then
  log "no Loki Route yet (the LokiStack isn't synced); run $0 after Argo CD's first sync"
  exit 0
fi
oc -n aigw-observability annotate route loki haproxy.router.openshift.io/ip_allowlist=127.0.0.1/32 --overwrite >/dev/null
log "Loki Route closed (router allowlist admits no client)"
