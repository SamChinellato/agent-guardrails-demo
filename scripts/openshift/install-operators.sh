#!/usr/bin/env bash
# Install the demo's OLM operators on OpenShift (OS-01, OS-08) as cluster-admin, before
# Argo CD takes over. Idempotent (REP-07): re-applies the same objects, approves only the
# install plan for each pinned CSV, and waits until every CSV has Succeeded.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
require_openshift

OS="$ROOT/deploy/openshift"

log "namespaces"
oc apply -f "$OS/namespaces.yaml" >/dev/null

log "operator subscriptions"
render_env "$OS/operators/operators.yaml" | oc apply -f - >/dev/null

# namespace|subscription|pinned CSV
subs=(
  "openshift-gitops-operator|openshift-gitops-operator|$GITOPS_OPERATOR_CSV"
  "openshift-cluster-observability-operator|cluster-observability-operator|$COO_CSV"
  "openshift-operators-redhat|loki-operator|$LOKI_OPERATOR_CSV"
  "openshift-tempo-operator|tempo-product|$TEMPO_OPERATOR_CSV"
  "openshift-opentelemetry-operator|opentelemetry-product|$OTEL_OPERATOR_CSV"
  "aigw-observability|grafana-operator|$GRAFANA_OPERATOR_CSV"
)

# approve_pinned NS CSV: approve the pending install plan in NS that installs exactly CSV.
# Plans for any other CSV (an upgrade) are left unapproved.
approve_pinned() {
  local ns="$1" csv="$2" plan
  plan="$(oc -n "$ns" get installplans -o json | jq -r --arg c "$csv" \
    '.items[] | select(.spec.approved == false and (.spec.clusterServiceVersionNames | index($c))) | .metadata.name' | head -1)"
  [[ -n "$plan" ]] || return 1
  oc -n "$ns" patch installplan "$plan" --type merge -p '{"spec":{"approved":true}}' >/dev/null
}

csv_phase() { oc -n "$1" get csv "$2" -o jsonpath='{.status.phase}' 2>/dev/null; }

for s in "${subs[@]}"; do
  IFS='|' read -r ns sub csv <<<"$s"
  # A CSV that exists at all was approved on an earlier run
  if [[ -n "$(csv_phase "$ns" "$csv")" ]]; then
    log "operator $csv present"; continue
  fi
  wait_for "install plan for $csv" 300 approve_pinned "$ns" "$csv"   # wait_for hides output
  log "approved install plan for $csv"
done

for s in "${subs[@]}"; do
  IFS='|' read -r ns sub csv <<<"$s"
  wait_for "operator $csv" 600 bash -c "[[ \"\$(oc -n '$ns' get csv '$csv' -o jsonpath='{.status.phase}' 2>/dev/null)\" == Succeeded ]]"
done
log "operators ready"
