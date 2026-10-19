# Shared helpers for scripts/*.sh. Source it; do not execute.
# Sets ROOT, loads versions.env, puts .tools/bin first on PATH, detects the container
# runtime (Docker or rootless Podman, D-009) and derives runtime-specific settings.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../versions.env
source "$ROOT/versions.env"

# Per-machine overrides (gitignored), e.g. HOST_PORT_GRAFANA=3001 when 3000 is taken.
# Values already set in the environment win.
if [[ -f "$ROOT/.env.local" ]]; then
  while IFS='=' read -r k v; do
    [[ "$k" =~ ^[A-Z_][A-Z0-9_]*$ && -z "${!k:-}" ]] && export "$k=$v"
  done < <(grep -E '^[A-Z_][A-Z0-9_]*=' "$ROOT/.env.local")
fi

export PATH="$ROOT/.tools/bin:$PATH"
# Keep the demo's kubeconfig out of ~/.kube/config
export KUBECONFIG="${KUBECONFIG_DEMO:-$ROOT/.kube/config}"

CLUSTER_NAME="ai-gateway-demo"
REGISTRY_NAME="k3d-registry.localhost"   # k3d prefixes "k3d-" to "registry.localhost"
REGISTRY_PORT=5000
REGISTRY_PUSH="127.0.0.1:$REGISTRY_PORT" # host side; *.localhost may resolve to ::1 only
REGISTRY_PULL="$REGISTRY_NAME:$REGISTRY_PORT"

