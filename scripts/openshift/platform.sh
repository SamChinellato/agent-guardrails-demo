#!/usr/bin/env bash
# What Argo CD doesn't manage on OpenShift (OS-09, OS-11), applied as cluster-admin:
# the demo namespaces, the image-puller binding on aigw-build, cluster-scoped telemetry
# RBAC with the Loki writer's token Secret, and the API server egress policy (per-cluster
# addresses). Run after scripts/openshift/install-operators.sh and before the first sync.
# Idempotent.
set -euo pipefail
TARGET=openshift
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
require_openshift

log "namespaces and image pullers"
oc apply -f "$ROOT/deploy/openshift/namespaces.yaml" >/dev/null
oc apply -f "$ROOT/deploy/openshift/build/image-pullers.yaml" >/dev/null

log "telemetry RBAC and the gateway tokens (Loki writer, Loki and Tempo readers)"
# The token Secrets' ServiceAccounts live in git (synced by Argo CD); create them here too
# if the first sync hasn't, so the token controller can fill the Secrets
for sa in aigw-gateway/apisix-log-writer aigw-observability/loki-reader aigw-observability/tempo-reader; do
  oc -n "${sa%/*}" create serviceaccount "${sa#*/}" --dry-run=client -o yaml | oc apply -f - >/dev/null
done
oc apply -f "$ROOT/deploy/openshift/platform/telemetry.yaml" >/dev/null

"$ROOT/scripts/openshift/apiserver-egress.sh"
