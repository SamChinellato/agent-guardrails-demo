#!/usr/bin/env bash
# Apply gateway/ (config as code, GW-08) to APISIX through a deliberate Admin API
# port-forward (rule 6), then read everything back and fail on drift. The apply and the
# drift check are scripts/gateway-apply.sh, which the OpenShift gateway-bootstrap Job also
# runs (OS-10); this wrapper supplies k3d's Admin API access and template variables.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
detect_runtime || die "no container runtime"
llm_host_settings

export ADMIN_KEY GW="$ROOT/gateway" LLM_MODE="${LLM_MODE:-mock}" OLLAMA_MODEL OLLAMA_SEED OLLAMA_URL NS_AI NS_OBS NS_GATEWAY
# Loki on k3d: the single-binary chart, no authentication
export LOKI_URL="http://loki.$NS_OBS.svc.cluster.local:3100" LOKI_PUSH_PATH=/loki/api/v1/push LOKI_HEADERS="{}"
ADMIN_KEY="$(cat "$ROOT/.secrets/apisix-admin-key")"
export CONSUMER_KEY_DEMO_USER CONSUMER_KEY_RESEARCHER CONSUMER_KEY_EXECUTOR CONSUMER_KEY_PROBE
CONSUMER_KEY_DEMO_USER="$(cat "$ROOT/.secrets/consumer-demo-user")"
CONSUMER_KEY_RESEARCHER="$(cat "$ROOT/.secrets/consumer-researcher")"
CONSUMER_KEY_EXECUTOR="$(cat "$ROOT/.secrets/consumer-executor")"
CONSUMER_KEY_PROBE="$(cat "$ROOT/.secrets/consumer-probe")"

admin_port_forward  # sets ADMIN_URL, kills the port-forward on exit
export ADMIN_URL
export -f log die
"$ROOT/scripts/gateway-apply.sh"
