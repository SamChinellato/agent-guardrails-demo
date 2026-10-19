#!/usr/bin/env bash
# Change one guard check's mode in the live policy (make policy-set CHECK=... MODE=...).
# The guard hot-reloads within 10 s (GRD-02). `make reset` restores policy/policy.yaml.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
check="${1:-}"; mode="${2:-}"
[[ -n "$check" && "$mode" =~ ^(enforce|monitor|off)$ ]] || die "usage: make policy-set CHECK=<check> MODE=enforce|monitor|off"

current="$(kubectl -n ai get configmap guard-policy -o jsonpath='{.data.policy\.yaml}')"
updated="$(uv run -q --no-project --with 'pyyaml>=6,<7' python -c '
import sys, yaml
policy = yaml.safe_load(sys.stdin.read())
check, mode = sys.argv[1], sys.argv[2]
known = sorted(policy.get("checks", {}))
if check not in known:
    sys.exit(f"unknown check {check!r}; known: {known}")
policy["checks"][check]["mode"] = mode
print(yaml.safe_dump(policy, sort_keys=False, default_flow_style=None))
' "$check" "$mode" <<<"$current")"
kubectl -n ai create configmap guard-policy --from-literal=policy.yaml="$updated" --dry-run=client -o yaml \
  | kubectl apply -f - >/dev/null
log "guard policy: $check -> $mode (the guard picks it up within 10 s)"
