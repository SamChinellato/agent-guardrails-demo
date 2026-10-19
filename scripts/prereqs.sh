#!/usr/bin/env bash
# Prerequisite checks (REP-05). Usage: scripts/prereqs.sh [mock|live]
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
LLM_MODE="${1:-${LLM_MODE:-mock}}"
FAILED=0

ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILED=1; }
warn() { printf '  \033[33mwarn\033[0m %s\n' "$*"; }

check_tool() {
  local name="$1" min="$2" cmd="$3" hint="$4" ver
  if ! command -v "$name" >/dev/null 2>&1; then
    fail "$name not found (need >= $min). $hint"; return
  fi
  ver="$(eval "$cmd" 2>/dev/null | grep -oE 'v?[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1 || true)"
  if [[ -z "$ver" ]]; then fail "$name: could not read version"
  elif version_ge "$ver" "$min"; then ok "$name $ver (>= $min)"
  else fail "$name $ver is older than $min. $hint"; fi
}

echo "Checking prerequisites (LLM_MODE=$LLM_MODE)"

# Container runtime: Docker, or Podman through its Docker-compatible socket (D-009)
if ! detect_runtime; then
  fail "no container runtime found. Install Docker (https://docs.docker.com/engine/install/) or Podman >= $PODMAN_MIN_VERSION"
elif [[ "$CONTAINER_RUNTIME" == docker ]]; then
  check_tool docker "$DOCKER_MIN_VERSION" "docker version --format '{{.Server.Version}}'" "https://docs.docker.com/engine/install/"
else
  check_tool podman "$PODMAN_MIN_VERSION" "podman version --format '{{.Client.Version}}'" "https://podman.io/docs/installation"
  if [[ "$RUNTIME_ROOTLESS" == true ]]; then
    if curl -s -m 5 --unix-socket "$DOCKER_SOCK" http://d/_ping 2>/dev/null | grep -q OK; then
      ok "podman API socket $DOCKER_SOCK"
    else
      fail "podman API socket not answering at $DOCKER_SOCK. Run: systemctl --user enable --now podman.socket"
    fi
    # k3s in a rootless container needs these controllers delegated to the user session
    ctrls="$(podman info --format '{{.Host.CgroupControllers}}' 2>/dev/null | tr -d '[]')"
    missing=""
    for c in cpu cpuset io memory pids; do [[ " $ctrls " == *" $c "* ]] || missing+="$c "; done
    if [[ -z "$missing" ]]; then ok "cgroup controllers delegated ($ctrls)"
    else
      fail "cgroup controllers not delegated: ${missing}(k3s fails with 'failed to find cpuset cgroup'). Fix once with:
         sudo mkdir -p /etc/systemd/system/user@.service.d
         printf '[Service]\\nDelegate=cpu cpuset io memory pids\\n' | sudo tee /etc/systemd/system/user@.service.d/delegate.conf
         sudo systemctl daemon-reload   # then restart user@\$(id -u).service or reboot"
    fi
  fi
fi

check_tool k3d     "$K3D_MIN_VERSION"     "k3d version"                    "Run: make tools"
check_tool kubectl "$KUBECTL_MIN_VERSION" "kubectl version --client"       "Run: make tools"
check_tool helm    "$HELM_MIN_VERSION"    "helm version --short"           "Run: make tools"
check_tool uv      "$UV_VERSION"          "uv --version"                   "https://docs.astral.sh/uv/getting-started/installation/"
check_tool jq      "1.6"                  "jq --version"                   "install jq from your package manager"
check_tool make    "4.0.0"                "make --version"                 "install GNU make (macOS: brew install make)"
check_tool curl    "7.68.0"               "curl --version"                 "install curl"

# Memory available to containers. Ollama runs on the host, so live mode needs no extra (D-002).
# An "8 GB" Docker VM reports ~7.7 GiB usable, hence the >= 7 floor.
if [[ -n "${CONTAINER_RUNTIME:-}" ]]; then
  if [[ "$CONTAINER_RUNTIME" == docker ]]; then
    mem_bytes="$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)"
    store="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
  else
    mem_bytes="$(podman info --format '{{.Host.MemTotal}}' 2>/dev/null || echo 0)"
    store="$(podman info --format '{{.Store.GraphRoot}}' 2>/dev/null || true)"
  fi
  mem_gb=$(( mem_bytes / 1024 / 1024 / 1024 ))
  if (( mem_gb >= 7 )); then ok "$CONTAINER_RUNTIME memory ${mem_gb} GB (>= 8 GB)"
  else fail "$CONTAINER_RUNTIME has ${mem_gb} GB memory; need >= 8 GB"; fi
