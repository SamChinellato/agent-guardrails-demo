#!/usr/bin/env bash
# Pull every pinned chart, image and (live mode) the Ollama model ahead of time (REP-06).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
detect_runtime || die "no container runtime"
LLM_MODE="${LLM_MODE:-mock}"

"$ROOT/scripts/install-tools.sh"

log "charts"
for c in "apisix APISIX" "kube-prometheus-stack KPS" "tempo TEMPO" "loki LOKI" "opentelemetry-collector OTELCOL"; do
  set -- $c; echo "  $(basename "$(chart_tgz "$1" "$2")")"
done

log "images"
while IFS='=' read -r key _; do
  ref="$(pinned_ref "${key%_IMAGE}")"
  if runtime_cli image exists "$ref" 2>/dev/null || runtime_cli image inspect "$ref" >/dev/null 2>&1; then
    echo "  have $ref"
  else
    echo "  pull $ref"; runtime_cli pull -q "$ref" >/dev/null
  fi
done < <(grep -E '^[A-Z0-9_]+_IMAGE=' "$ROOT/versions.env")

if [[ "$LLM_MODE" == live ]]; then
  log "ollama model $OLLAMA_MODEL (host)"
  curl -sSf -m 1800 http://localhost:11434/api/pull -d "{\"model\":\"$OLLAMA_MODEL\",\"stream\":false}" >/dev/null
  digest="sha256:$(curl -sf http://localhost:11434/api/tags | jq -r --arg m "$OLLAMA_MODEL" '.models[]|select(.name==$m)|.digest')"
  [[ "$digest" == "$OLLAMA_MODEL_DIGEST" ]] || die "pulled $OLLAMA_MODEL has digest $digest, pinned $OLLAMA_MODEL_DIGEST"
fi
