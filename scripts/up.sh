#!/usr/bin/env bash
# Create or converge the whole demo (make up). Idempotent (REP-07): every step checks
# current state first, so a second run changes nothing.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

LLM_MODE="${LLM_MODE:-mock}"
[[ "$LLM_MODE" == mock || "$LLM_MODE" == live ]] || die "LLM_MODE must be mock or live"

"$ROOT/scripts/prereqs.sh" "$LLM_MODE" >/dev/null || { "$ROOT/scripts/prereqs.sh" "$LLM_MODE"; exit 1; }
detect_runtime || die "no container runtime"
llm_host_settings
export K3S_IMAGE K3S_DIGEST HOST_BIND HOST_PORT_GATEWAY HOST_PORT_GRAFANA HOST_PORT_PANEL \
  HOST_PORT_PROMETHEUS HOST_PORT_ALERTMANAGER HOST_PORT_EVIDENCE
export DEMO_ROOT="$ROOT"
mkdir -p "$ROOT/evidence"
# k3d's own helper images, pinned (REP-02)
export K3D_IMAGE_LOADBALANCER="$(pinned_ref K3D_PROXY)" K3D_IMAGE_TOOLS="$(pinned_ref K3D_TOOLS)"

# ---------------------------------------------------------------- registry + cluster
if k3d registry list -o json 2>/dev/null | jq -e --arg n "$REGISTRY_NAME" '.[] | select(.name == $n)' >/dev/null; then
  log "registry $REGISTRY_NAME exists"
else
  log "creating registry $REGISTRY_NAME"
  net=bridge; [[ "$CONTAINER_RUNTIME" == podman ]] && net=podman
  k3d registry create "${REGISTRY_NAME#k3d-}" --image "$(pinned_ref REGISTRY)" \
    --port "127.0.0.1:$REGISTRY_PORT" --default-network "$net"
fi

if k3d cluster list -o json 2>/dev/null | jq -e --arg n "$CLUSTER_NAME" '.[] | select(.name == $n)' >/dev/null; then
  if [[ "$(k3d cluster list -o json | jq -r --arg n "$CLUSTER_NAME" '.[] | select(.name == $n) | .serversRunning')" == 0 ]]; then
    log "starting stopped cluster $CLUSTER_NAME"; k3d cluster start "$CLUSTER_NAME" --wait
  else
    log "cluster $CLUSTER_NAME exists"
  fi
  # Port mappings are fixed at creation: refuse to silently keep a different bind/port
  want="$HOST_BIND:$HOST_PORT_GATEWAY $HOST_BIND:$HOST_PORT_GRAFANA $HOST_BIND:$HOST_PORT_PANEL"
  want+=" $HOST_BIND:$HOST_PORT_PROMETHEUS $HOST_BIND:$HOST_PORT_ALERTMANAGER $HOST_BIND:$HOST_PORT_EVIDENCE"
  have="$(runtime_cli port "k3d-$CLUSTER_NAME-serverlb" 2>/dev/null | awk '{print $3}' | sort | tr '\n' ' ')"
  for w in $want; do
    [[ " $have " == *" $w "* ]] || die "cluster ports ($have) differ from requested ($want). Run: make down && make up"
  done
else
  log "creating cluster $CLUSTER_NAME ($CONTAINER_RUNTIME, rootless=$RUNTIME_ROOTLESS)"
  extra=()
  if [[ "$RUNTIME_ROOTLESS" == true ]]; then
    extra+=(--k3s-arg '--kubelet-arg=feature-gates=KubeletInUserNamespace=true@server:*')
  fi
  [[ -n "$OLLAMA_HOST_IP" ]] && extra+=(--host-alias "$OLLAMA_HOST_IP:$OLLAMA_HOST_NAME")
  k3d cluster create --config "$ROOT/k3d/cluster.yaml" "${extra[@]}"
fi
mkdir -p "$(dirname "$KUBECONFIG")"
(umask 077; k3d kubeconfig get "$CLUSTER_NAME" >"$KUBECONFIG.tmp") && mv "$KUBECONFIG.tmp" "$KUBECONFIG"
wait_for "API server" 120 kubectl get --raw /readyz

# ---------------------------------------------------------------- base objects
kubectl apply -f "$ROOT/deploy/k3d/00-namespaces.yaml" >/dev/null
"$ROOT/scripts/gen-secrets.sh"

log "etcd"
sed "s|\${ETCD_REF}|$(pinned_ref ETCD)|" "$ROOT/deploy/base/gateway/10-etcd.yaml" | kubectl apply -f - >/dev/null
kubectl -n gateway rollout status statefulset/etcd --timeout=180s >/dev/null