fi

# Free disk where images are stored (fall back to the repo's filesystem)
disk_path="$ROOT"; [[ -n "${store:-}" && -d "${store:-}" ]] && disk_path="$store"
free_gb=$(( $(df -Pk "$disk_path" | awk 'NR==2{print $4}') / 1024 / 1024 ))
if (( free_gb >= 20 )); then ok "free disk ${free_gb} GB at $disk_path (>= 20 GB)"
else fail "only ${free_gb} GB free at $disk_path; need >= 20 GB"; fi

# Host ports mapped by k3d (7.1) and the local registry must be free
port_in_use() {
  if command -v ss >/dev/null 2>&1; then [[ -n "$(ss -ltnH "sport = :$1" 2>/dev/null)" ]]
  else lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; fi
}
for p in "$HOST_PORT_GATEWAY" "$HOST_PORT_GRAFANA" "$HOST_PORT_PANEL" "$REGISTRY_PORT"; do
  if port_in_use "$p"; then
    owner="$(runtime_cli ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | grep -E ":$p->" | awk '{print $1}' | head -1 || true)"
    if [[ "$owner" == k3d-* ]]; then ok "port $p in use by $owner (this demo)"
    else fail "host port $p is already in use${owner:+ by $owner}; free it, or override (HOST_PORT_GATEWAY / HOST_PORT_GRAFANA / HOST_PORT_PANEL)"; fi
  else
    ok "host port $p free"
  fi
done

# Live mode: the host's Ollama must be up, reachable from containers, and hold the pinned model
if [[ "$LLM_MODE" == "live" ]]; then
  if ! command -v ollama >/dev/null 2>&1 && ! curl -sf -m 5 http://localhost:11434/api/version >/dev/null; then
    fail "Ollama not found on the host. Install it from https://ollama.com/download"
  elif ! ollama_ver="$(curl -sf -m 5 http://localhost:11434/api/version | jq -r .version)"; then
    fail "Ollama is not answering on localhost:11434. Start it (ollama serve, or systemctl start ollama)"
  else
    if version_ge "$ollama_ver" "$OLLAMA_MIN_VERSION"; then ok "host Ollama $ollama_ver (>= $OLLAMA_MIN_VERSION)"
    else fail "host Ollama $ollama_ver is older than $OLLAMA_MIN_VERSION"; fi

    # k3d pods reach the host via the Docker network gateway, so a loopback-only bind is unreachable on Linux
    if [[ "$(uname -s)" == "Linux" ]] && command -v ss >/dev/null 2>&1; then
      if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE '^(\*|0\.0\.0\.0|\[::\]):11434$'; then
        ok "Ollama listens on all interfaces (reachable from k3d)"
      else
        fail "Ollama listens on loopback only; k3d pods cannot reach it. Set OLLAMA_HOST=0.0.0.0:11434 (systemctl edit ollama) and restart"
      fi
    fi

    digest="$(curl -sf -m 5 http://localhost:11434/api/tags | jq -r --arg m "$OLLAMA_MODEL" '.models[]|select(.name==$m)|.digest' || true)"
    if [[ -z "$digest" ]]; then fail "model $OLLAMA_MODEL not pulled on the host. Run: make prefetch LLM_MODE=live"
    elif [[ "sha256:$digest" == "$OLLAMA_MODEL_DIGEST" ]]; then ok "model $OLLAMA_MODEL matches pinned digest"
    else fail "model $OLLAMA_MODEL digest sha256:$digest != pinned $OLLAMA_MODEL_DIGEST"; fi
  fi

  host_mem_gb="$( (awk '/MemTotal/{print int($2/1024/1024)}' /proc/meminfo 2>/dev/null || sysctl -n hw.memsize 2>/dev/null | awk '{print int($1/1024/1024/1024)}') | head -1)"
  if [[ -n "$host_mem_gb" ]] && (( host_mem_gb < 15 )); then warn "host has ${host_mem_gb} GB RAM; 16 GB recommended for live mode (NFR-04)"; fi
fi

# versions.env must have no unpinned entry
if unpinned="$(grep -E '^[A-Z0-9_]+=$' "$ROOT/versions.env")"; then
  fail "versions.env has unpinned entries: $(echo "$unpinned" | tr '\n' ' ')"
else
  ok "versions.env fully pinned"
fi

if (( FAILED )); then echo "Prerequisite check FAILED"; exit 1; fi
echo "All prerequisites satisfied"
