#!/usr/bin/env bash
# Apply gateway/ (config as code, GW-08) to APISIX through a deliberate Admin API
# port-forward (rule 6), then read everything back and fail on drift.
#
# Templates: a string that is exactly "${VAR}" becomes VAR's typed value (string, number
# or object); "${VAR}" inside a longer string is interpolated. Unknown variables fail.
#
# Idempotent (REP-07): an object whose live value already matches is not written.
# Kill-switch state is preserved: an active fault-injection in a plugin config or on a
# consumer is carried over, so `make up` during an incident never silently lifts a kill.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
detect_runtime || die "no container runtime"
llm_host_settings
LLM_MODE="${LLM_MODE:-mock}"
GW="$ROOT/gateway"

# ------------------------------------------------------------------ Admin API access
ADMIN_KEY="$(cat "$ROOT/.secrets/apisix-admin-key")"
admin_port_forward  # sets ADMIN_URL, kills the port-forward on exit

admin() { # admin METHOD PATH [BODY_FILE]
  local args=(-sS -o "$TMP/resp" -w '%{http_code}' -X "$1" -H "X-API-KEY: $ADMIN_KEY")
  [[ $# -ge 3 ]] && args+=(-H 'Content-Type: application/json' --data-binary "@$3")
  curl "${args[@]}" "$ADMIN_URL/apisix/admin/$2"
}

# ------------------------------------------------------------------ template variables
vars="$(jq -n \
  --arg dk "$(cat "$ROOT/.secrets/consumer-demo-user")" \
  --arg rk "$(cat "$ROOT/.secrets/consumer-researcher")" \
  --arg ek "$(cat "$ROOT/.secrets/consumer-executor")" \
  --arg pk "$(cat "$ROOT/.secrets/consumer-probe")" \
  --arg model "$OLLAMA_MODEL" --argjson seed "$OLLAMA_SEED" --arg url "$OLLAMA_URL" \
  '{CONSUMER_KEY_DEMO_USER: $dk, CONSUMER_KEY_RESEARCHER: $rk, CONSUMER_KEY_EXECUTOR: $ek,
    CONSUMER_KEY_PROBE: $pk, OLLAMA_MODEL: $model, OLLAMA_SEED: $seed, OLLAMA_URL: $url}')"

RENDER='
  def var($n): if $vars | has($n) then $vars[$n] else error("template variable \($n) is not set") end;
  walk(if type == "string" then
         if test("^\\$\\{[A-Z0-9_]+\\}$") then var(.[2:-1])
         else gsub("\\$\\{(?<n>[A-Z0-9_]+)\\}"; var(.n) | tostring) end
       else . end)'
render() { jq --argjson vars "$vars" "$RENDER" "$1"; }

provider="$GW/upstreams/llm.$LLM_MODE.json"
[[ -f "$provider" ]] || die "no LLM provider config $provider"
vars="$(jq --argjson p "$(render "$provider")" '. + {LLM_PROVIDER: $p}' <<<"$vars")"

# ------------------------------------------------------------------ desired vs live
# subset(d; l): every key/value in d is present and equal in l (arrays compared exactly)
SUBSET='
  def subset($d; $l):
    if ($d | type) == "object" then ($l | type) == "object" and all($d | keys[]; . as $k | subset($d[$k]; $l[$k]))
    elif ($d | type) == "array" then ($l | type) == "array" and ($d | length) == ($l | length)
      and all(range($d | length); . as $i | subset($d[$i]; $l[$i]))
    else $d == $l end;'

# preserve_kill KIND DESIRED LIVE: keep an engaged kill switch from the live object
preserve_kill() {
  jq --argjson live "$3" --arg kind "$1" '
    if $kind == "plugin_configs" and ($live.plugins["fault-injection"]._meta.disable == false) then
      .plugins["fault-injection"]._meta.disable = false
    elif $kind == "consumers" and ($live.plugins["fault-injection"] != null) then
      .plugins["fault-injection"] = $live.plugins["fault-injection"]
    else . end' <<<"$2"
}

changed=0
apply_kind() { # apply_kind KIND ID_FIELD
  local kind="$1" idf="$2" f id desired live code
  for f in "$GW/$kind"/*.json; do
    [[ "$kind" == upstreams && "$(basename "$f")" == llm.*.json ]] && continue  # ai-proxy provider fragments
    desired="$(render "$f")"
    id="$(jq -r ".$idf" <<<"$desired")"
    code="$(admin GET "$kind/$id")"
    live='null'; [[ "$code" == 200 ]] && live="$(jq '.value' "$TMP/resp")"
    desired="$(preserve_kill "$kind" "$desired" "$live")"
    if [[ "$live" != null ]] && jq -en --argjson d "$desired" --argjson l "$live" "$SUBSET subset(\$d; \$l)" >/dev/null; then
      continue
    fi
    printf '%s' "$desired" >"$TMP/body"
    code="$(admin PUT "$kind/$id" "$TMP/body")"
    [[ "$code" == 200 || "$code" == 201 ]] || die "PUT $kind/$id failed ($code): $(jq -c '.error_msg // .' "$TMP/resp")"
    log "applied $kind/$id"
    changed=$((changed + 1))
  done
}

verify_kind() { # verify_kind KIND ID_FIELD: fail on any drift between gateway/ and APISIX
  local kind="$1" idf="$2" f id desired live
  for f in "$GW/$kind"/*.json; do
    [[ "$kind" == upstreams && "$(basename "$f")" == llm.*.json ]] && continue
    desired="$(render "$f")"; id="$(jq -r ".$idf" <<<"$desired")"
    [[ "$(admin GET "$kind/$id")" == 200 ]] || die "drift: $kind/$id missing after apply"
    live="$(jq '.value' "$TMP/resp")"
    desired="$(preserve_kill "$kind" "$desired" "$live")"
    jq -en --argjson d "$desired" --argjson l "$live" "$SUBSET subset(\$d; \$l)" >/dev/null \
      || die "drift: live $kind/$id differs from gateway/$kind/$(basename "$f")"
  done
}

# Dependencies first: plugin configs and consumers before the routes that use them
log "bootstrapping gateway (LLM_MODE=$LLM_MODE)"
apply_kind plugin_configs id
apply_kind consumers username
apply_kind upstreams id
apply_kind routes id

verify_kind plugin_configs id
verify_kind consumers username
verify_kind upstreams id
verify_kind routes id
log "gateway config verified ($changed object(s) changed)"
