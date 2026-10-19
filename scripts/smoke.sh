#!/usr/bin/env bash
# Post-`make up` smoke test through the host port: auth, consumer restriction and a
# completion on the LLM route. Polls with a deadline rather than sleeping (rule 8).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

URL="http://127.0.0.1:$HOST_PORT_GATEWAY/llm/v1/chat/completions"
BODY='{"model":"demo","messages":[{"role":"user","content":"smoke test"}]}'
OUT="$(mktemp)"; trap 'rm -f "$OUT"' EXIT

status() { # status CONSUMER|-: HTTP status of one LLM call
  local key=()
  [[ "$1" != - ]] && key=(-H "apikey: $(cat "$ROOT/.secrets/consumer-$1")")
  curl -s -m 30 -o "$OUT" -w '%{http_code}' "${key[@]}" -H 'Content-Type: application/json' \
    -H 'X-Demo-Scenario: smoke' -d "$BODY" "$URL" || echo 000
}

expect() { # expect DESCRIPTION WANT CONSUMER
  local got; got="$(status "$3")"
  [[ "$got" == "$2" ]] || die "smoke: $1: expected $2, got $got: $(head -c 300 "$OUT")"
  log "smoke: $1 -> $got"
}

# The route can take a moment to appear after bootstrap
wait_for "LLM route" 30 bash -c "[[ \"\$(curl -s -o /dev/null -w '%{http_code}' -X POST $URL)\" == 401 ]]"

expect "no key is rejected" 401 -
expect "demo-user may not call the LLM" 403 demo-user
expect "researcher gets a completion" 200 researcher
if [[ "${LLM_MODE:-mock}" == mock ]]; then
  jq -e '.choices[0].message.content == "smoke-ok researcher"' "$OUT" >/dev/null \
    || die "smoke: unexpected completion $(head -c 300 "$OUT")"
fi
