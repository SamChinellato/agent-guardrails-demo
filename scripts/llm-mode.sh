#!/usr/bin/env bash
# Switch the gateway's LLM between the mock and the live model without a redeploy
# (make llm-mode MODE=mock|live, D-060). Only the gateway's llm upstream changes
# (gateway/upstreams/llm.<mode>.json); the agents call the gateway either way.
# - k3d: re-run the gateway bootstrap with LLM_MODE (live: the host Ollama, D-002). The
#   next `make up` applies its own LLM_MODE again.
# - OpenShift: set the llm-mode ConfigMap, which Argo CD creates once and then leaves alone,
#   and run a one-off copy of the gateway-bootstrap Job, which reads it. Later syncs re-run
#   the bootstrap with the same ConfigMap, so the switch stays.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
mode="${1:-}"
[[ "$mode" == mock || "$mode" == live ]] || die "usage: make llm-mode MODE=mock|live [TARGET=openshift]"

if [[ "$TARGET" != openshift ]]; then
  if [[ "$mode" == live ]]; then
    "$ROOT/scripts/prereqs.sh" live >/dev/null || { "$ROOT/scripts/prereqs.sh" live; exit 1; }
  fi
  LLM_MODE="$mode" "$ROOT/scripts/bootstrap-gateway.sh"
  log "LLM mode: $mode"
  exit 0
fi

if [[ "$mode" == live ]]; then
  oc -n "$NS_AI" rollout status deploy/llamacpp --timeout=10s >/dev/null 2>&1 \
    || die "llama.cpp in $NS_AI isn't ready (oc -n $NS_AI get pods -l app=llamacpp)"
fi
oc -n "$NS_GATEWAY" patch configmap llm-mode --type merge -p "{\"data\":{\"mode\":\"$mode\"}}" >/dev/null

# The bootstrap Job Argo CD last ran, as a new Job: same image, Secrets, mounts and pod
# label (the only label the Admin API's NetworkPolicy admits besides the kill switch)
job="gateway-bootstrap-llm-$(date +%s)"
oc -n "$NS_GATEWAY" get job gateway-bootstrap -o json \
  | jq --arg name "$job" '{apiVersion, kind,
        metadata: {name: $name, namespace: .metadata.namespace, labels: .metadata.labels},
        spec: {backoffLimit: 0, activeDeadlineSeconds: .spec.activeDeadlineSeconds, ttlSecondsAfterFinished: 600,
               template: {metadata: {labels: .spec.template.metadata.labels}, spec: .spec.template.spec}}}' \
  | oc apply -f - >/dev/null
log "applying the gateway config (Job $job)"
if ! oc -n "$NS_GATEWAY" wait --for=condition=complete "job/$job" --timeout=300s >/dev/null 2>&1; then
  oc -n "$NS_GATEWAY" logs "job/$job" --tail=20 >&2 || true
  die "the gateway bootstrap failed; llm-mode is now $mode but the gateway may still use the previous one"
fi
oc -n "$NS_GATEWAY" logs "job/$job" | grep -E "LLM_MODE|verified" | sed 's/^/  /' || true
log "LLM mode: $mode"
