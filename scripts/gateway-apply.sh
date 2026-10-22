#!/usr/bin/env bash
# Apply gateway/ (config as code, GW-08) to APISIX's Admin API, then read everything back
# and fail on drift. Shared by both targets: scripts/bootstrap-gateway.sh calls it through
# an Admin API port-forward on k3d; the gateway-bootstrap Job runs it in the cluster on
# OpenShift (OS-10). It needs only bash, curl and jq.
#
# Inputs (environment):
#   ADMIN_URL, ADMIN_KEY          the Admin API and its admin key
#   GW                            the gateway/ directory
#   LLM_MODE                      mock | live (selects gateway/upstreams/llm.<mode>.json)
#   CONSUMER_KEY_DEMO_USER, CONSUMER_KEY_RESEARCHER, CONSUMER_KEY_EXECUTOR, CONSUMER_KEY_PROBE
#   OLLAMA_MODEL, OLLAMA_SEED, OLLAMA_URL   the live LLM (template variables)
#   NS_AI, NS_OBS, NS_GATEWAY     the demo namespaces (service addresses, log labels)
#   LOKI_URL, LOKI_PUSH_PATH      where APISIX's loki-logger pushes access logs
#   LOKI_HEADERS                  its request headers, as a JSON object ({} for none)
#
# Templates: a string that is exactly "${VAR}" becomes VAR's typed value (string, number
# or object); "${VAR}" inside a longer string, or in an object key, is interpolated.
# Unknown variables fail.
#
# Idempotent (REP-07): an object whose live value already matches is not written.
# Kill-switch state is preserved: an active fault-injection in a plugin config or on a
# consumer is carried over, so a re-apply during an incident never silently lifts a kill.
set -euo pipefail
declare -F log >/dev/null || log() { printf '==> %s\n' "$*"; }
declare -F die >/dev/null || die() { printf 'error: %s\n' "$*" >&2; exit 1; }
for v in ADMIN_URL ADMIN_KEY GW LLM_MODE CONSUMER_KEY_DEMO_USER CONSUMER_KEY_RESEARCHER \
         CONSUMER_KEY_EXECUTOR CONSUMER_KEY_PROBE OLLAMA_MODEL OLLAMA_SEED OLLAMA_URL NS_AI NS_OBS \
         NS_GATEWAY LOKI_URL LOKI_PUSH_PATH LOKI_HEADERS; do
  [[ -n "${!v:-}" ]] || die "gateway-apply: $v is not set"
done
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

admin() { # admin METHOD PATH [BODY_FILE]
  local args=(-sS -o "$TMP/resp" -w '%{http_code}' -X "$1" -H "X-API-KEY: $ADMIN_KEY")
  [[ $# -ge 3 ]] && args+=(-H 'Content-Type: application/json' --data-binary "@$3")
  curl "${args[@]}" "$ADMIN_URL/apisix/admin/$2"
}

# ------------------------------------------------------------------ template variables
vars="$(jq -n \
  --arg dk "$CONSUMER_KEY_DEMO_USER" --arg rk "$CONSUMER_KEY_RESEARCHER" \
  --arg ek "$CONSUMER_KEY_EXECUTOR" --arg pk "$CONSUMER_KEY_PROBE" \
  --arg model "$OLLAMA_MODEL" --argjson seed "$OLLAMA_SEED" --arg url "$OLLAMA_URL" \
  --arg nsai "$NS_AI" --arg nsobs "$NS_OBS" --arg nsgw "$NS_GATEWAY" \
  --arg lokiurl "$LOKI_URL" --arg lokipath "$LOKI_PUSH_PATH" --argjson lokihdr "$LOKI_HEADERS" \
  '{CONSUMER_KEY_DEMO_USER: $dk, CONSUMER_KEY_RESEARCHER: $rk, CONSUMER_KEY_EXECUTOR: $ek,
    CONSUMER_KEY_PROBE: $pk, OLLAMA_MODEL: $model, OLLAMA_SEED: $seed, OLLAMA_URL: $url,
    NS_AI: $nsai, NS_OBS: $nsobs, NS_GATEWAY: $nsgw,
    LOKI_URL: $lokiurl, LOKI_PUSH_PATH: $lokipath, LOKI_HEADERS: $lokihdr}')"

RENDER='
  def var($n): if $vars | has($n) then $vars[$n] else error("template variable \($n) is not set") end;
  def interp: gsub("\\$\\{(?<n>[A-Z0-9_]+)\\}"; var(.n) | tostring);
  walk(if type == "string" then
         if test("^\\$\\{[A-Z0-9_]+\\}$") then var(.[2:-1]) else interp end
       elif type == "object" then with_entries(.key |= interp)   # upstream nodes are keyed by host:port
       else . end)'
render() { jq --argjson vars "$vars" "$RENDER" "$1"; }

provider="$GW/upstreams/llm.$LLM_MODE.json"
[[ -f "$provider" ]] || die "no LLM provider config $provider"
vars="$(jq --argjson p "$(render "$provider")" --argjson m "$(render "$GW/upstreams/llm.mock.json")" \
  '. + {LLM_PROVIDER: $p, LLM_PROVIDER_MOCK: $m}' <<<"$vars")"

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
      | .labels = (($live.labels // {}) | with_entries(select(.key | startswith("killswitch."))))
    elif $kind == "consumers" and ($live.plugins["agent-kill-switch"] != null) then
      .plugins["agent-kill-switch"] = $live.plugins["agent-kill-switch"]
    else . end' <<<"$2"
}

# object_id FILE ID_FIELD DESIRED: the Admin API id; "@file" means the file's basename
# (plugin metadata is keyed by plugin name and has no id field)
object_id() {
  if [[ "$2" == @file ]]; then basename "$1" .json; else jq -r ".$2" <<<"$3"; fi
}

changed=0
apply_kind() { # apply_kind KIND ID_FIELD
  local kind="$1" idf="$2" f id desired live code
  for f in "$GW/$kind"/*.json; do
    [[ "$kind" == upstreams && "$(basename "$f")" == llm.*.json ]] && continue  # ai-proxy provider fragments
    desired="$(render "$f")"
    id="$(object_id "$f" "$idf" "$desired")"
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
    desired="$(render "$f")"; id="$(object_id "$f" "$idf" "$desired")"
    [[ "$(admin GET "$kind/$id")" == 200 ]] || die "drift: $kind/$id missing after apply"
    live="$(jq '.value' "$TMP/resp")"
    desired="$(preserve_kill "$kind" "$desired" "$live")"
    jq -en --argjson d "$desired" --argjson l "$live" "$SUBSET subset(\$d; \$l)" >/dev/null \
      || die "drift: live $kind/$id differs from gateway/$kind/$(basename "$f")"
  done
}

# Dependencies first: plugin configs and consumers before the routes that use them
log "bootstrapping gateway (LLM_MODE=$LLM_MODE)"
apply_kind plugin_metadata @file
apply_kind plugin_configs id
apply_kind consumers username
apply_kind upstreams id
apply_kind routes id

verify_kind plugin_metadata @file
verify_kind plugin_configs id
verify_kind consumers username
verify_kind upstreams id
verify_kind routes id
log "gateway config verified ($changed object(s) changed)"
