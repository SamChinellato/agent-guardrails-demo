#!/usr/bin/env bash
# Render docs/architecture.png (k3d) and docs/architecture-openshift.png from
# docs/diagram/architecture.py (make diagram).
# Graphviz isn't needed on the host: rendering runs in a throwaway container from the
# pinned Python base image.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
detect_runtime || die "no container runtime"
runtime_cli run --rm -v "$ROOT/docs/diagram:/src:ro" -v "$ROOT/docs:/out" -w /src "$(pinned_ref PYTHON_BASE)" sh -c '
  apt-get update -qq >/dev/null && apt-get install -y -qq graphviz fonts-dejavu-core >/dev/null &&
  pip install -q --no-cache-dir diagrams==0.25.1 >/dev/null 2>&1 &&
  python architecture.py && TARGET=openshift python architecture.py'
cp "$ROOT/docs/architecture.png" "$ROOT/docs/architecture-openshift.png" "$ROOT/services/console/src/console_app/static/"
log "wrote docs/architecture.png and docs/architecture-openshift.png (the console serves its target's one at /console/architecture.png)"
