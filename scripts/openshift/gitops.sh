#!/usr/bin/env bash
# Point the cluster's openshift-gitops Argo CD at this repository (OS-08, OS-11, OS-14):
# a read-only deploy key (generated once into .secrets/, never committed), the repository
# Secret, and the aigw project and Application. Idempotent.
#
# The public key must be added to the GitHub repository as a read-only deploy key once;
# the script prints it and where to add it.
#
# GITOPS_REVISION: the branch Argo follows. Default: the one the Applications already
# follow, else main. (Re-running without it must not move a cluster off a work branch.)
set -euo pipefail
TARGET=openshift
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
require_openshift

NS=openshift-gitops
REPO="$(git -C "$ROOT" remote get-url origin)"
[[ "$REPO" == git@* || "$REPO" == ssh://* ]] || die "origin ($REPO) isn't an SSH URL; the deploy key needs one"
current="$(oc -n "$NS" get application aigw-workloads -o jsonpath='{.spec.source.targetRevision}' 2>/dev/null || true)"
export GITOPS_REPO="$REPO" GITOPS_REVISION="${GITOPS_REVISION:-${current:-main}}"

KEY="$ROOT/.secrets/argocd-deploy-key"
mkdir -p "$ROOT/.secrets"; chmod 700 "$ROOT/.secrets"
if [[ ! -s "$KEY" ]]; then
  log "generating the Argo CD deploy key"
  ssh-keygen -q -t ed25519 -N "" -C "argocd@aigw-demo" -f "$KEY"
fi
chmod 600 "$KEY"

# Argo CD finds repository credentials by this label
oc -n "$NS" create secret generic aigw-repo --from-literal=type=git --from-literal=url="$REPO" \
  --from-file=sshPrivateKey="$KEY" --dry-run=client -o yaml \
  | oc label --local -f - argocd.argoproj.io/secret-type=repository -o yaml | oc apply -f - >/dev/null

render_env "$ROOT/deploy/openshift/gitops/workloads.yaml" | oc apply -f - >/dev/null
log "Argo CD follows $REPO at $GITOPS_REVISION"
cat <<EOF

  If the repository doesn't have it yet, add this read-only deploy key on GitHub
  (Settings > Deploy keys > Add deploy key, leave "Allow write access" off):

    $(cat "$KEY.pub")

EOF
