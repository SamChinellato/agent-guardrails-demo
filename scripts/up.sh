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
export K3S_IMAGE K3S_DIGEST HOST_PORT_GATEWAY HOST_PORT_GRAFANA HOST_PORT_PANEL
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
kubectl apply -f "$ROOT/deploy/manifests/00-namespaces.yaml" >/dev/null
"$ROOT/scripts/gen-secrets.sh"

log "etcd"
sed "s|\${ETCD_REF}|$(pinned_ref ETCD)|" "$ROOT/deploy/manifests/10-etcd.yaml" | kubectl apply -f - >/dev/null
kubectl -n gateway rollout status statefulset/etcd --timeout=180s >/dev/null

# ---------------------------------------------------------------- built images
"$ROOT/scripts/build-images.sh"
# shellcheck source=/dev/null
source "$ROOT/.cache/image-tags.env"

# ---------------------------------------------------------------- workloads
log "ai services"
render_manifest() {
  sed -e "s|\${MOCK_LLM_IMAGE}|$REGISTRY_PULL/mock-llm:$MOCK_LLM_TAG|g" "$1"
}
kubectl -n ai create configmap mock-llm-scripts --from-file="$ROOT/scenarios/fixtures/mock-llm" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
render_manifest "$ROOT/deploy/manifests/30-mock-llm.yaml" | kubectl apply -f - >/dev/null
kubectl -n ai rollout status deployment/mock-llm --timeout=180s >/dev/null

log "apisix"
kubectl -n gateway create configmap apisix-custom-plugins --from-file="$ROOT/gateway/plugins" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
# subPath mounts don't follow ConfigMap updates: a checksum annotation rolls the pod on change
plugins_sha="$(cat "$ROOT"/gateway/plugins/*.lua | sha256sum | cut -c1-16)"
helm_install apisix gateway apisix APISIX "$ROOT/deploy/helm-values/apisix.yaml" \
  --set "image.tag=${APISIX_IMAGE##*:}@$APISIX_DIGEST" \
  --set "podAnnotations.demo/custom-plugins-sha=$plugins_sha"
kubectl -n gateway rollout status deployment/apisix --timeout=300s >/dev/null

# ---------------------------------------------------------------- gateway config
"$ROOT/scripts/bootstrap-gateway.sh"
"$ROOT/scripts/smoke.sh"

cat <<EOF

Demo is up (LLM_MODE=$LLM_MODE, runtime=$CONTAINER_RUNTIME).
  Gateway:     http://localhost:$HOST_PORT_GATEWAY
  KUBECONFIG:  $KUBECONFIG
  Secrets:     $ROOT/.secrets/ (mode 600)
EOF
