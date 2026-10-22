#!/usr/bin/env bash
# Load Grafana datasources and dashboards (sidecar ConfigMaps), alert rules and the demo
# ServiceMonitors from git (OBS-05, KS-10). Idempotent: kubectl apply of rendered objects.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
OBS="$ROOT/observability"

apply_cm() { # apply_cm NAME LABEL FILE...
  local name="$1" label="$2"; shift 2
  local args=(); for f in "$@"; do args+=("--from-file=$f"); done
  kubectl -n observability create configmap "$name" "${args[@]}" --dry-run=client -o yaml \
    | kubectl label --local -f - "$label=1" -o yaml | kubectl apply -f - >/dev/null
}

apply_cm grafana-datasources grafana_datasource "$OBS/datasources.yaml"
shopt -s nullglob
for d in "$OBS"/dashboards/*.json; do
  apply_cm "dashboard-$(basename "$d" .json)" grafana_dashboard "$d"
done
for r in "$OBS"/alerts/*.yaml; do kubectl apply -f "$r" >/dev/null; done
kubectl apply -f "$ROOT/deploy/k3d/40-servicemonitors.yaml" >/dev/null