# ---------------------------------------------------------------- observability (P2)
log "observability"
# kube-prometheus-stack first: it installs the ServiceMonitor/PrometheusRule CRDs the others use
helm_install kps observability kube-prometheus-stack KPS "$ROOT/deploy/helm-values/kube-prometheus-stack.yaml" \
  --set "prometheus.prometheusSpec.image.sha=${PROMETHEUS_DIGEST#sha256:}" \
  --set "alertmanager.alertmanagerSpec.image.sha=${ALERTMANAGER_DIGEST#sha256:}" \
  --set "prometheusOperator.image.sha=${PROM_OPERATOR_DIGEST#sha256:}" \
  --set "prometheusOperator.prometheusConfigReloader.image.sha=${PROM_CONFIG_RELOADER_DIGEST#sha256:}" \
  --set "grafana.image.sha=${GRAFANA_DIGEST#sha256:}" \
  --set "grafana.sidecar.image.sha=${K8S_SIDECAR_DIGEST#sha256:}"
helm_install otel-collector observability opentelemetry-collector OTELCOL "$ROOT/deploy/helm-values/opentelemetry-collector.yaml" \
  --set "image.tag=${OTELCOL_IMAGE##*:}" --set "image.digest=$OTELCOL_DIGEST"
helm_install tempo observability tempo TEMPO "$ROOT/deploy/helm-values/tempo.yaml" \
  --set "tempo.tag=${TEMPO_IMAGE##*:}@$TEMPO_DIGEST"
helm_install loki observability loki LOKI "$ROOT/deploy/helm-values/loki.yaml" \
  --set "loki.image.tag=${LOKI_IMAGE##*:}" --set "loki.image.digest=$LOKI_DIGEST"
"$ROOT/scripts/apply-observability.sh"
sed "s|\${PYTHON_BASE_REF}|$(pinned_ref PYTHON_BASE)|" "$ROOT/deploy/k3d/60-evidence-viewer.yaml" | kubectl apply -f - >/dev/null

# ---------------------------------------------------------------- built images
"$ROOT/scripts/build-images.sh"
# shellcheck source=/dev/null
source "$ROOT/.cache/image-tags.env"

# ---------------------------------------------------------------- workloads
log "ai services"
llm_model="demo"; [[ "$LLM_MODE" == live ]] && llm_model="$OLLAMA_MODEL"
render_manifest() {
  sed -e "s|\${MOCK_LLM_IMAGE}|$REGISTRY_PULL/mock-llm:$MOCK_LLM_TAG|g" \
      -e "s|\${GUARD_IMAGE}|$REGISTRY_PULL/guard:$GUARD_TAG|g" \
      -e "s|\${MCP_DOCS_IMAGE}|$REGISTRY_PULL/mcp-docs:$MCP_DOCS_TAG|g" \
      -e "s|\${MCP_ACTIONS_IMAGE}|$REGISTRY_PULL/mcp-actions:$MCP_ACTIONS_TAG|g" \
      -e "s|\${AGENTS_IMAGE}|$REGISTRY_PULL/agents:$AGENTS_TAG|g" \
      -e "s|\${CONSOLE_IMAGE}|$REGISTRY_PULL/console:$CONSOLE_TAG|g" \
      -e "s|\${KILLSWITCH_IMAGE}|$REGISTRY_PULL/killswitch:$KILLSWITCH_TAG|g" \
      -e "s|\${K8S_SIDECAR_IMAGE}|$K8S_SIDECAR_IMAGE|g" -e "s|\${K8S_SIDECAR_DIGEST}|$K8S_SIDECAR_DIGEST|g" \
      -e "s|\${LLM_MODEL}|$llm_model|g" \
      -e "s|\${HOST_PORT_GRAFANA}|$HOST_PORT_GRAFANA|g" "$1"
}
apply_cm() { # apply_cm NAMESPACE NAME SOURCE...
  local ns="$1" name="$2"; shift 2
  local args=(); for s in "$@"; do args+=("--from-file=$s"); done
  kubectl -n "$ns" create configmap "$name" "${args[@]}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}
apply_cm ai mock-llm-scripts "$ROOT/scenarios/fixtures/mock-llm"
apply_cm ai fixtures-documents "$ROOT/scenarios/fixtures/documents"
apply_cm ai fixtures-records "$ROOT/scenarios/fixtures/records.json"
# The live policy may differ from git after `make policy-set`; only seed it if missing
# (`make reset` restores it from policy/policy.yaml).
kubectl -n ai get configmap guard-policy >/dev/null 2>&1 || apply_cm ai guard-policy "$ROOT/policy/policy.yaml"
apply_cm ai guard-policy-baseline "$ROOT/policy/policy.yaml"   # what the console restores (D-043)
for m in 30-mock-llm 20-guard 21-mcp-servers 22-agents; do render_manifest "$ROOT/deploy/base/ai/$m.yaml" | kubectl apply -f - >/dev/null; done
# Network policies before the rollouts: the guard's policy-sync sidecar needs its API
# server egress (D-059), and on an existing cluster the old guard-egress would block it
log "network policies"
case "$CONTAINER_RUNTIME" in
  podman) ollama_egress="$OLLAMA_HOST_IP/32" ;;
  *) ip="$(kubectl -n kube-system get configmap coredns -o jsonpath='{.data.NodeHosts}' | awk '$2=="host.k3d.internal"{print $1}')"
     ollama_egress="${ip:-192.0.2.1}/32" ;;   # 192.0.2.1: TEST-NET, matches nothing