# Host ports (7.1). Spec defaults; override when a port is taken on the host (D-011).
# HOST_BIND: address the gateway, Grafana and panel ports listen on. 0.0.0.0 makes the demo
# reachable from the local network (D-023); 127.0.0.1 keeps it on this machine only.
HOST_BIND="${HOST_BIND:-0.0.0.0}"
HOST_PORT_GATEWAY="${HOST_PORT_GATEWAY:-8080}"
HOST_PORT_GRAFANA="${HOST_PORT_GRAFANA:-3000}"
HOST_PORT_PANEL="${HOST_PORT_PANEL:-8090}"

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# version_ge A B: true when A >= B (leading "v" ignored)
version_ge() { [[ "$(printf '%s\n%s\n' "${2#v}" "${1#v}" | sort -V | head -1)" == "${2#v}" ]]; }

# detect_runtime: sets CONTAINER_RUNTIME (docker|podman) and RUNTIME_ROOTLESS (true|false).
# For podman it exports DOCKER_HOST/DOCKER_SOCK so k3d talks to podman's Docker-compatible API.
# Honors a preset CONTAINER_RUNTIME. Returns 1 if no usable runtime is found.
detect_runtime() {
  local rt="${CONTAINER_RUNTIME:-}"
  if [[ -z "$rt" ]]; then
    if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 \
       && ! docker version 2>/dev/null | grep -qi podman; then
      rt=docker
    elif command -v podman >/dev/null 2>&1; then
      rt=podman
    else
      return 1
    fi
  fi
  CONTAINER_RUNTIME="$rt"
  RUNTIME_ROOTLESS=false
  if [[ "$rt" == podman ]]; then
    RUNTIME_ROOTLESS="$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null || echo false)"
    local sock
    if [[ "$RUNTIME_ROOTLESS" == true ]]; then
      sock="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"
    else
      sock="/run/podman/podman.sock"
    fi
    export DOCKER_HOST="unix://$sock" DOCKER_SOCK="$sock"
  fi
  export CONTAINER_RUNTIME RUNTIME_ROOTLESS
}

# runtime_cli: run the native CLI of the detected runtime (podman or docker)
runtime_cli() { "$CONTAINER_RUNTIME" "$@"; }

# Host Ollama as seen from pods (D-002, D-009). Docker: k3d injects host.k3d.internal.
# Rootless Podman: that name points at the rootless netns gateway, not the host; pasta maps
# the host at 169.254.1.2 (podman >= 5.3), which k3d injects into CoreDNS via --host-alias.
llm_host_settings() {
  if [[ "$CONTAINER_RUNTIME" == podman ]]; then
    OLLAMA_HOST_NAME="host.containers.internal"
    OLLAMA_HOST_IP="${OLLAMA_HOST_IP:-169.254.1.2}"
  else
    OLLAMA_HOST_NAME="host.k3d.internal"
    OLLAMA_HOST_IP=""   # resolved after cluster creation from the CoreDNS NodeHosts entry
  fi
  OLLAMA_URL="http://$OLLAMA_HOST_NAME:11434"
  export OLLAMA_HOST_NAME OLLAMA_HOST_IP OLLAMA_URL
}

# pinned_ref NAME: "<image>@<digest>" for NAME_IMAGE / NAME_DIGEST in versions.env
pinned_ref() {
  local i="${1}_IMAGE" d="${1}_DIGEST"
  [[ -n "${!i:-}" && -n "${!d:-}" ]] || die "versions.env has no pinned $1 image"
  echo "${!i}@${!d}"
}

# git_tag SERVICE_DIR: image tag for a built service (REP-03). The git short SHA, plus a
# content hash of the service sources when they differ from that commit, so a rebuild of
# uncommitted work never reuses a stale tag (D-012).
git_tag() {
  local sha dirty
  sha="$(git -C "$ROOT" rev-parse --short=8 HEAD 2>/dev/null || echo nogit)"
  dirty="$(git -C "$ROOT" status --porcelain -- "$@" 2>/dev/null)"
  if [[ -n "$dirty" ]]; then
    sha="$sha-$( (cd "$ROOT" && find "$@" -type f -not -path '*/.venv/*' -not -path '*/__pycache__/*' -print0 \
      | sort -z | xargs -0 sha256sum) | sha256sum | cut -c1-8)"
  fi
  echo "$sha"
}

# wait_for DESCRIPTION TIMEOUT_SECONDS CMD...: poll CMD every second until it succeeds (rule 8)
wait_for() {
  local desc="$1" timeout="$2"; shift 2
  local deadline=$(( SECONDS + timeout ))
  until "$@" >/dev/null 2>&1; do
    (( SECONDS < deadline )) || die "timed out after ${timeout}s waiting for $desc"
    sleep 1
  done
}

# chart_tgz NAME KEY: path to the pinned chart archive, downloading it into the prefetch
# cache on first use. KEY selects KEY_CHART_REPO / KEY_CHART_VERSION from versions.env.
chart_tgz() {
  local name="$1" repo="${2}_CHART_REPO" ver="${2}_CHART_VERSION" dir="$ROOT/.prefetch/charts"
  local tgz="$dir/$name-${!ver}.tgz"
  if [[ ! -s "$tgz" ]]; then
    mkdir -p "$dir"
    helm pull "$name" --repo "${!repo}" --version "${!ver}" -d "$dir" >/dev/null
  fi
  echo "$tgz"
}

# helm_install RELEASE NAMESPACE CHART KEY VALUES_FILE [helm args...]
# Installs the pinned chart; skips the upgrade when chart, values and args are unchanged
# since the last successful install, so `make up` twice is a no-op (REP-07).
helm_install() {
  local release="$1" ns="$2" chart="$3" key="$4" values="$5"; shift 5
  local tgz want have status
  tgz="$(chart_tgz "$chart" "$key")"
  want="$( { sha256sum "$tgz" "$values" | awk '{print $1}'; printf '%s\n' "$@"; } | sha256sum | cut -c1-16)"
  have="$(kubectl -n "$ns" get configmap demo-helm-state -o jsonpath="{.data.$release}" 2>/dev/null || true)"
  status="$(helm -n "$ns" status "$release" -o json 2>/dev/null | jq -r '.info.status' || true)"
  if [[ "$want" == "$have" && "$status" == deployed ]]; then
    log "helm release $release unchanged"
    return 0
  fi
  log "helm upgrade --install $release ($chart ${tgz##*-})"
  helm upgrade --install "$release" "$tgz" -n "$ns" -f "$values" "$@" --wait --timeout 10m >/dev/null
  kubectl -n "$ns" get configmap demo-helm-state >/dev/null 2>&1 \
    || kubectl -n "$ns" create configmap demo-helm-state >/dev/null
  kubectl -n "$ns" patch configmap demo-helm-state --type merge -p "{\"data\":{\"$release\":\"$want\"}}" >/dev/null
}

# admin_port_forward: deliberate port-forward to the APISIX Admin API (rule 6, GW-07).
# Sets ADMIN_URL and TMP (scratch dir); both are cleaned up when the script exits.
admin_port_forward() {
  TMP="$(mktemp -d)"
  kubectl -n gateway port-forward svc/apisix-admin :9180 >"$TMP/pf.log" 2>&1 &
  ADMIN_PF_PID=$!
  trap 'kill "$ADMIN_PF_PID" 2>/dev/null || true; rm -rf "$TMP"' EXIT
  local port=""
  wait_for "admin port-forward" 30 grep -q 'Forwarding from 127.0.0.1:' "$TMP/pf.log"
  port="$(sed -n 's/^Forwarding from 127.0.0.1:\([0-9]*\).*/\1/p' "$TMP/pf.log" | head -1)"
  ADMIN_URL="http://127.0.0.1:$port"
  wait_for "admin API" 30 curl -s -o /dev/null "$ADMIN_URL/apisix/admin/routes"
}

# os_arch: e.g. LINUX_AMD64, DARWIN_ARM64 (for versions.env checksum keys)
os_arch() {
  local os arch
  os="$(uname -s | tr '[:lower:]' '[:upper:]')"
  case "$(uname -m)" in
    x86_64|amd64) arch=AMD64 ;;
    aarch64|arm64) arch=ARM64 ;;
    *) die "unsupported architecture $(uname -m)" ;;
  esac
  echo "${os}_${arch}"
}
