#!/usr/bin/env bash
# Prerequisite checks (REP-05). Usage: scripts/prereqs.sh [mock|live]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../versions.env
source "$ROOT/versions.env"
LLM_MODE="${1:-${LLM_MODE:-mock}}"
FAILED=0

ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAILED=1; }
warn() { printf '  \033[33mwarn\033[0m %s\n' "$*"; }

# version_ge A B: true when A >= B (semver-ish, leading "v" ignored)
version_ge() { [[ "$(printf '%s\n%s\n' "${2#v}" "${1#v}" | sort -V | head -1)" == "${2#v}" ]]; }

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

check_tool docker  "$DOCKER_MIN_VERSION"  "docker version --format '{{.Client.Version}}'" "https://docs.docker.com/engine/install/"
check_tool k3d     "$K3D_MIN_VERSION"     "k3d version"                    "https://k3d.io/#installation"
check_tool kubectl "$KUBECTL_MIN_VERSION" "kubectl version --client"       "https://kubernetes.io/docs/tasks/tools/"
check_tool helm    "$HELM_MIN_VERSION"    "helm version --short"           "https://helm.sh/docs/intro/install/"
check_tool uv      "$UV_VERSION"          "uv --version"                   "https://docs.astral.sh/uv/getting-started/installation/"
check_tool jq      "1.6"                  "jq --version"                   "install jq from your package manager"
check_tool make    "4.0.0"                "make --version"                 "install GNU make (macOS: brew install make)"
check_tool curl    "7.68.0"               "curl --version"                 "install curl"

# Docker daemon access and memory
if command -v docker >/dev/null 2>&1; then
  if mem_bytes="$(docker info --format '{{.MemTotal}}' 2>/dev/null)" && [[ "$mem_bytes" -gt 0 ]]; then
    mem_gb=$(( mem_bytes / 1024 / 1024 / 1024 ))
    # Ollama runs on the host, so live mode needs no extra Docker memory (D-002).
    # An "8 GB" Docker VM reports ~7.7 GiB usable, hence the >= 7 floor.
    if (( mem_gb >= 7 )); then ok "Docker memory ${mem_gb} GB (>= 8 GB)"
    else fail "Docker has ${mem_gb} GB memory; need >= 8 GB"; fi
  else
    fail "cannot talk to the Docker daemon. Start Docker, or on Linux add yourself to the docker group: sudo usermod -aG docker \$USER (then log out and in)"
  fi
fi

# Free disk where Docker stores data (fall back to the repo's filesystem)
docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
disk_path="$ROOT"; [[ -n "$docker_root" && -d "$docker_root" ]] && disk_path="$docker_root"
free_gb=$(( $(df -Pk "$disk_path" | awk 'NR==2{print $4}') / 1024 / 1024 ))
if (( free_gb >= 20 )); then ok "free disk ${free_gb} GB at $disk_path (>= 20 GB)"
else fail "only ${free_gb} GB free at $disk_path; need >= 20 GB"; fi

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
