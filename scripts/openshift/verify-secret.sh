#!/usr/bin/env bash
# Create (or update) the verify-oidc Secret in aigw-ai from .secrets/verify.env, then restart
# the console so it picks it up (D-072, docs/IDENTITY.md). Argo CD never manages Secrets
# (OS-09); the console reads every key as optional, so without this Secret it stays anonymous.
#
#   scripts/openshift/verify-secret.sh            # AUTH_MODE=verify: sign in with IBM Verify
#   MODE=none scripts/openshift/verify-secret.sh  # roll back: anonymous console, Secret kept
#
# The redirect URI is built from the cluster's ingress domain, so nothing cluster-specific is
# in git. Idempotent.
set -euo pipefail
TARGET=openshift
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
require_openshift

ENV_FILE="$ROOT/.secrets/verify.env"
[[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE (docs/IDENTITY.md, step 4)"
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a
for k in VERIFY_TENANT VERIFY_CONSOLE_CLIENT_ID VERIFY_CONSOLE_CLIENT_SECRET \
         VERIFY_RESEARCHER_CLIENT_ID VERIFY_RESEARCHER_CLIENT_SECRET \
         VERIFY_EXECUTOR_CLIENT_ID VERIFY_EXECUTOR_CLIENT_SECRET; do
  [[ -n "${!k:-}" ]] || die "$ENV_FILE is missing $k"
done
MODE="${MODE:-verify}"
[[ "$MODE" == verify || "$MODE" == none ]] || die "MODE must be verify or none"

DOMAIN="$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}')"
[[ -n "$DOMAIN" ]] || die "couldn't read the cluster's ingress domain"
REDIRECT="https://aigw-console.$DOMAIN/console/auth/callback"

oc -n aigw-ai create secret generic verify-oidc \
  --from-literal=auth_mode="$MODE" \
  --from-literal=tenant="${VERIFY_TENANT#https://}" \
  --from-literal=redirect_uri="$REDIRECT" \
  --from-literal=console_client_id="$VERIFY_CONSOLE_CLIENT_ID" \
  --from-literal=console_client_secret="$VERIFY_CONSOLE_CLIENT_SECRET" \
  --from-literal=researcher_client_id="$VERIFY_RESEARCHER_CLIENT_ID" \
  --from-literal=researcher_client_secret="$VERIFY_RESEARCHER_CLIENT_SECRET" \
  --from-literal=executor_client_id="$VERIFY_EXECUTOR_CLIENT_ID" \
  --from-literal=executor_client_secret="$VERIFY_EXECUTOR_CLIENT_SECRET" \
  --dry-run=client -o yaml | oc apply -f - >/dev/null
log "verify-oidc Secret applied (AUTH_MODE=$MODE, redirect URI $REDIRECT)"

oc -n aigw-ai rollout restart deploy/console >/dev/null
oc -n aigw-ai rollout status deploy/console --timeout=180s
log "console restarted; open https://aigw-console.$DOMAIN/console/"
