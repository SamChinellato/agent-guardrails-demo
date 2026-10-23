# Runbook

Day-to-day operation: checking health, using the kill switch, investigating what happened, changing policy and the LLM mode, resetting, and fixing common problems. Installing is in [DEPLOYMENT.md](DEPLOYMENT.md); the presenter's talk track is [DEMO_SCRIPT.md](DEMO_SCRIPT.md).

**Target support.** The `make` targets act on k3d unless they say otherwise; most don't honour `TARGET=openshift` yet ([roadmap](OPENSHIFT_MIGRATION.md#future-roadmap)). Each section gives the OpenShift way where it differs.

## Is it healthy?

| | k3d | OpenShift |
| --- | --- | --- |
| Overall | `make status`: pods, kill-switch state per scope, guard policy version and modes, firing alerts | `oc get pods -n aigw-ai -n aigw-gateway -n aigw-observability`, and the Argo CD Applications `aigw-workloads` and `aigw-apisix` (Synced, Healthy) |
| Logs of one service | `make logs SVC=guard` (any Deployment or StatefulSet name) | `TARGET=openshift make logs SVC=guard`, or `oc -n aigw-ai logs -f deploy/guard` |
| Network policies | run by `make up`; `make test` asserts each flow | `TARGET=openshift scripts/openshift/verify-netpol.sh` |
| Repair | `make up` again: it fixes anything not running and keeps data, the policy and any engaged kill | Argo CD self-heals; a failed sync shows its reason in the Application |

## The kill switch

### Trip and reset

| How | Trip | Reset |
| --- | --- | --- |
| Control panel (k3d `:8090`, OpenShift `aigw-killswitch.<domain>`) | Pick a scope, give a reason, **Trip** | **Reset**, with a reason. Resetting *Everything* needs `RESET GLOBAL` typed in |
| CLI (k3d) | `make trip SCOPE=agent:executor REASON="drill"` | `make kill-reset SCOPE=agent:executor REASON="cleared"` |
| API | `POST /v1/trip {scope, reason, actor, source}` | `POST /v1/reset {scope, reason, actor, confirm?}` |

Scopes: `agent:researcher`, `agent:executor`, `tools` (every MCP call), `global` (all AI traffic). The panel's operator token is in `.secrets/killswitch-operator-token`.

What to expect:

- **Within milliseconds** the affected calls get 503 `kill_switch_engaged`, and agents stop with `halted_by_kill_switch`.
- **Every change is verified:** the kill switch reads APISIX back and, for `global` and `tools`, sends a probe through the gateway. An event with `verified: false` (and an HTTP 500) means APISIX didn't take the change: check APISIX and the kill switch's logs.
- **Calls already in flight finish** (KS-11). LLM calls aren't streamed, so at most one model call per agent completes after a trip.
- **Tripping something already tripped** records an event and changes nothing (KS-03).
- **Repeated trips of one target within 10 minutes join one incident** (KS-12).

### Automatic trips

