#!/usr/bin/env bash
# OpenShift counterpart of scripts/build-images.sh (OS-02): build every service image in
# the cluster with binary BuildConfigs in aigw-build. Tags <svc>:latest (what workloads
# follow) and <svc>:<git-tag> (REP-03). A service whose <git-tag> already exists is not
# rebuilt (REP-07). Builds run in parallel; the script fails if any build fails.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../lib.sh"
require_openshift

SERVICES_DIR="$ROOT/services"
NS=aigw-build
members="$(sed -n '/^\[tool.uv.workspace\]/,/^\[/p' "$SERVICES_DIR/pyproject.toml" \
  | grep -oE '"[a-z0-9-]+"' | tr -d '"' | grep -vxE 'common|cli')"   # cli runs on the host
tag="$(git_tag services)"

# Build context: services/ without what .dockerignore excludes (a binary build uploads the
# directory as is, so the 200+ MB local .venv would otherwise go with it)
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
tar -C "$SERVICES_DIR" --exclude-vcs --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
  --exclude='.pytest_cache' --exclude='./cli' -czf "$TMP/context.tar.gz" .

export PYTHON_BASE_REF UV_REF
PYTHON_BASE_REF="$(pinned_ref PYTHON_BASE)"; UV_REF="$(pinned_ref UV)"

pids=()
for svc in $members; do
  export SERVICE="$svc" BUILD_CLASSIFIER_MODEL="" BUILD_CLASSIFIER_REVISION="" BUILD_CLASSIFIER_ONNX_SHA256="" \
    BUILD_CLASSIFIER_TOKENIZER_SHA256="" BUILD_CLASSIFIER_CONFIG_SHA256=""
  if [[ "$svc" == guard ]]; then
    BUILD_CLASSIFIER_MODEL="$CLASSIFIER_MODEL" BUILD_CLASSIFIER_REVISION="$CLASSIFIER_REVISION"
    BUILD_CLASSIFIER_ONNX_SHA256="$CLASSIFIER_ONNX_SHA256" BUILD_CLASSIFIER_TOKENIZER_SHA256="$CLASSIFIER_TOKENIZER_SHA256"
    BUILD_CLASSIFIER_CONFIG_SHA256="$CLASSIFIER_CONFIG_SHA256"
  fi
  render_env "$ROOT/deploy/openshift/build/service.yaml" | oc apply -f - >/dev/null
  if oc -n "$NS" get istag "$svc:$tag" >/dev/null 2>&1; then
    log "image $svc:$tag already built"; continue
  fi
  log "building $svc:$tag"
  ( oc -n "$NS" start-build "$svc" --from-archive="$TMP/context.tar.gz" --wait >"$TMP/$svc.log" 2>&1 \
      && oc -n "$NS" tag "$svc:latest" "$svc:$tag" >/dev/null \
      || { echo "build $svc failed:"; tail -20 "$TMP/$svc.log"; exit 1; } ) >&2 &
  pids+=($!)
done
failed=0
for p in "${pids[@]}"; do wait "$p" || failed=1; done
(( failed == 0 )) || die "image build failed (oc -n $NS get builds)"
log "images built ($tag)"
