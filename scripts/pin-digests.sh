#!/usr/bin/env bash
# Resolve every *_IMAGE (repo:tag) in versions.env to its multi-arch index digest
# and write it to the matching *_DIGEST line. Run only when deliberately changing
# versions (rule 3: never change pins silently).
#
# The Ollama model runs on the host, so its digest is pinned from the host's
# Ollama after `ollama pull` (see scripts/prefetch.sh), not resolved here.
#
# Usage: scripts/pin-digests.sh [--check]
#   --check  resolve digests and fail if any differ from versions.env (no writes)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSIONS="$ROOT/versions.env"
MODE="${1:-write}"
ACCEPT="application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json"

die() { echo "pin-digests: $*" >&2; exit 1; }

# split "registry/repo:tag" into registry, repo, tag (Docker Hub defaults applied)
parse_ref() {
  local ref="$1" name tag registry repo
  name="${ref%:*}"; tag="${ref##*:}"
  [[ "$name" == "$ref" ]] && die "image '$ref' has no tag"
  if [[ "$name" == */* && "${name%%/*}" == *[.:]* ]]; then
    registry="${name%%/*}"; repo="${name#*/}"
  else
    registry="registry-1.docker.io"; repo="$name"
    [[ "$repo" != */* ]] && repo="library/$repo"
  fi
  [[ "$registry" == "docker.io" ]] && registry="registry-1.docker.io"
  printf '%s %s %s\n' "$registry" "$repo" "$tag"
}

# fetch an anonymous bearer token from the WWW-Authenticate challenge, if any
token_for() {
  local registry="$1" repo="$2" hdr realm service
  hdr="$(curl -sSL -m 30 -o /dev/null -D - "https://$registry/v2/" | tr -d '\r' | grep -i '^www-authenticate:' || true)"
  [[ -z "$hdr" ]] && return 0
  realm="$(sed -n 's/.*realm="\([^"]*\)".*/\1/p' <<<"$hdr")"
  service="$(sed -n 's/.*service="\([^"]*\)".*/\1/p' <<<"$hdr")"
  curl -sS -m 30 -G "$realm" --data-urlencode "service=$service" \
    --data-urlencode "scope=repository:$repo:pull" | jq -r '.token // .access_token'
}

resolve_digest() {
  local registry repo tag token digest
  read -r registry repo tag < <(parse_ref "$1")
  token="$(token_for "$registry" "$repo")"
  digest="$(curl -sSL -m 30 -I -H "Accept: $ACCEPT" ${token:+-H "Authorization: Bearer $token"} \
    "https://$registry/v2/$repo/manifests/$tag" | tr -d '\r' | awk -F': ' 'tolower($1)=="docker-content-digest"{print $2}')"
  [[ "$digest" == sha256:* ]] || die "could not resolve digest for $1"
  echo "$digest"
}

[[ -f "$VERSIONS" ]] || die "versions.env not found"
drift=0
while IFS='=' read -r key ref; do
  dkey="${key%_IMAGE}_DIGEST"
  grep -q "^$dkey=" "$VERSIONS" || die "$dkey missing from versions.env"
  digest="$(resolve_digest "$ref")"
  current="$(grep "^$dkey=" "$VERSIONS" | cut -d= -f2-)"
  if [[ "$MODE" == "--check" ]]; then
    [[ "$current" == "$digest" ]] || { echo "DRIFT $ref: pinned=$current upstream=$digest"; drift=1; }
  elif [[ "$current" != "$digest" ]]; then
    sed -i.bak "s|^$dkey=.*|$dkey=$digest|" "$VERSIONS" && rm -f "$VERSIONS.bak"
    echo "$ref -> $digest"
  fi
done < <(grep -E '^[A-Z0-9_]+_IMAGE=' "$VERSIONS")
exit "$drift"
