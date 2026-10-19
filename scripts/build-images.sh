#!/usr/bin/env bash
# Build every service image for the host architecture, tag it <service>:<git-short-sha>
# and push it to the k3d registry (REP-03, REP-04). An image whose tag is already in the
# registry is not rebuilt. Writes the tags to .cache/image-tags.env for up.sh.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
detect_runtime || die "no container runtime"

SERVICES_DIR="$ROOT/services"
mkdir -p "$ROOT/.cache"
TAGS="$ROOT/.cache/image-tags.env"
: >"$TAGS.tmp"

# Workspace members except the shared library
members="$(sed -n '/^\[tool.uv.workspace\]/,/^\[/p' "$SERVICES_DIR/pyproject.toml" \
  | grep -oE '"[a-z0-9-]+"' | tr -d '"' | grep -vx common)"

# One tag for all services: they share demo-common and uv.lock
tag="$(git_tag services)"

push_args=(); [[ "$CONTAINER_RUNTIME" == podman ]] && push_args=(--tls-verify=false)

for svc in $members; do
  var="$(tr '[:lower:]-' '[:upper:]_' <<<"$svc")_TAG"
  echo "$var=$tag" >>"$TAGS.tmp"
  if curl -sf -o /dev/null -I -H 'Accept: application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json' \
       "http://$REGISTRY_PUSH/v2/$svc/manifests/$tag"; then
    log "image $svc:$tag already in registry"
    continue
  fi
  log "building $svc:$tag"
  runtime_cli build -q -f "$SERVICES_DIR/Dockerfile" \
    --build-arg "PYTHON_BASE=$(pinned_ref PYTHON_BASE)" \
    --build-arg "UV_IMAGE=$(pinned_ref UV)" \
    --build-arg "SERVICE=$svc" \
    -t "$REGISTRY_PUSH/$svc:$tag" "$SERVICES_DIR" >/dev/null
  runtime_cli push -q "${push_args[@]}" "$REGISTRY_PUSH/$svc:$tag" >/dev/null
done
mv "$TAGS.tmp" "$TAGS"
