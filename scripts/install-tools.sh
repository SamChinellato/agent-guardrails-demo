#!/usr/bin/env bash
# Install pinned k3d, kubectl and helm into .tools/bin, verified by SHA-256 (REP-02).
# Idempotent: a tool already at the pinned version is left alone.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

BIN="$ROOT/.tools/bin"
mkdir -p "$BIN"
OA="$(os_arch)"                                  # e.g. LINUX_AMD64
os="$(tr '[:upper:]' '[:lower:]' <<<"${OA%_*}")"  # linux | darwin
arch="$(tr '[:upper:]' '[:lower:]' <<<"${OA#*_}")" # amd64 | arm64
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

sha256_of() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | awk '{print $1}'; }

# fetch URL DEST EXPECTED_SHA
fetch() {
  curl -sSLf --retry 4 --retry-delay 2 -m 300 -o "$2" "$1"
  local got; got="$(sha256_of "$2")"
  [[ "$got" == "$3" ]] || die "checksum mismatch for $1: got $got, want $3"
}

installed_version() { [[ -x "$BIN/$1" ]] && "$BIN/$1" "${@:2}" 2>/dev/null | grep -oE 'v?[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true; }

sha_var() { local v="${1}_SHA256_${OA}"; [[ -n "${!v:-}" ]] || die "no pinned checksum $v in versions.env"; echo "${!v}"; }

if [[ "$(installed_version k3d version)" == "v$K3D_VERSION" ]]; then
  log "k3d v$K3D_VERSION already installed"
else
  log "installing k3d v$K3D_VERSION"
  fetch "https://github.com/k3d-io/k3d/releases/download/v$K3D_VERSION/k3d-$os-$arch" "$TMP/k3d" "$(sha_var K3D)"
  install -m 0755 "$TMP/k3d" "$BIN/k3d"
fi

if [[ "$(installed_version kubectl version --client)" == "v$KUBECTL_VERSION" ]]; then
  log "kubectl v$KUBECTL_VERSION already installed"
else
  log "installing kubectl v$KUBECTL_VERSION"
  fetch "https://dl.k8s.io/release/v$KUBECTL_VERSION/bin/$os/$arch/kubectl" "$TMP/kubectl" "$(sha_var KUBECTL)"
  install -m 0755 "$TMP/kubectl" "$BIN/kubectl"
fi

if [[ "$(installed_version helm version --short)" == "v$HELM_VERSION" ]]; then
  log "helm v$HELM_VERSION already installed"
else
  log "installing helm v$HELM_VERSION"
  fetch "https://get.helm.sh/helm-v$HELM_VERSION-$os-$arch.tar.gz" "$TMP/helm.tgz" "$(sha_var HELM)"
  tar -xzf "$TMP/helm.tgz" -C "$TMP" "$os-$arch/helm"
  install -m 0755 "$TMP/$os-$arch/helm" "$BIN/helm"
fi