- **The guard** trips an agent on any critical violation in enforce mode: a tool outside its allowlist, a tool denied to everyone, or a forbidden argument. The trip completes before the blocked call is answered.
- **Alerts** trip an agent for its behaviour: repeated blocks, token burn, or a tool-call spike ([OBSERVABILITY.md](OBSERVABILITY.md#alerts-that-stop-agents)).

An alert that was already firing before you reset won't trip the agent again (D-037). If an agent keeps getting tripped after a reset, it's still misbehaving: find out why before resetting again.

## Investigating an incident

1. **Start from the console**, if the incident happened there: **why?** on the kill-switch notice, or **Decision trail** under the reply. Both open the Decision trail dashboard for that request's trace.
2. **The Decision trail** shows, for one trace: the guard's decisions in order (verdict, check, mode, reason, rule, policy version), the kill-switch actions with the same decision ID, and the full trace.
3. **The Incident dashboard** shows what was stopped and for how long, with guard blocks and trips as annotations.
4. **The kill-switch panel** lists recent events: scope, source, reason, incident ID, verification time.
5. **Export it** (k3d): `make evidence INCIDENT=<incident id>` (or `TRACE=<trace id>`), then `make evidence-verify DIR=evidence/<id>`. The bundle and what it proves are described in [OBSERVABILITY.md](OBSERVABILITY.md#evidence-bundles).

## Changing guard policy

The policy baseline is `policy/policy.yaml`; the live copy is the `guard-policy` ConfigMap. The guard picks up a change within about 3 s (D-059), and refuses a file that doesn't parse, keeping the previous policy. Every decision records the `policy_version` it was made under.

| | k3d | OpenShift |
| --- | --- | --- |
| Change one check's mode | `make policy-set CHECK=mcp.tool_result_injection MODE=monitor` | `oc -n aigw-ai edit configmap guard-policy` |
| Back to the baseline | `make reset`, or **New chat** in the console | **New chat** in the console |
| Change the baseline | Edit `policy/policy.yaml`, then `make reset` | Edit `policy/policy.yaml` and push; `guard-policy-baseline` follows git, then **New chat** |

Modes: `enforce` (block), `monitor` (record a flag, let it through), `off`. The checks are listed in [CONFIGURATION.md](CONFIGURATION.md#guard-policy).

## Switching the LLM

```bash
make llm-mode MODE=mock          # k3d
make llm-mode MODE=live TARGET=openshift
```

Only the gateway's LLM upstream changes; nothing restarts, and it takes a few seconds (D-060). On k3d, `live` needs the host's Ollama (it runs the live prerequisites check first); on OpenShift it refuses `live` while llama.cpp isn't ready. The story prompts in the console are scripted and use the mock model in either mode; free chat follows the mode.

## Resetting the demo

| | What it does |
| --- | --- |
| **New chat** (console, both targets) | Clears the conversation, opens every kill switch, restores the policy baseline, empties uploaded documents |
| `make reset` (k3d) | Opens every kill switch, clears the guard's auto-trip memory, restores the policy, clears the email outbox and restores supplier records, archives the audit log and starts a new chain, then waits for kill-switch alerts to clear. Under 2 minutes |

## Troubleshooting

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| An agent stays stopped | A kill switch is still engaged | **New chat** in the console, or reset the scope in the panel |
| "Where it went" says the trace hasn't arrived; the decision trail is empty | Telemetry takes a few seconds | Wait about 10 s and try again |
| Every call gets 503 without `kill_switch_engaged` | The guard is down or not ready; the gateway fails closed | Check the guard's pod. It's ready only after its model has loaded and its policy has parsed |
| A live answer is wordy or wrong | The small local model varies | The gateway behaves the same; ask again, or use the scripted story prompts |
| The console says it couldn't reach its server (OpenShift) | A request over the router or proxy timeout | Routes and the console proxy allow 10 minutes (D-061); check that the console Route still carries `haproxy.router.openshift.io/timeout` |
| Grafana dashboards or datasources missing after a restart (OpenShift) | The operator's Grafana keeps its database on an emptyDir | They come back within a minute (resync period) |
| Grafana shows its own login page (OpenShift) | The OAuth proxy forwarded Basic auth | All proxies must set `--pass-basic-auth=false` (D-058) |
| Argo CD shows `ComparisonError` | The repo-root kustomization doesn't build | Run `oc kustomize .` locally to see the error; `make openshift-check` catches a stale root kustomization |
| Gateway plugin change has no effect | APISIX loads custom plugins at start | k3d: `make up` rolls APISIX. OpenShift: `oc -n aigw-gateway rollout restart deploy/apisix` |
| `make up` fails at the network policy check (rootless Podman) | kube-router can't apply its rules | Set `net.core.wmem_default = 2097152` on the host (D-029); `make prereqs` prints the fix |
| A port is already in use (k3d) | Another service on the host | Override it in `.env.local`, for example `HOST_PORT_GRAFANA=3001` |
| `oc` commands fail with `Unauthorized` | Your OpenShift login token expired | `oc login` again |
| The OpenShift Loki Route answers | It was recreated with the LokiStack | `TARGET=openshift scripts/openshift/loki-route.sh` (D-063) |
