#!/usr/bin/env bash
# Tail one service's logs (make logs SVC=<deployment or statefulset name>).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
svc="${1:-}"; [[ -n "$svc" ]] || die "usage: make logs SVC=<name>"
ref="$(kubectl get deploy,sts -A -o json \
  | jq -r --arg s "$svc" '.items[] | select(.metadata.name == $s) | "\(.metadata.namespace) \(.kind | ascii_downcase)/\(.metadata.name)"' | head -1)"
[[ -n "$ref" ]] || die "no deployment or statefulset named $svc"
read -r ns obj <<<"$ref"
exec kubectl -n "$ns" logs -f --tail=100 "$obj"