esac
apiserver="$(kubectl get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')/32"
sed -e "s|\${OLLAMA_EGRESS}|$ollama_egress|" -e "s|\${APISERVER_EGRESS}|$apiserver|" \
  "$ROOT/deploy/k3d/50-networkpolicies.yaml" | kubectl apply -f - >/dev/null
for d in mock-llm mcp-docs mcp-actions guard researcher executor console; do kubectl -n ai rollout status "deployment/$d" --timeout=300s >/dev/null; done
"$ROOT/scripts/verify-netpol.sh"

log "apisix"
for lua in "$ROOT"/gateway/plugins/*.lua; do
  kubectl -n gateway create configmap "apisix-plugin-$(basename "$lua" .lua)" --from-file="$lua" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
done
kubectl -n gateway delete configmap apisix-custom-plugins --ignore-not-found >/dev/null
# subPath mounts don't follow ConfigMap updates: a checksum annotation rolls the pod on change.
# wait-etcd: a new pod's NetworkPolicy allow can lag its start (D-036), so wait for etcd
# instead of crash-looping once.
plugins_sha="$(cat "$ROOT"/gateway/plugins/*.lua | sha256sum | cut -c1-16)"
wait_etcd="$(jq -cn --arg image "$(pinned_ref APISIX)" '[{
  name: "wait-etcd", image: $image,
  command: ["bash", "-c", "for i in $(seq 60); do (exec 3<>/dev/tcp/etcd.gateway.svc.cluster.local/2379) 2>/dev/null && exit 0; sleep 1; done; exit 1"],
  resources: {requests: {cpu: "10m", memory: "16Mi"}, limits: {cpu: "100m", memory: "64Mi"}},
  securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: ["ALL"]}}
}]')"
helm_install apisix gateway apisix APISIX "$ROOT/deploy/helm-values/apisix.yaml" \
  --set "image.tag=${APISIX_IMAGE##*:}@$APISIX_DIGEST" \
  --set "podAnnotations.demo/custom-plugins-sha=$plugins_sha" \
  --set-json "extraInitContainers=$wait_etcd"
kubectl -n gateway rollout status deployment/apisix --timeout=300s >/dev/null

# ---------------------------------------------------------------- gateway config
"$ROOT/scripts/bootstrap-gateway.sh"

log "killswitch"
render_manifest "$ROOT/deploy/base/gateway/40-killswitch.yaml" | kubectl apply -f - >/dev/null
kubectl -n gateway rollout status deployment/killswitch --timeout=180s >/dev/null

"$ROOT/scripts/smoke.sh"

lan=""
if [[ "$HOST_BIND" == 0.0.0.0 ]]; then
  ip="$( (hostname -I 2>/dev/null || ipconfig getifaddr en0 2>/dev/null) | awk '{print $1}')"
  [[ -n "$ip" ]] && lan="  From the LAN: http://$ip:$HOST_PORT_GATEWAY/console, http://$ip:$HOST_PORT_GRAFANA (Grafana), http://$ip:$HOST_PORT_PANEL (kill switch),
               http://$ip:$HOST_PORT_PROMETHEUS (Prometheus), http://$ip:$HOST_PORT_ALERTMANAGER (Alertmanager), http://$ip:$HOST_PORT_EVIDENCE (evidence)"
fi
cat <<EOF

Demo is up (LLM_MODE=$LLM_MODE, runtime=$CONTAINER_RUNTIME).
  Gateway:     http://localhost:$HOST_PORT_GATEWAY
  Grafana:     http://localhost:$HOST_PORT_GRAFANA  (anonymous viewer; admin password in .secrets/grafana-admin-password)
  Console:     http://localhost:$HOST_PORT_GATEWAY/console
  Kill switch: http://localhost:$HOST_PORT_PANEL  (operator token: cat .secrets/killswitch-operator-token)
  Prometheus:  http://localhost:$HOST_PORT_PROMETHEUS   Alertmanager: http://localhost:$HOST_PORT_ALERTMANAGER   Evidence: http://localhost:$HOST_PORT_EVIDENCE
  KUBECONFIG:  $KUBECONFIG
  Secrets:     $ROOT/.secrets/ (mode 600)
$lan
EOF
