#!/usr/bin/env bash
# Delete the cluster and its registry (make down). Keeps .prefetch/ (charts cache) and
# .secrets/ so the next `make up` reuses the same keys; `make clean-room` removes those.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
detect_runtime || die "no container runtime"

if k3d cluster list -o json 2>/dev/null | jq -e --arg n "$CLUSTER_NAME" '.[] | select(.name == $n)' >/dev/null; then
  log "deleting cluster $CLUSTER_NAME"; k3d cluster delete "$CLUSTER_NAME"
else
  log "cluster $CLUSTER_NAME not present"
fi
if k3d registry list -o json 2>/dev/null | jq -e --arg n "$REGISTRY_NAME" '.[] | select(.name == $n)' >/dev/null; then
  log "deleting registry $REGISTRY_NAME"; k3d registry delete "$REGISTRY_NAME"
fi
rm -f "$KUBECONFIG" "$ROOT/.cache/image-tags.env"
