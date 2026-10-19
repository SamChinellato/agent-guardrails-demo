# Shared helpers for scripts/*.sh. Source it; do not execute.
# Sets ROOT, loads versions.env, puts .tools/bin first on PATH, detects the container
# runtime (Docker or rootless Podman, D-009) and derives runtime-specific settings.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../versions.env
source "$ROOT/versions.env"

export PATH="$ROOT/.tools/bin:$PATH"
# Keep the demo's kubeconfig out of ~/.kube/config
export KUBECONFIG="${KUBECONFIG_DEMO:-$ROOT/.kube/config}"

CLUSTER_NAME="ai-gateway-demo"
REGISTRY_NAME="k3d-registry.localhost"   # k3d prefixes "k3d-" to "registry.localhost"
REGISTRY_PORT=5000
REGISTRY_PUSH="127.0.0.1:$REGISTRY_PORT" # host side; *.localhost may resolve to ::1 only
REGISTRY_PULL="$REGISTRY_NAME:$REGISTRY_PORT"

# Host ports (7.1). Spec defaults; override when a port is taken on the host (D-011).
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
