#!/usr/bin/env bash
# Render docs/architecture.png (k3d) and docs/architecture-openshift.png from
# docs/diagram/architecture.py (make diagram, D-070).
# The script writes an SVG; rsvg-convert turns it into the PNG. Neither is needed on the host:
# rendering runs in a throwaway container from the pinned Python base image. The icons are in
# docs/diagram/icons, so rendering downloads nothing but the two Debian packages.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
detect_runtime || die "no container runtime"
runtime_cli run --rm -v "$ROOT/docs/diagram:/src:ro" -v "$ROOT/docs:/out" -w /src "$(pinned_ref PYTHON_BASE)" sh -c '
  apt-get update -qq >/dev/null && apt-get install -y -qq librsvg2-bin fonts-liberation >/dev/null &&
  python architecture.py && TARGET=openshift python architecture.py &&
  for f in architecture architecture-openshift; do
    rsvg-convert -z 1.5 /out/$f.svg -o /out/$f.png && rm /out/$f.svg
  done'
cp "$ROOT/docs/architecture.png" "$ROOT/docs/architecture-openshift.png" "$ROOT/services/console/src/console_app/static/"
log "wrote docs/architecture.png and docs/architecture-openshift.png (the console serves its target's one at /console/architecture.png)"
