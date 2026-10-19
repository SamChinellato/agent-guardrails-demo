#!/usr/bin/env bash
# Prove NetworkPolicy is actually enforced (NFR-08): a fresh pod in the ai namespace must be
# cut off from the internet. Catches a policy controller that fails silently, as kube-router
# did under rootless Podman with a small net.core.wmem_default (D-029).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
name="netpol-check-$RANDOM"
trap 'kubectl -n ai delete pod "$name" --now --wait=false >/dev/null 2>&1 || true' EXIT
kubectl -n ai run "$name" --image=docker.io/curlimages/curl:8.11.1 --labels=app=netpol-check \
  --restart=Never --command -- sleep 120 >/dev/null
kubectl -n ai wait --for=condition=Ready "pod/$name" --timeout=90s >/dev/null
blocked() { [[ "$(kubectl -n ai exec "$name" -- curl -s -o /dev/null -m 3 -w '%{http_code}' https://1.1.1.1 2>/dev/null || true)" == 000 ]]; }
for _ in $(seq 20); do blocked && { log "network policy enforced (ai namespace has no egress)"; exit 0; }; sleep 1; done
die "NetworkPolicy is NOT enforced: a pod in ai can reach the internet. Check 'make prereqs' and the k3s log for 'Message too large'"
