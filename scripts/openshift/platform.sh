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

log "telemetry RBAC and the Loki writer's token"
# The token Secret's ServiceAccount lives in git (synced by Argo CD); create it here too
# if the first sync hasn't, so the token controller can fill the Secret
oc -n aigw-gateway create serviceaccount apisix-log-writer --dry-run=client -o yaml | oc apply -f - >/dev/null
oc apply -f "$ROOT/deploy/openshift/platform/telemetry.yaml" >/dev/null

"$ROOT/scripts/openshift/apiserver-egress.sh"
