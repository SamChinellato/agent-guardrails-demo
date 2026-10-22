#!/usr/bin/env bash
# Argo CD renders deploy/openshift/apps from git, so its pinned image refs are written out
# there rather than substituted from versions.env as on k3d. Fail if any image@digest in
# the overlays isn't an *_IMAGE@*_DIGEST pair from versions.env (REP-02). No network.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=/dev/null
set -a; source "$ROOT/versions.env"; set +a

pinned="$(compgen -v | grep '_IMAGE$' | while read -r i; do d="${i%_IMAGE}_DIGEST"; [[ -n "${!d:-}" ]] && echo "${!i}@${!d}"; done)"
bad=0
while read -r ref; do
  grep -qxF "$ref" <<<"$pinned" || { echo "check-overlay-pins: $ref is not pinned in versions.env" >&2; bad=1; }
done < <(grep -rhoE '[a-z0-9./-]+(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}' "$ROOT/deploy/openshift/apps" \
          | grep -E '^[a-z].*/' | sort -u)
# Helm values pin as `tag: "<tag>@sha256:..."` with the repository elsewhere: the pair must
# end some pinned ref
while read -r tagref; do
  grep -qE ":${tagref//./\\.}\$" <<<"$pinned" || { echo "check-overlay-pins: tag $tagref is not pinned in versions.env" >&2; bad=1; }
done < <(grep -rhoE 'tag: *"?[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}' "$ROOT/deploy/openshift/apps" | sed -E 's/tag: *"?//' | sort -u)
(( bad == 0 )) && echo "OpenShift overlay pins match versions.env"
exit "$bad"
