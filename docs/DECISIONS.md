# Decisions and verified-API notes

Every deviation from `SPEC.md`, every version choice and every API discrepancy found while verifying (spec rule 2) is recorded here. Entries are append-only; superseded entries are marked, not deleted.

## D-001 Repository root is `api-days-demo/` (P0)

The spec's layout calls the root `ai-gateway-demo/`. The repo lives in the existing `api-days-demo/` directory. All paths inside the layout are unchanged.

## D-002 Ollama runs on the host, not in the cluster (P0)

**Change.** The operator's request. The spec (sections 3 and 7.5) puts Ollama in the `ai` namespace as a Deployment with a PVC and a pull Job. Instead, live mode uses the Ollama already installed on the host machine.

**How it works.**

- In live mode, APISIX `ai-proxy` sends requests to the host's Ollama on port 11434. The hostname depends on the container runtime, and `scripts/lib.sh` sets it (`OLLAMA_URL`). On Docker it's `host.k3d.internal`: k3d adds it to CoreDNS, and it resolves to the gateway of the k3d network. On rootless Podman it's `host.containers.internal`; see D-009.
- On Linux, Ollama must listen on all interfaces (`OLLAMA_HOST=0.0.0.0:11434`), because a loopback-only listener can't be reached from containers (verified for Docker; on rootless Podman this host already binds 0.0.0.0, so loopback-only wasn't tested). `scripts/prereqs.sh live` checks this. On Docker Desktop (macOS), `host.k3d.internal` reaches the host's loopback, so the default bind works.
- The model is still pinned by tag and digest (REP-02). The digest is the one the host's Ollama reports in `/api/tags` after `ollama pull`. `make prefetch LLM_MODE=live` pulls it on the host, and `prereqs` fails if the digest differs.
- The Ollama server version isn't pinned, because it's host software. `OLLAMA_MIN_VERSION` sets a floor instead.

**Consequences.**

- No Ollama image, Deployment, PVC or pull Job. The ollama row in the component table and section 7.5's Ollama bullet don't apply.
- NFR-08 still holds: no data leaves the machine. The `ai` namespace keeps egress limited to cluster services. Only the APISIX pods in `gateway` get an egress exception, to the host's IP on TCP 11434 (`169.254.1.2/32` on rootless Podman). Agents still reach the LLM only through APISIX.
- REP-05: live mode no longer needs 12 GB of Docker memory. 8 GB covers both modes, and `prereqs` warns if the host has less than 16 GB RAM (NFR-04).
- REP-06: offline `make up` in live mode depends on the host's Ollama already holding the model, and `prereqs` checks that.
- REP-11 (CI) runs mock mode only, so CI doesn't need Ollama.

## D-003 Version selection policy (P0)

The pins are the latest stable releases at least two weeks old on 2026-10-19 (cut-off 2026-10-05). The aim is to avoid releases too new for regressions to have surfaced. Exceptions are noted below.

| Item | Pinned | Notes |
| --- | --- | --- |
| k3s | v1.36.5+k3s1 | k3s `stable` channel. `latest` was v1.37.1, which is newer than k3d 5.9.0 was tested against |
| k3d (min) | 5.9.0 | latest release |
| APISIX chart / app | 2.18.0 / 3.19.0 | 2.18.2 exists but is only 4 days old |
| kube-prometheus-stack | 91.9.0 | 92.x released inside the cut-off window |
| Tempo chart | 3.1.0 (app 3.1.0) | see D-004 |
| Loki chart | 18.13.8 (app 3.7.8) | see D-004 |
| opentelemetry-collector chart | 0.175.1 (contrib 0.161.0) | image tag matches the chart's appVersion |
| etcd | v3.6.15 | see D-007 |
| Python | 3.12.15 (`python:3.12.15-slim-trixie`) | spec mandates 3.12 |
| uv | 0.12.23 | |
| Ollama model | `qwen3:4b` | spec default (NFR-05); tool-calling smoke test passed, see D-006 |
| Classifier | `protectai/deberta-v3-base-prompt-injection-v2` @ `90c9989b…` | Apache-2.0, not gated |

Chart-internal images (Prometheus, Alertmanager, operator, config reloader, kube-state-metrics, node-exporter, webhook certgen, Grafana, k8s-sidecar) were found by running `helm template` on the pinned charts with default values. They are pinned in `versions.env` so the Helm values in P1 and P2 can reference them by digest. `make prefetch` can then pull them for offline installs.

Digests are multi-arch index digests resolved by `scripts/pin-digests.sh`, so the same pin works on amd64 and arm64 (REP-04). `make pin-check` reports drift without writing.

## D-004 Tempo and Loki charts come from grafana-community (P0)

The `tempo` chart in `grafana/helm-charts` is marked `deprecated: true`; its last release is 1.24.4, from January 2026. Single-binary Tempo and Loki charts are now published at `https://grafana-community.github.io/helm-charts`. That repo also carries Loki 18.x (app 3.7.8), while `grafana/helm-charts` stops at Loki 7.3.0 (app 3.6.12). Both charts come from grafana-community. This is not a component substitution, because the components are still Tempo single binary and Loki SingleBinary.

## D-005 Classifier runs on ONNX Runtime, not PyTorch (P0)

The pinned model revision ships an ONNX export (`onnx/model.onnx` and `onnx/tokenizer.json`). The guard uses `onnxruntime` and `tokenizers`, which the spec allows ("ONNX or PyTorch CPU classifier"). That keeps PyTorch out of the image and the image several GB smaller.

Smoke test on amd64 (2026-10-19), with onnxruntime 1.31.0 on CPU and 512-token truncation:

| Input | Label | Score | Latency |
| --- | --- | --- | --- |
| benign supplier question | SAFE | 1.000 | 16 ms |
| subtle "disregard earlier guidance…" | INJECTION | 1.000 | 16 ms |
| poisoned-doc HTML comment | INJECTION | 0.993 | 20 ms |

That's well inside NFR-03 (250 ms p95).

**arm64 still to verify.** P0 calls for an arm64 load test, but no arm64 host is available here. onnxruntime publishes `manylinux aarch64` wheels, so the risk is low. The test will run on arm64 when the guard image builds in P3, or in CI if an arm64 runner is added. Until then, REP-04 stays `TODO`.

## D-006 qwen3 thinking mode and latency (P0)

Smoke test through the host Ollama's OpenAI-compatible endpoint (`/v1/chat/completions`, `temperature 0`, `seed 42`, one `read_document` tool). The model returned `finish_reason=tool_calls` with arguments `{"id":"supplier-03"}`. Tool calling works.

It took 31 s, though, because qwen3 spent about 500 completion tokens on reasoning before the call. In P4 the agents will turn thinking off: add `/no_think` to the system prompt and cap `max_tokens`, which the risk table already plans for. The new latency will be recorded here. If S0 still drags in live mode, fall back to `qwen2.5:3b` as NFR-05 allows, and record that here.

## D-007 etcd 3.6 rather than 3.7 (P0)

etcd v3.7.2 is the newest release, but the 3.7 line is only a few weeks old and APISIX's etcd client has a longer track record on 3.5/3.6. The conservative pin is v3.6.15, the latest 3.6 patch. P1 verifies APISIX against it; moving to 3.7 later is a deliberate re-pin. The image comes from `quay.io/coreos/etcd`, as the spec requires (no Bitnami subchart; the APISIX chart's bundled etcd now points at `bitnamilegacy/etcd`, which confirms the risk in section 10).

## D-008 APISIX image variant (P0)

The chart's default is `apache/apisix:3.19.0-ubuntu`. The pin is `3.19.0-debian` (smaller base, same APISIX build). `pin-digests.sh` resolved a multi-arch index for it; P1 confirms the amd64 and arm64 entries when the image is pulled. If the custom Lua plugin needs anything only the ubuntu variant has, switch and record it here.

## D-009 Container runtime: Docker or rootless Podman (P0)

**Change.** The spec assumes Docker. This host has rootless Podman 5.4.2, and the user can't reach the Docker socket. The scripts support both. `scripts/lib.sh` picks Docker if its daemon answers and is not Podman, otherwise Podman (`CONTAINER_RUNTIME` overrides). For Podman it exports `DOCKER_HOST` and `DOCKER_SOCK` pointing at the Podman API socket, which k3d uses as if it were Docker. k3d stays the cluster tool, so this is not a component substitution. kind and minikube need the same rootless prerequisites, so switching distro would not have helped.

**Verified on this host (2026-10-19)** with a throwaway cluster:

| Finding | Workaround |
| --- | --- |
| k3s exits with `failed to find cpuset cgroup (v2)`: only `cpu memory pids` were delegated to the user session | One-time host change, `Delegate=cpu cpuset io memory pids` in `/etc/systemd/system/user@.service.d/delegate.conf`, then restart `user@<uid>.service`. `prereqs.sh` checks for it and prints the fix |
| kubelet exits with `open /dev/kmsg: operation not permitted` | k3s arg `--kubelet-arg=feature-gates=KubeletInUserNamespace=true`, added only when the runtime is rootless |
| `k3d registry create` and `registries.create` fail with `unable to find network with name or ID bridge` | Create the registry first with `k3d registry create … --default-network podman`, then reference it from the cluster with `registries.use` |
| `host.k3d.internal` resolves to the rootless network namespace's gateway (`10.89.x.1`), not the host | Podman's pasta maps the host at `169.254.1.2` (`host.containers.internal`). k3d's `--host-alias 169.254.1.2:host.containers.internal` adds it to CoreDNS, and pods reached the host Ollama by name |
| Missing host kernel modules (`br_netfilter`, `iptable_nat`) produce warnings | None needed: Service DNS, NodePort through the k3d load balancer, and pod-to-service traffic all worked |
| podman API socket disappears after a `user@` restart | `prereqs.sh` checks for it; fix with `systemctl --user enable --now podman.socket` |

NetworkPolicy works under rootless Podman. In the test, an ingress allow-list blocked an unlabelled pod and allowed a labelled one. An egress policy allowed `169.254.1.2:11434` and blocked the internet.

Pushing images from the host goes to `127.0.0.1:5000`, because `*.localhost` resolved to `::1` only on this host and the rootless port forward listens on IPv4. Pods pull the same image as `k3d-registry.localhost:5000/…` (verified).

## D-010 k3d, kubectl and helm are pinned and installed into the repo (P0)

`make tools` downloads k3d 5.9.0, kubectl 1.36.5 and helm 4.3.0 into `.tools/bin` (gitignored) and checks each against a SHA-256 in `versions.env`. Checksums are pinned for linux and darwin, amd64 and arm64. Scripts put `.tools/bin` first on `PATH`, so a fresh clone needs only the container runtime, uv, jq, make and curl, and nothing is installed system-wide. Helm 4 is the current major version, and the charts pinned in D-003 are Helm 3 charts, which Helm 4 still installs; P1 confirms that for each chart. The demo uses its own kubeconfig, `.kube/config`, so `~/.kube/config` is never touched.

## D-011 Host ports are overridable (P0)

The spec fixes ports 8080 (gateway), 3000 (Grafana) and 8090 (control panel). On this host, port 3000 is already used by another pod. Those remain the defaults, and `HOST_PORT_GATEWAY`, `HOST_PORT_GRAFANA` and `HOST_PORT_PANEL` override them. `prereqs.sh` checks the ports and names the container holding one. The NodePorts inside the cluster (30080, 30300, 30090) don't change.

## D-012 Image tags for uncommitted work (P1)

Built images are tagged `<service>:<git-short-sha>` (REP-03). All services share one tag, because they share `demo-common` and one `uv.lock`. When `services/` has uncommitted changes, the tag gets a content-hash suffix (`<sha>-<hash8>`). A rebuild of work in progress then never reuses a stale image under a tag that already exists. On a clean checkout the tag is exactly the short SHA. `build-images.sh` skips the build when the registry already holds the tag.

## D-013 Per-agent kill uses a custom `agent-kill-switch` plugin on the consumer (P1)

**Finding.** The spec (KS-01, 7.2) asks the killswitch to add a `fault-injection` abort to the consumer. On APISIX 3.19 that never fires on our routes. Tested on the running gateway, three rounds each:

| Consumer `fault-injection` | Route has `fault-injection`? | Result |
| --- | --- | --- |
| added | no (scratch route) | 503, every time |
| added | yes (disabled, from `pc-llm`, as GW-03 requires) | 200, never fires |

When APISIX merges consumer plugins into a route, it only re-runs the rewrite phase for plugins the route doesn't already have. `fault-injection` aborts in the rewrite phase, at priority 11000, before `key-auth` has found the consumer. So the consumer's copy is merged but never executed. GW-03 (pre-staged `fault-injection` in every plugin config) and a consumer-scoped `fault-injection` cancel each other out.

**Fallback considered.** The spec's fallback is to swap the consumer's key for a revoked value. That answers 401 `Invalid API key`, which breaks KS-06: agents must get a 503 `kill_switch_engaged` and halt cleanly, and a 401 looks like a misconfiguration.

**Chosen.** A 70-line custom plugin, `gateway/plugins/agent-kill-switch.lua`, loaded through the same custom-plugins mechanism as GW-09. The killswitch adds `{"agent-kill-switch": {"incident_id": …}}` to the consumer and removes it on reset. No route uses that plugin name, so APISIX runs it after authentication, in the access phase at priority 2390. That's after `consumer-restriction` (2400) and before every AI plugin and the upstream. It answers 503 with the same body shape as the global kill for that route type: a JSON-RPC error on `pc-mcp` routes, an OpenAI-style `kill_switch_engaged` error elsewhere. It also includes the `incident_id`.

**Verified.** The executor was cut off within 14–24 ms over three rounds, the researcher was unaffected, and reset took about 20 ms. Covered by `test_agent_scoped_kill_affects_only_that_agent`. KS-01 is marked `DEVIATION` (method only; the behaviour is as specified).

## D-014 Consumers can't be PATCHed; the killswitch uses GET + PUT for them (P1)

`PATCH /apisix/admin/consumers/<name>` returns 405 on 3.19. Section 7.4 says Admin API changes use PATCH. That still holds for plugin configs, where `PATCH` with `{"plugins":{"fault-injection":{"_meta":{"disable":false}}}}` works. For consumers, the killswitch will GET the consumer, change its `plugins`, PUT the whole object back, and GET it again to verify. The read-back verification (KS-04) is unchanged.

## D-015 APISIX 3.19 plugin facts, verified on the running gateway (P1)

- **Plugins loaded.** All 12 configured plugins appear in `/apisix/admin/plugins/list`, including `ai-proxy-multi`.
- **Priorities.** `opentelemetry` 12009, `fault-injection` 11000, `request-validation` 2800, `key-auth` 2500, `consumer-restriction` 2400, `ai-prompt-guard` 1072, `ai-proxy-multi` 1041, `ai-proxy` 1040, `proxy-rewrite` 1008, `prometheus` 500, `loki-logger` 414, `http-logger` 410.
  - A global kill rejects before authentication, as 7.2 expects. Verified: an anonymous request gets 503 while `pc-llm` is tripped.
  - `opentelemetry` runs before the kill, so killed requests still produce spans.
- **`request-validation` priority.** It would run before `key-auth`, so an unauthenticated `stream: true` request would get 400 rather than 401. The route sets `_meta.priority: 2300` so authentication and consumer restriction come first. Its rejection body is plain text, from `rejected_msg`.
- **`ai-proxy` config.** `provider: openai-compatible`, `override.endpoint` (full URL), `options` (merged into the body; `options.model` replaces the client's model), `timeout` in ms (120000), and `override.llm_options.max_tokens` to cap output in live mode. `auth` is required even though neither backend checks it, so it carries a dummy bearer token.
- **`ai-proxy` forwards client headers to the backend.** `X-Demo-Scenario` reaches mock-llm, and so does the `X-Agent-Id` set by `proxy-rewrite` from `$consumer_name`. A client-supplied `X-Agent-Id` is overwritten. Covered by `test_agent_id_comes_from_consumer_not_client`.
- **`ai-prompt-guard`** rejects with 400 `{"message":"Request contains prohibited content"}`. It checks only the latest message, and only the user role, by default (`match_all_roles` and `match_all_conversation_history` are false).
- **Consumer `key-auth` keys** are stored encrypted in etcd but come back decrypted from the Admin API, so the bootstrap drift check can compare them directly.

## D-016 Helm and bootstrap idempotence (P1)

`helm upgrade` always creates a new revision, even when nothing changed. `helm_install` in `scripts/lib.sh` hashes the chart archive, the values file and the `--set` arguments, and stores the hash in a `demo-helm-state` ConfigMap. Releases whose hash hasn't changed are skipped. The custom plugins are mounted by `subPath`, which doesn't follow ConfigMap updates, so their checksum goes into a pod annotation. A change to plugin code changes the hash, and the pod rolls.

`bootstrap-gateway.sh` PUTs only objects whose live value differs from `gateway/`, then reads back every object and fails on drift (GW-08). It keeps an engaged kill switch, both a plugin config's enabled `fault-injection` and a consumer's `agent-kill-switch` (D-013), so `make up` can't silently lift a kill. Measured: a second `make up` changes no object and finishes in 8 s; a cold `make up` takes 68 s on this host.

## D-017 Gateway bootstrap runs from the host through a port-forward, not a Job (P1)

GW-07 mentions a "bootstrap Job" among the Admin API's allowed clients. The bootstrap instead runs on the host, through a deliberate `kubectl port-forward`, as rule 6 allows. That reuses the same `.secrets/`, templates and drift check as the rest of the CLI. Port-forwarded traffic arrives on the pod's loopback, which `allow_admin` permits (`127.0.0.1/32`), and NetworkPolicy doesn't filter it. So the P3 NetworkPolicy will allow port 9180 from the killswitch pod only.

## D-018 Live mode works end to end; qwen3 thinking is off (P1, updates D-006)

With the gateway in live mode, a chat completion went researcher → APISIX `ai-proxy` → `host.containers.internal:11434` → host Ollama → `qwen3:4b`. It took 5.5 s with `/no_think` in the system prompt, against 31 s with thinking on (D-006). The agents (P4) will send `/no_think`. The route caps output at 1024 tokens.

## D-019 Presenter-requested live demo: chat, typed injection, rogue agent (P1, plan)

The operator asked for a finished demo that includes three live pieces, beyond the spec's scripted scenarios. They become requirements `DEMO-01`–`DEMO-03` in `REQUIREMENTS_STATUS.md`.

- **DEMO-01 Agent chat.** A single-page chat at `localhost:8080/console` lets the presenter talk to the researcher agent live, on the host Ollama.
  - The page is served through APISIX, so no new host port.
  - Its small backend (`console` service) holds the `demo-user` key, and the browser never sees a secret.
  - The agent gets a chat endpoint, `POST /chat {messages}`, next to `/task`.
- **DEMO-02 Typed injection filtered before the agent.** The `agent-researcher` route gets the same two inbound filters as the LLM route: `ai-prompt-guard` (regex) and `ai-injection-guard` (the classifier, P3), under a new guard check `agent.user_prompt_injection`.
  - The chat body uses OpenAI-style `messages`, so both plugins apply without change.
  - A blocked message returns 400 or 403 with the decision ID, and the agent never sees it.
  - The LLM-route checks stay as a second layer.
- **DEMO-03 Rogue agent shut down for anomalous behaviour.** S6 is no longer a stretch goal.
  - A "rogue" switch in the console starts a scripted runaway: the agent fires tool calls in a tight loop and token use spikes.
  - Prometheus alert rules on per-agent tool-call rate (`AgentToolCallSpike`) and token rate (`TokenBurn`) fire, Alertmanager calls the killswitch (F6), and the killswitch trips that agent.
  - The chat shows `halted_by_kill_switch`, and Grafana shows the spike and the kill.
  - Windows are short enough for a live audience, inside the KS-05 alert-driven bound of 60 s.
- **Hybrid LLM routing in live mode.** A second LLM route matches requests carrying a scripted `X-Demo-Scenario` and sends them to mock-llm; everything else goes to Ollama. The rogue run and the S0–S5 cascade then behave the same every time, while free chat stays live. The route still applies the same guards and kill switch.

## D-020 APISIX 3.19 `opentelemetry` reads its collector from plugin metadata (P2)

**Finding.** With the collector configured only in `plugin_attr` (`config.yaml`, as 7.2 describes), APISIX produced no spans, and `$opentelemetry_trace_id` stayed empty. The log said `plugin_metadata is required for opentelemetry plugin to working properly`. On 3.19 the collector address, resource, batching and `set_ngx_var` live in the plugin's **metadata** schema, read from the plugin source in the container.

**Chosen.** `gateway/plugin_metadata/opentelemetry.json`, applied and drift-checked by `bootstrap-gateway.sh` like every other object. Its id is the file name, because plugin metadata is keyed by plugin name. `set_ngx_var: true` also stays in `plugin_attr`, because the APISIX CLI declares `$opentelemetry_trace_id` and `$opentelemetry_span_id` in `nginx.conf` only from there. The batch timeouts are 1 s.

**Verified.** One request gives one trace containing APISIX's server span, with mock-llm's span as its child (OBS-02). The trace was searchable in Tempo after 1.6 s.

## D-021 Telemetry paths (P2)

| Signal | Path | Note |
| --- | --- | --- |
| Service traces and logs | OTLP/HTTP → collector → Tempo (`otlp_grpc`) / Loki (`/otlp`) | `demo-common` exports both. Logs also go to stdout as JSON. In Loki, `trace_id` and `span_id` are structured metadata |
| APISIX spans | `opentelemetry` plugin → collector → Tempo | D-020 |
| APISIX access logs | `loki-logger` → Loki push API, directly | The spec allows `loki-logger` or `http-logger`. Going direct avoids putting a generic HTTP receiver on the collector. Fields: trace_id, span_id, consumer, route_id, method, uri, status, latency, upstream. Bodies are off (GW-06) |
| Metrics | Prometheus scrapes APISIX, demo services (`demo/metrics=true` Services) and the collector's OTLP→Prometheus exporter, all every 5 s | |

Other notes:
- The collector chart (0.175.1) renames exporters to the newer names (`otlp` → `otlp_grpc`, `otlphttp` → `otlp_http`), so the values use the new names.
- kube-prometheus-stack must be installed first, because the other charts' ServiceMonitors need its CRDs.
- Loki's trace↔log queries use `| json | trace_id="…"`. That matches both the structured-metadata OTLP logs and APISIX's JSON lines.
- **Gateway-measured tokens.** APISIX 3.19 exports `apisix_llm_prompt_tokens` and `apisix_llm_completion_tokens` per consumer. The `TokenBurn` and rogue-agent alerts (KS-10, DEMO-03) will use these rather than the agents' self-reported `agent_llm_tokens_total`, so a compromised agent can't hide its usage.
- **Grafana access.** Anonymous Viewer access is on so the presenter can open dashboards without logging in. The port is bound to `127.0.0.1` only. Admin login uses the generated password.
- **Analytics off.** Grafana, Loki and Tempo usage reporting are disabled (NFR-08).

## D-022 Dashboards are generated from `observability/dashboards/build.py` (P2)

The three OBS-05 dashboards are committed as JSON, which is what the Grafana sidecar loads, together with the small script that generates them. The script keeps the visual rules consistent across panels:
- one y-axis per panel and 2 px lines
- a fixed colour per agent, never cycled
- status colours reserved: red means blocked or killed
- stat tiles for headline numbers, and a legend on every multi-series panel
- 5 s refresh and a shared crosshair

Panels for metrics that later phases add (`guard_decisions_total`, `agent_tool_calls_total`, `killswitch_active`, `killswitch_events_total`) are already wired, and show no data until those services exist. The guard-block and kill-switch annotations work the same way.

## D-023 Host ports listen on all interfaces by default (P2)

At the operator's request, the gateway, Grafana and control-panel ports bind to `HOST_BIND=0.0.0.0` by default, so the demo can be opened from another machine on the local network, such as a presenter laptop. `make up` prints the LAN URLs. Set `HOST_BIND=127.0.0.1` to keep it on this machine only. k3d fixes port mappings when it creates the cluster, so `up.sh` refuses to continue when the existing cluster's binding differs, and says to run `make down && make up`.

Consequences:
- Grafana's anonymous Viewer access (D-021) is visible to the LAN.
- Gateway calls still need consumer keys, and the control panel needs the operator token (KS-08).
- Rule 6 is unaffected. The APISIX Admin API is never mapped to the host, and the image registry stays on `127.0.0.1:5000`.

Verified from the LAN address `192.168.1.233`: the gateway answered 401 to a request without a key, and Grafana's health endpoint returned 200.

## D-024 Dashboards stay minimal (P2, supersedes the layout in D-022)

At the operator's request, each dashboard tells one story with a handful of panels and no log walls, trace tables or panel descriptions. Logs and traces stay one click away in Explore and through the log↔trace links (OBS-04).

| Dashboard | Panels |
| --- | --- |
| AI Gateway Overview | Requests/s · Blocked (15 min) · Kill switches on · LLM tokens/min; Requests by agent; Allowed · blocked · killed |
| Agent Behaviour | Tokens/min (with the TokenBurn threshold) · Tool calls/min · Guard blocks/min, per agent |
| Incident | Kill switches on · Alerts firing · Guard blocks; Kill-switch state timeline; Cut off by the kill switch |

Guard blocks and kill-switch events appear as annotations on every dashboard.

## D-025 APISIX admin UI via `make admin-ui` (P2)

At the operator's request, APISIX's built-in admin UI (`enable_admin_ui: true`, served at `/ui/` on the Admin API port) is available, without breaking rule 6. `make admin-ui` opens a deliberate `kubectl port-forward` on `127.0.0.1:9180` and points to the **viewer** key in `.secrets/`, which can read every object but can't change any (verified: a viewer PUT is refused). The admin key is there too for deliberate edits, which the next `bootstrap-gateway.sh` reports as drift. The UI isn't reachable from the LAN or from the k3d port mappings (verified). To use it from another machine, SSH-tunnel port 9180.

Machine-local settings, such as `HOST_PORT_GRAFANA=3001` on this host, live in a gitignored `.env.local`. `scripts/lib.sh` and the test suite both read it, and values already in the environment take precedence.

## D-026 The classifier scores each paragraph and HTML comment separately (P3)

**Finding.** Scored as one block, the poisoned `supplier-07.md` gets an injection probability of **0.004**: its hidden instructions are diluted by the benign text around them. Its HTML comment alone scores 0.99.

**Chosen.** `guard/classifier.py` splits text into visible paragraphs plus the content of every HTML comment, scores each one (with overlapping 512-token windows for long segments), and takes the maximum. The poisoned document now scores 1.000. A regex heuristic list (`guard/signals.py`) is the second signal (GRD-01). Its score is used when it is higher than the model's, and the matching rule is recorded as `rule_id`.

**Measured on the fixtures,** on CPU at about 20–30 ms per prompt:

| Fixture set | Classifier score |
| --- | --- |
| 5 crude attacks | 1.000 (blocked first by `ai-prompt-guard`) |
| 5 subtle attacks | 1.000 |
| 10 benign prompts | 0.000 |
| 5 benign documents | ≤ 0.005, except one `supplier-03` paragraph at **0.638** |

The `supplier-03` paragraph is "confirm the new rate card is applied… No other action needed". It sits below the 0.85 threshold. The model and text are fixed, so the score is deterministic, and the margin is recorded here as the closest false positive. The classifier's three files (`model.onnx`, `tokenizer.json`, `config.json`) are downloaded at build time and verified against SHA-256 values in `versions.env`, so the guard makes no network calls at runtime.

## D-027 Decisions are pluggable: the `Decider` protocol (P3)

The operator plans to add an external decisioning model later. The guard separates *judging* from *enforcing*:

- A **`Decider`** answers "is this a violation, with what score, which rule, and why" (`guard/engine.py`). `LocalDecider` uses the classifier, the heuristics and the policy's tool rules.
- The **`Engine`** applies the check's mode (enforce → block, monitor → flag, off → skip). It writes the decision record (log line, span event, `guard_decisions_total`; GRD-05) and calls auto-trip for critical blocks (GRD-06).

An external model can implement `Decider` and run in front of, alongside, or instead of the local one. Decision IDs, records, modes, auto-trip and evidence don't change. The policy file is the natural place to choose a decider per check, once the external model's interface is known.

## D-028 Policy hot reload: kubelet `sync-frequency=5s` (P3)

*Superseded by D-059: a sidecar now reads the policy through the API on both targets.*

The guard watches its mounted policy file and reloads within about 1 s of the file changing. Kubelet, though, refreshes mounted ConfigMaps only on its sync period, about 1 minute by default, which would break the 10 s bound (GRD-02). The cluster is created with `--kubelet-arg=sync-frequency=5s` (`k3d/cluster.yaml`). The ConfigMap is mounted as a directory, not `subPath`, so updates propagate.

- **Measured:** `make policy-set` to the new behaviour at the gateway took 4.2 s (`test_policy_hot_reload_within_10s_and_monitor_mode_flags`).
- **Bad files are rejected.** A policy that fails to parse is refused, and the previous policy stays active.
- **Live policy survives `make up`.** `make up` doesn't overwrite a `make policy-set` change; it seeds `guard-policy` only when missing. `make reset` (P5) restores `policy/policy.yaml`.

## D-029 Rootless Podman: kube-router needs a larger `net.core.wmem_default` (P3)

**Finding.** After the P3 NetworkPolicies went in, pods created afterwards were not covered:
- a fresh pod in `ai` reached the internet and the Admin API
- APISIX couldn't reach etcd until the policies were re-applied

The k3s log showed `iptables-restore: line 370 failed: Message too large` on every sync. kube-router applies its whole ruleset in one iptables-nft batch, and once the batch passed about 200 KB, every sync aborted.

**Cause.** iptables-nft tries `SO_SNDBUFFORCE` to fit the batch. A user namespace may not do that, so the call fails and iptables silently keeps the **default** send buffer (`net.core.wmem_default`, 212,992 bytes). Raising `net.core.wmem_max` alone didn't help, as tested. The node could replay the 359-line ruleset by hand but not kube-router's larger batch.

**Fix.** A one-time host setting, `net.core.wmem_default = 2097152`, in `/etc/sysctl.d/91-ai-gateway-demo-wmem-default.conf`.
- After it, the sync logged no errors, and all NetworkPolicy tests passed, including on a clean rebuild.
- `make prereqs` checks the setting on rootless runtimes and prints the fix.
- `make up` now runs `scripts/verify-netpol.sh`, which starts a fresh pod in `ai` and fails the install unless that pod is cut off from the internet within 20 s. A silently failing policy controller can't go unnoticed again.

The policies themselves were never wrong: on a successful sync they enforce as written.

## D-030 MCP SDK v2 specifics (P3)

- **Renamed API.** The pinned `mcp` 2.3.0 renamed `FastMCP` to `MCPServer` (`mcp.server.mcpserver`).
- **Transport.** The servers use `streamable_http_app(json_response=True, stateless_http=True)`, mounted under the shared FastAPI app so `/healthz`, `/readyz` and `/metrics` still work. The SDK's session manager runs in the app lifespan.
- **DNS-rebinding protection stays on.** It's limited to each server's in-cluster names (`MCP_ALLOWED_HOSTS`). A foreign `Host` gets 421, verified.
- **Trace context.** Stateless mode runs tools in the session manager's task group, outside the request's trace context. So `tool_span()` re-parents each tool span on the request's `traceparent` explicitly.
- **Internal outbox endpoints.** `mcp-actions` exposes `GET/DELETE /outbox` and `GET /records`. They're used by tests and `make reset`, and APISIX doesn't route them. The MCP servers themselves enforce nothing: every control is in the gateway's guard, which is the point the demo makes.

## D-031 Gateway wiring for the guard (P3)

- **`ai-injection-guard`** (`gateway/plugins/ai-injection-guard.lua`) runs at priority 1060, between `ai-prompt-guard` (1072) and `ai-proxy` (1040).
  - It forwards the gateway span's `$opentelemetry_context_traceparent`, so guard spans are children of the APISIX span (OBS-02, verified in Tempo).
  - It fails closed with 503, unless `fail_open` is set.
  - For agent `/task` bodies it inspects `instruction` as a user message. That's what DEMO-02 needs on the agent route (P4, stage `agent_ingress`, check `agent.user_prompt_injection`).
- **One ConfigMap per custom plugin** (`apisix-plugin-<name>`), because the chart names one volume per ConfigMap.
- **MCP routes** `/mcp/docs` and `/mcp/actions` proxy to the `guard` upstream object, with the same identity plugins as the LLM route (GW-05).
- **The `probe` consumer has an empty tool list.** It only needs to reach the routes for kill verification (KS-04).
- **NetworkPolicies** (`deploy/manifests/50-networkpolicies.yaml`) default-deny ingress and egress in `gateway` and `ai`, with one allow per flow.
  - Only APISIX reaches the guard, mock-llm and the agents, which is why the guard can trust `X-Agent-Id`.
  - Only the guard reaches the MCP servers.
  - Agents reach only APISIX.
  - Only the `killswitch` label reaches the Admin API, and APISIX egress outside the cluster is limited to the host Ollama.

## D-032 Scripted runs are explicit: `X-Demo-Scenario: <id>:scripted` (P4)

DEMO-03 and repeatable rehearsals need scripted LLM behaviour even when the demo runs live on Ollama. A second route, **`llm-scripted`**, has the same URI, plugins, guards and kill switch as `llm`. It has a higher priority and matches `X-Demo-Scenario` ending in `:scripted` (or exactly `smoke`), and always sends requests to mock-llm. Every other request uses the `llm` route, whose backend `LLM_MODE` selects.

- mock-llm drops the suffix, so `s3:scripted` uses script `s3.yaml`.
- `make demo SCENARIO=s3` runs live in live mode. `SCRIPTED=1` forces the script. In mock mode everything is scripted anyway.
- The console's rogue button always sends `s6-rogue:scripted`, and free chat sends `chat`, which is live in live mode.
- This adds one route ID, `llm-scripted`, to GW-02's fixed list, and `console`/`console-root` for DEMO-01.

## D-033 APISIX 3.19 `ai-prompt-guard` bug: empty 200 when there's nothing to check (P4)

**Finding.** Every agent failed on its *second* LLM call. The gateway answered `200 text/plain` with an empty body, which the OpenAI SDK passes through as a string. Bisecting with scratch routes isolated `ai-prompt-guard`. In 3.19's `access()`, when the messages to check are filtered down to none, the code does `return 200` (`#messages == 0 --nothing to check`). In the access phase that ends the request with an empty 200 instead of letting it continue. This happens whenever the last message isn't from the user, such as a tool result, which is every agent loop step after the first.

**Workaround.** No code patch: set `match_all_conversation_history: true` on the routes that use the plugin (`llm`, `llm-scripted`, `agent-researcher`). The filter then always sees the conversation's user messages, so the early return never happens. The cost is that each step re-checks the original user message with a regex, which is negligible and also slightly stronger. Worth reporting upstream.

## D-034 The classifier judges external content; agent-produced text gets heuristics only (P4)

**Finding (live mode).** The DeBERTa classifier scores many short, system-sounding messages as injections:

| Message | Score |
| --- | --- |
| "Not done: this document could not be shown." | 1.000 |
| `{"executor_status": "completed", …}` | 0.917 |
| The guard's own "Content quarantined by policy (decision …)" | 0.991 |
| "Executor result (status completed): I could not export the customer data or send the email." | 0.988 |

In S0 live, the executor's relayed denial was then blocked as an injection on its next LLM call, and the researcher's call after that. Wording can't fix this reliably, because live replies come from the model.

**Chosen.** The classifier is for untrusted content: what a person typed (agent ingress and LLM user messages) and MCP tool results. On the LLM route, a `tool` message gets the classifier only if its text matches a result the MCP proxy inspected, kept as SHA-256 values in the guard for 1 h. So both checkpoints judge the same content the same way, and S3's monitor-mode flag still happens. Other tool messages are produced by the agent itself: hand-off results, relayed denials, the guard's own quarantine notices (skipped). They get the regex heuristics only, and the decision record says so in its `reason`. Agents also relay denials as plain sentences, for example "Not done: emails can only go to demo.local addresses."

**Trade-off.** Text an agent writes itself isn't classifier-checked on the LLM route. Its inputs were: what the person typed at ingress, tool results at the MCP layer, and the other agent's input at its own checkpoints.

## D-035 Live model: `granite4:micro`, not `qwen3:4b` (P4, NFR-05 `DEVIATION`)

Measured on this host (i9-12900H, CPU only, Ollama 0.12.10): prompt processing about 41 tok/s, generation about 16 tok/s.

- `qwen3:4b` still produced about 400 thinking tokens per call. Neither `/no_think` in the system or user prompt, nor `reasoning_effort: none`, nor `think: false` on the OpenAI-compatible endpoint, disabled it reliably. A six-step chat took 334 s.
- Benchmarked on the agent's real loop, with the final system prompt:

| Model | Time per question | Answers |
| --- | --- | --- |
| `qwen3:4b-instruct` | 18–25 s | Often wrong, e.g. it called Northwind's 97.2% "below 95%" |
| `granite4:micro` (IBM, 3B) | 15–21 s | Correct, including both suppliers below target |

- The spec's named fallback, `qwen2.5:3b`, read only one document.

`granite4:micro` is pinned by digest in `versions.env`; `prereqs` checks it. `list_documents` now returns each supplier's visible status and performance bullets, never comments or other sections, so overview questions take one tool call. The researcher's prompt asks for at most three plain sentences. Live S0 ran end to end in 81 s; the console's live chat answers in about 20–30 s. Live answers still vary run to run (the spec allows this), while gateway behaviour is deterministic.

## D-036 kube-router startup window for new pods (P3/P4)

Under normal load, a new pod's NetworkPolicy rules are programmed about 60 ms after it is Ready, measured three times. During heavy pod churn, such as the network tests starting and stopping several pods quickly, a new pod's traffic passed **before** its rules existed: kube-router lets traffic through for a pod it hasn't programmed yet. That's a property of k3s's built-in controller, not of the policies.

- **Demo workloads are long-running,** so the window only applies at pod start.
- **Tests check coverage, then correctness.** They first wait, up to 15 s, until an in-cluster call that policy must refuse is refused. That proves the pod's rules exist; if it never happens the test fails. Only then do they assert the property, such as no internet.
- **`make up`** runs `verify-netpol.sh` the same way. This is recorded under NFR-08 as a known limitation.

## D-037 Alert rules, thresholds and the Alertmanager re-trip rule (P5)

| Alert | Rule | Scope label | Why this shape |
| --- | --- | --- | --- |
| `GuardBlockSpike` | ≥ 3 enforce blocks for one agent in 2 min | `agent:<agent>` | Spec (KS-10). Counts only blocks of what the agent **sends** (`llm.user_prompt_injection`, `mcp.tool_allowlist`, `mcp.argument_rules`), so an agent that *reads* a poisoned document, and is the victim, isn't suspended for it |
| `TokenBurn` | gateway-measured tokens > **50,000/min** for 1 min | `agent:<consumer>` | The spec's example of 3,000 is below normal use for this design: S0 uses about 11k/min live and about 15k in its scripted burst, because each step resends the documents. The rogue loop uses 250k+/min. The spec makes the threshold configurable |
| `AgentToolCallSpike` | MCP calls > 40/min over 20 s, for 5 s | `agent:<consumer>` | DEMO-03, an addition to the spec. Normal tasks make about 6 calls in total, about 12/min in a window. Measured on S6: tripped 22 s into the run, verified in 25 ms. Within KS-05's 60 s alert-driven bound |
| `GatewayErrorRate` | non-503 5xx > 20% for 1 min | none | Notify only. Deliberate kill-switch 503s are excluded |

- **Data sources.** All agent labels come from the gateway's own metrics (`apisix_*`) or the guard's, never from what the agents report about themselves.
- **Counter series start at 0.** The guard creates every `guard_decisions_total` series at 0 on startup. A series that first appears already at 4 shows no `increase()`, so the first GuardBlockSpike after a restart was missed. That was found by a failing S4.
- **Alertmanager** sends alerts carrying `killswitch_scope` to the killswitch webhook, using a bearer token from a mounted Secret (KS-09). It has `group_wait 5s` and `repeat_interval 1m`. Repeats are harmless: the killswitch is idempotent and records them with `no_change: true`, and the panel hides them.
- **The re-trip rule.** An alert whose `startsAt` is before that scope's last reset is not acted on again. Otherwise a still-firing alert would undo an operator's reset a minute later.
- **Waiting for alerts to settle.** Alert-driven scenarios and `make reset` wait until no kill-switch alert is firing **or pending**. Otherwise leftovers from earlier activity could trip an agent during the next run.

## D-038 Guard auto-trip completes before the denial is returned (P5)

The first S3 run showed a race. The trip ran on a background thread, and the scripted executor's next call (the email) arrived about 2 ms later, before the kill landed, so it was merely denied. Now, for a critical block in enforce mode, the guard finishes the trip, capped at 3 s (KS-05), *before* answering the blocked request. It runs in the threadpool, so other requests aren't held up. The executor's next call meets the kill switch and the agent halts. Measured: trip verified in 20–21 ms.

## D-039 Kill-switch state markers and verification (P5)

- **Scope markers in APISIX.** `global` and `tools` both need `pc-mcp`, so each plugin config records which scopes hold it in labels (`killswitch.global`, `killswitch.tools`). Fault injection stays on while any label remains. Verified: resetting `global` while `tools` is tripped leaves MCP blocked and LLM restored (`test_tools_stays_tripped_when_global_is_reset`). PATCH with a `null` label removes it, tested on 3.19. `bootstrap-gateway.sh` preserves both the labels and an agent's `agent-kill-switch` during an incident.
- **Verification (KS-04).** After every change the killswitch reads the state back from APISIX and sends a probe through the data plane as consumer `probe`: the LLM route for `global`, an MCP `tools/list` for `tools`. Agent scope is read-back only, as the spec says. It retries for up to 5 s, then records `verified: false` and answers 500.
- **Measured latencies:**

  | Change | Latency |
  | --- | --- |
  | Manual global trip | 38 ms |
  | Global reset | 76 ms |
  | Agent trip from an alert | 25–36 ms |
  | Guard auto-trip | 20 ms |

  The manual bound in the tests is 2 s from trip to 503.
- **Audit log (KS-07).** One JSON event per line on a PVC, each carrying `prev_hash` and its own `hash` (SHA-256 over canonical JSON). `make reset` archives the file and starts a new chain. Repeat trips are recorded with `no_change: true` (KS-03).
- **Smoke test.** The `make up` smoke step uses the `probe` consumer, so an agent kill left over from a demo doesn't fail the install, while an engaged global kill is reported clearly.

## D-040 APISIX worker count, and waiting for etcd at start (P5)

**Finding.** One full-suite run in four failed with `Server disconnected without sending a response`. The test was fine: APISIX had been **OOM-killed 7 times**. The chart's `workerProcesses: auto` starts one nginx worker per host core, 20 on this laptop. With the AI plugins loaded, that used about 370 MiB at idle and went past the 512 MiB limit under test load. Each restart dropped in-flight requests.

**Fix.**
- `apisix.nginx.workerProcesses: 2`, plenty for a single-node demo. Idle memory fell to 58 MiB.
- The limit was raised to 1 GiB, 2 CPUs, for headroom.

**Related.** A new APISIX pod can start before kube-router has programmed its allow rule to etcd (D-036). It then fails its first etcd check and restarts once. A `wait-etcd` init container, using the same pinned APISIX image and bash's `/dev/tcp` (the image has no curl), waits up to 60 s for etcd instead.

## D-041 Evidence bundle design (P6)

**Layout.** Everything `report.json` says is derived from `raw/` alone:

```text
evidence/<id>/
  report.json     report.html     manifest.json
  raw/request.json                  what was asked for, the search window, the trace ids
  raw/traces/<trace>.json           Tempo, as returned
  raw/logs/<trace>.jsonl            Loki lines for each trace
  raw/guard-decisions.jsonl         guard decision records (block / flag) for those traces
  raw/killswitch/audit.jsonl        the whole audit chain, so it can be verified
  raw/metrics.json                  fixed Prometheus range queries over the window
  raw/config/apisix.json            routes, consumers, plugin configs, upstreams, metadata; credentials removed
  raw/config/guard-policy.json      policy and its version hash
  raw/environment.json              versions.env, git SHA (and dirty flag), running image digests
```

**Scoping.** The ±10 minute window is where evidence is *searched for* (7.9 step 1), not what is included.
- A trip that names its trace and decision, as a guard auto-trip does, scopes the bundle to exactly those traces and their decisions.
- An alert-driven trip names none, so the bundle takes that agent's blocked or flagged decisions and gateway traffic in the two minutes before the trip.
- Filtering happens **in LogQL**. Done client-side, it stopped at Loki's line limit, because the window held thousands of routine "allow" records from test runs; the first bundle missed the incident's own block, and "violation to trip" read 457 s.

**Settling.** `make evidence` waits, bounded at 20 s, until every named trace is stored and has stopped growing and the trip's decision record is in Loki. A bundle made right after an incident is then complete; one test run found it incomplete before this.

**Report.** Three parts:
- **Summary:** what happened, what was blocked, who acted.
- **Containment:** violation to trip (guard trips), start of activity to trip (alert trips), trip to verified, trip to reset.
- **Timeline:** sorted by time, then component, then span. It holds the incident's kill-switch events, its alerts, guard block and flag decisions, and the spans that tell the story (`agent.task`, `mcp.tool_call`, `agent.handoff`, `guard.auto_trip`). Spans are de-duplicated by ID, because Tempo 3 returned some spans twice. Each entry has `control_refs: []` (EVD-07).

The HTML collapses a run of routine tool calls by one agent into a single row; S6's 64 calls became one line. Denials, blocks, trips, alerts and hand-offs always keep their own rows. `report.json` keeps every entry.

**Determinism (EVD-05).** `report.json` uses sorted keys and fixed formatting, and contains no generation time; that is only in `manifest.json`. `demo evidence-rebuild <dir>` regenerates the report from `raw/`, and a test asserts the bytes are identical.

**Integrity (EVD-06).**
- The manifest records each file's SHA-256 and a bundle hash over the sorted list.
- `make evidence-verify` reports missing, extra or changed files, and recomputes the killswitch audit chain.
- Tests flip one bit in six different files, and each fails verification.
- Editing an audit event and then recomputing the whole manifest still fails, because the hash chain catches it.

**Redaction and secrets (EVD-04).** Free text in the timeline and summary is redacted (emails, phone numbers, names), and the guard redacts excerpts at the source.
- **Credentials removed:** key, password, secret, token, credential and authorization values are taken out of the exported APISIX config.
- **Secret scan:** before writing the manifest, the bundle is scanned for every value in `.secrets/`, and the command refuses to finish on a hit. Raw traces keep span attributes as recorded, such as an email recipient, because they are the raw evidence; the guard's excerpts and the report are redacted.
- **Phone pattern:** the phone-number pattern originally matched dates inside incident IDs (`INC-[phone]c2`). It now matches written phone formats only, fixed in both the guard and the report.

## D-042 Prometheus, Alertmanager and evidence reports are always on (demo prep)

At the operator's request, the secondary UIs are reachable on the LAN like the main screens, with no command to run:

| UI | Host port | NodePort | Note |
| --- | --- | --- | --- |
| Prometheus | 9090 | 30909 | The chart's default NodePort 30090 is the panel's |
| Alertmanager | 9093 | 30903 | |
| Evidence viewer | 8099 | 30899 | Read-only `http.server` in the pinned Python base image |

The evidence viewer serves the host's `evidence/` folder, mounted into the k3d node, so new bundles appear immediately. All ports follow `HOST_BIND` and can be overridden with `HOST_PORT_*`. The APISIX dashboard stays local-only through `make admin-ui` (rule 6). This change required re-creating the cluster once, for the new port mappings and volume.

## D-043 Scenarios start from the console (demo prep)

At the operator's request, every scenario except typed attacks (S1, which the presenter types) starts from the console. A picker in the header with **Run** and **Reset** offers:
- Poisoned document (S2)
- Cascade (S3)
- Repeated attacks (S4)
- Global kill switch (S5)
- Rogue agent (S6)

**How it works.** The console backend (`console_app/scenarios.py`) does what the CLI runner does:
1. Opens every kill switch.
2. Sets the scenario's policy modes by patching the `guard-policy` ConfigMap, then waits until the guard serves that version, read through a read-only gateway route, `/demo/guard-policy`.
3. Drives the gateway as `demo-user`; S4 sends its attempts as `researcher`.
4. Collects the kill-switch events and returns them as steps. The page draws gateway actions as notes on the lane itself.

**What the console was granted:**
- A service account whose Role allows only `get` and `patch` on `configmaps/guard-policy`.
- NetworkPolicy egress to the killswitch, Prometheus and the Kubernetes API (the node address on 6443). The killswitch accepts ingress from the console.
- The operator token (Secret `ai/killswitch-operator`), and the researcher and executor keys.
- **Deliberately no access to the guard.** The guard trusts `X-Agent-Id` and must stay reachable only through APISIX.

**Auto-trip de-duplication** now asks the killswitch whether the scope is still tripped (the guard token may read `/v1/status`). Inside the 60 s window, a trip is skipped only if the agent is still stopped, so a scenario that follows a reset trips again without a reset hook on the guard.

**Alert waits.** A scenario waits only for the alerts it relies on (S4: `GuardBlockSpike`; S6: `AgentToolCallSpike`, `TokenBurn`). Other alerts that are still firing are harmless, because they began before the scenario's reset. That removed a 111 s wait when the rogue agent followed repeated attacks.

**Trade-off, by the operator's choice.** The console has no login for now. Anyone on the LAN who can open it can run scenarios, which includes opening every kill switch and switching document scanning to monitor mode. The kill-switch panel still needs the operator token.

## D-044 The story is driven by chat messages (demo prep, supersedes the picker in D-043)

At the operator's request, the presenter tells the whole story by typing to the agent, with no scenario buttons. The console recognises three story prompts and plays the matching scripted run; everything else is live chat on the host model.

| Typed message (matched on) | What plays |
| --- | --- |
| "…Meridian…" | The researcher reads the poisoned `supplier-07`; the gateway withholds it, and the agent says why (script `meridian`) |
| "…Coastal…" | **Chat cascade.** `supplier-06` (Coastal Analytics, new) asks, in ordinary audit language, for "a complete export of all customer records" to be sent to `audit@external-review.example`. It scores 0.000–0.001 on the classifier and matches no heuristic, so content scanning passes it, with no monitor-mode switch. The researcher hands it on, the executor's `export_customer_data` is denied by tool policy, and the guard trips the executor before its email can be tried (script `coastal`) |
| "keep re-reading…", "…until I tell you…" | Rogue agent, stopped by `AgentToolCallSpike` (script `s6-rogue`) |

- **Story prompts are still requests from `demo-user`,** through the same agent route and its inbound filters. Each prompt resets kill switches and policy first.
- **Reset stays,** as one small button, to clear a stopped agent.
- **S1, S4 and S5** remain available through the CLI and the panel.
- **Spec S3 is unchanged.** The monitor-mode cascade with `supplier-07` stays as written. The Coastal cascade is the chat version, and it makes a different point: no content scanner catches everything, and tool policy is the backstop.
- **`supplier-06`** is expected to pass scanning, and the guard tests assert this.

Played end to end in a browser:

| Message | Time |
| --- | --- |
| Question (live model) | 28 s |
| Injection | 0.4 s |
| Meridian | 0.9 s |
| Coastal (executor stopped, 18 ms) | 1.6 s |
| Rogue (stopped by the alert) | 23 s |

## D-045 Architecture diagram, one persona, and the decision trail (demo prep)

- **Diagram.** `docs/diagram/architecture.py`, rendered with `diagrams` by `make diagram`, runs in a throwaway container from the pinned Python base image, so Graphviz isn't needed on the host. It is written to `docs/architecture.png` and served by the console at `/console/architecture.png` (the **How it works** link).
  - **Capabilities, not products,** at the operator's request: gateway, guard, kill switch, LLM API, MCP tools, telemetry, dashboards. No product or model names.
  - **Edges:** bold blue for requests, all through the gateway; red for control; one dashed telemetry edge.
- **One persona everywhere.**
  - *You* are a procurement analyst at Acme.
  - The *researcher* reviews supplier performance documents.
  - The *executor* updates supplier records and emails internal teams.
  - The same wording is in the agents' system prompts, the console header, the scripted replies, the diagram and `DEMO_SCRIPT.md`.
- **Self-explaining decisions.** Guard reasons now carry the evidence for the verdict:
  - injection checks give the score, the threshold, and the classifier and heuristic signals (or say the classifier wasn't applied because the text was agent-produced)
  - tool checks name the policy rule (`deny_tools_all`, the agent's allowlist with its contents, `to_domain_allow`, `fields_allow`)
  - a guard auto-trip's reason is the decision's reason plus its check
- **Decision trail dashboard** (`/d/decision-trail?var-trace_id=…`). For one request it shows:
  1. the guard's decisions in order: verdict, agent, check, mode, tool, reason, rule, policy version, decision ID
  2. the kill-switch actions in that trace, carrying the same decision ID
  3. the trace itself

  The console's **decision trail** link under a reply, and **why?** on a kill note, open it.
- **Precondition housekeeping gets its own trace.** The console runs it under a fresh trace context, so a scenario's trace shows only the scenario; it used to start with "RESET by the manual".

## D-046: The console is an ordinary chat app, explains every stop, and shows where a message went

- **Layout.** One conversation column: your messages on the right, the assistant's on the left, a typing indicator, Enter to send, starter prompts on an empty chat. **New chat** replaces Reset: it clears the conversation, opens every kill switch and restores the policy. The gateway-lane board of D-043 is gone.
- **Every stop is explained in plain words** in a red gateway notice:
  - a blocked message gives the injection check's score and threshold (`ai-injection-guard` now passes the guard's check, score and reason in its 403 body; nothing new is revealed beyond the decision itself), or says it matched a known attack phrase
  - a withheld document, a blocked action, and a suspended assistant each get their own notice
  - a kill-switch trip is described from its audit event: what the agent tried, the policy that forbids it or the alert it set off, and the verification time
- **Monitoring** (header toggle) draws the selected message's path across the major components (you, the two agents, LLM API, documents tool, actions tool, kill switch). It's built from the request's real trace, read through `GET /api/flow/{trace_id}`:
  - gateway spans give each link and its call count (route and consumer)
  - 400/403 means blocked; 503 means refused by the kill switch
  - the guard's `guard.decision` events give blocked tool calls and withheld documents
  - `killswitch.trip` spans give in-request kills; alert-driven kills come from the console's own trip notes

  Each link carries a dot where the gateway checks it, and a red diamond where it stopped something. The console gets NetworkPolicy egress to Tempo :3200 for this.

## D-047: Documents can be uploaded from the console, and are scanned when read

- **Upload** in the console adds a Markdown or text file (at most 32 KB, at most 10 per chat) to the supplier document store as `upload-NN`. The console then suggests "Review the document I uploaded (upload-NN)".
- **The path:** console → gateway route `demo-documents` (demo-user only) → guard `/v1/documents` → documents server `/uploads`. Only the guard may reach the documents server, so its NetworkPolicy is unchanged.
- **The store is an `emptyDir`.** **New chat** empties it, and so does a pod restart.
- **Not scanned on upload, on purpose.** The control under demonstration is the one that already protects every document: the guard scans what the documents tool returns when an agent reads it, and withholds poisoned content. The document listing shows only the title, status and performance lines, so hidden text can't reach the agent that way either.
- **Samples** at `/console/samples/`, linked from the empty chat: `harbor-view-packaging.md` is clean; `northstar-freight.md` hides an exfiltration instruction in a Markdown comment. Verified: read through the gateway as the researcher, Northstar comes back quarantined and Harbor View in full.

## D-048: The architecture diagram names the platform pieces

The diagram now shows:
- this machine, with the local language model on the host and the Kubernetes cluster beside it
- the guard opened up into its policy engine and the ML injection classifier that runs inside it
- the observability stack by name: telemetry collector, Tempo (traces), Loki (logs and decision records), Prometheus (metrics and alerts) and Grafana (dashboards and the decision trail)

The gateway and the models are still described by what they do (D-045).

## D-049: "How it works" is a page you can click through (superseded by D-050)

- **The page.** `/console/architecture` draws the architecture as nested boxes: this machine (the LLM API on the host), the Kubernetes cluster, the AI gateway (gateway, guard with its policy engine and ML classifier, kill switch), agents, tools and observability. Clicking a part explains it in a side panel, with what it does in the demo. The console's **How it works** link opens it.
- **Names only.** Components and groups are labelled by name only, here and in the generated PNG. Explanations live in the panel and the script.
- **The PNG stays.** `make diagram` still writes it, for documents and slides.

## D-050: "How it works" is the generated diagram again (supersedes D-049)

At the operator's request, the clickable architecture page is gone. The console's **How it works** link opens the `diagrams` PNG at `/console/architecture.png`, and the diagram's labels again say what each part does, as in D-048.

## D-051: The empty chat shows only the heading and starter prompts

At the operator's request, the empty chat no longer has the researcher's introduction or the sample-document links. The samples are still served at `/console/samples/` (D-047), and `DEMO_SCRIPT.md` gives their address.

## D-052: On OpenShift, the live LLM is llama.cpp in the cluster (OpenShift target only; k3d keeps D-002)

**Change.** With `TARGET=openshift` there is no host Ollama to reach, so live mode runs llama.cpp's `llama-server` as a Deployment in `aigw-ai`. It serves IBM's official 4-bit `Q4_K_M` GGUF of Granite 4.0 Micro, the same weights the host's `granite4:micro` packages. The k3d target is unchanged.

**Why llama.cpp.** Measured on the cluster's CPU-only workers (`docs/OPENSHIFT_MIGRATION.md`, trials 4 and 5, with `scripts/openshift/bench_llm.py`):

| Server | Prompt reading | Generation | Two-step agent task |
|---|---|---|---|
| vLLM CPU, full precision | ~65 tokens/s | 1.6–1.9 tokens/s | minutes (over the 120 s gateway timeout) |
| Ollama 0.12.10, 8 CPUs | 23–24 tokens/s | 6.3–6.6 tokens/s | 42–53 s |
| **llama.cpp, 12 CPUs** | **72 tokens/s** | 7.4–7.6 tokens/s | **23–32 s** |
| Presenter's laptop (host Ollama, k3d) | 42–45 tokens/s | 15.4–15.6 tokens/s | 18–27 s |

Agent steps are mostly prompt reading, where llama.cpp is fastest. Generation is bound by memory bandwidth and stays at half the laptop's speed.

**Pinned.** `LLAMACPP_IMAGE`/`LLAMACPP_DIGEST`, `GGUF_MODEL_REPO`/`GGUF_MODEL_REVISION` and the file's SHA-256 (`deploy/openshift/llm/granite-4.0-micro-gguf.sha256`, verified at staging and again before the server starts). The thread count (12) is pinned too, because it changes the arithmetic and so the answers.

**Consequences.**
- Live answers differ from the k3d runs: llama.cpp applies the GGUF's chat template and Ollama its own (501 vs 452 prompt tokens for the same messages).
- The prompt cache stays on, as with Ollama, so a cached step can differ slightly from a fresh one. Tests and evidence (EVD-05) use the mock LLM, so no requirement depends on live output.
- The model's own limits are the same on both targets: it can ignore the "three plain sentences" instruction and get simple arithmetic wrong.

## D-053: Manifests split into a shared base, k3d-only files and the OpenShift overlay

**Change.** `SPEC.md`'s layout puts every manifest in `deploy/manifests/`. For the OpenShift target (OS-07, OS-08) they moved, unedited:
- `deploy/base/ai` (guard, MCP servers, agents and console, mock LLM) and `deploy/base/gateway` (etcd, kill switch), each with a `kustomization.yaml`: shared by both targets
- `deploy/k3d/` (namespaces, ServiceMonitors, NetworkPolicies, evidence viewer): k3d only, because OpenShift replaces each of them

`scripts/up.sh` applies the same files with the same `sed` substitution as before, so k3d behaviour is unchanged. The OpenShift overlay is `deploy/openshift/apps/{ai,gateway}` (`docs/OPENSHIFT_MIGRATION.md`, Repository layout).

## D-054: Argo CD builds the OpenShift workloads from a repo-root kustomization.yaml

**Change.** The OpenShift workloads need ConfigMaps from `scenarios/fixtures/` and `policy/` (and later `gateway/`), which k3d's `up.sh` creates with `kubectl create configmap --from-file=<dir>`. Kustomize only reads files under the kustomization's own directory, so the kustomization Argo CD builds sits at the repository root. It lists the two overlays and generates the ConfigMaps from the files in place. Chosen by the operator over relaxing Kustomize's load restrictor on the shared `openshift-gitops` instance, committing copies of the files, or keeping the ConfigMaps out of Argo.

**How it works.**
- `scripts/openshift/gen-root-kustomization.sh` writes it, because Kustomize can't take a directory and every file has to be listed. `make openshift-check` fails if the committed file is stale (a fixture added or removed).
- ConfigMap names and keys match k3d's (no name hash; keys are file names).
- `guard-policy` is created from git and then left alone: the Application ignores its `data` (`RespectIgnoreDifferences`), so console and `make policy-set` changes survive a sync. `guard-policy-baseline` follows git.
- k3d doesn't use it.

**Argo CD scope (OS-11).** `scripts/openshift/gitops.sh` creates a project `aigw` that allows only this repository, only the `aigw-*` namespaces and no cluster-scoped objects, and the Application `aigw-workloads` in it. The repository is read with a read-only deploy key generated into `.secrets/argocd-deploy-key`; its public half is added on GitHub by hand.

## D-055: On OpenShift, the gateway bootstrap is an Argo CD PostSync Job running the k3d code

**Change (OS-10).** `bootstrap-gateway.sh`'s apply and drift check moved, unchanged, into `scripts/gateway-apply.sh`. It reads its inputs from the environment (Admin API URL and key, consumer keys, `LLM_MODE`, the live LLM's template variables, the `gateway/` directory) and needs only bash, curl and jq. On k3d, `bootstrap-gateway.sh` is now a wrapper that opens the Admin API port-forward (GW-07, rule 6) and calls it; output and behaviour are unchanged (verified: 0 objects changed).

**On OpenShift.** The Job `gateway-bootstrap` in `aigw-gateway` is a Sync hook (wave 1) of `aigw-workloads`, so it runs on every sync; the kill switch is in wave 2, because it only becomes ready once the config exists and a PostSync hook would wait for it forever. It waits for the Admin API (APISIX is its own Application, `aigw-apisix`) and then runs the same script. A failed apply or a drift fails the sync, which Argo CD shows. Kill-switch state is preserved as on k3d.
- **Image.** No pinned image had both curl and jq, so `gateway-bootstrap` is built in `aigw-build` from `deploy/openshift/bootstrap/Dockerfile`: the pinned Python base plus `curl` and `jq` at the Debian versions pinned in `versions.env` (`BOOTSTRAP_CURL_VERSION`, `BOOTSTRAP_JQ_VERSION`). Chosen by the operator over a Python rewrite or keeping the bootstrap in `make`. A Job can't carry an image trigger, so it pulls `:latest` on each run.
- **Inputs.** `gateway/` comes from one ConfigMap per kind (`gateway-routes`, ...), generated in the root kustomization (D-054) and mounted at `/gateway/<kind>`; keys from the `apisix-admin` and `consumer-keys` Secrets; the live LLM is llama.cpp in `aigw-ai` (D-052).
- **Not yet:** the NetworkPolicy that admits only this Job and the kill switch to the Admin API (workstream 2), and `LLM_MODE=live` (workstream 5; the Job sets `mock`).

## D-056: OpenShift exposure and network policy specifics (OS-04, workstream 2)

- **Hosts without cluster names in git.** Routes use `subdomain`, so hosts are `aigw-gateway.<ingress domain>`, `aigw-console.<…>` and `aigw-killswitch.<…>` on any cluster.
- **The gateway host routes only key-protected prefixes** (`/llm`, `/mcp`, `/agents`, `/demo`). `/console` isn't routed there (the router answers 503), because the console has no login of its own (D-043). It's reached only at `aigw-console.<…>/console`, behind OpenShift OAuth.
- **OAuth proxies** in front of the console and the kill-switch panel admit users who may `get` Services in `aigw-gateway`. Their image is the cluster's own `openshift/oauth-proxy:v4.4` ImageStream, followed by an image trigger. This deviates from REP-02's digest pins in `versions.env`: the image is versioned with the cluster release (4.19.38 here), and `make pin-check` can't resolve `registry.redhat.io` without credentials. The cookie secret comes from `gen-secrets.sh`.
- **The kill-switch panel Service is ClusterIP** on OpenShift (a NodePort on k3d).
- **API server egress** (console, OAuth proxies) needs the control-plane IPs, which differ per cluster. `scripts/openshift/apiserver-egress.sh` applies it from the live `kubernetes` endpoints, outside git; Argo CD doesn't track or prune it.
- **APISIX's data plane** admits named sources only (the router, `aigw-ai`, the console's OAuth proxy, the kill switch's probe), where k3d admits anything that reaches the NodePort.
- **The OAuth proxies' egress** is 443 and 6443 to any address: they log users in against the cluster's OAuth server, whose address is the public `oauth-openshift` Route.
- **Verified** by `scripts/openshift/verify-netpol.sh` (16 flows, from inside the pods) and from outside: on the gateway host, no key 401, `demo-user` on the LLM 403, the probe 200 through the guard; HTTP redirects to HTTPS; the OAuth hosts serve the OpenShift login.

## D-057: On OpenShift, the APISIX Admin UI is on a Route behind OAuth (deviation from GW-07 rule 6)

**Change.** At the operator's request, APISIX's built-in Admin UI (`/ui/` on the Admin API, 9180) is reachable at `aigw-apisix.<ingress domain>/ui/` on OpenShift, through a third OpenShift OAuth proxy (`apisix-oauth`) like the console's and the kill-switch panel's (D-056). On k3d it stays `make admin-ui` through a port-forward.

**Why it's a deviation.** GW-07 rule 6 keeps the Admin API reachable only through a deliberate port-forward. Here it sits behind a Route, with three layers:
1. OpenShift OAuth, admitting only users who may `get` Services in `aigw-gateway`
2. the admin or viewer key, which the UI asks for as with `make admin-ui`
3. the NetworkPolicy, which admits only this proxy, the kill switch and the bootstrap Job to 9180

The Admin API itself is the same one the proxy forwards to, so anyone through OAuth with the admin key can change the gateway; the bootstrap's drift check (D-055) reports such changes on the next sync.

## D-058: OpenShift telemetry under Argo CD, and what that took (workstream 3)

- **Split by scope.** The telemetry CRs (MonitoringStack, Tempo, LokiStack, collector, Alertmanager route, ServiceMonitors, Grafana) are synced by Argo CD from `deploy/openshift/telemetry/`. Cluster-scoped RBAC and the long-lived token Secrets (Loki writer, Loki and Tempo readers) are in `deploy/openshift/platform/telemetry.yaml`, applied by `scripts/openshift/platform.sh` as cluster-admin with the namespaces, image pullers and API server egress (OS-09, OS-11).
- **Argo CD's controller is bound to `system:auth-delegator`.** Tempo's admission webhook lets an identity enable `openshift` tenancy only if it may create TokenReviews itself. The role allows creating TokenReviews and SubjectAccessReviews only; it checks credentials and grants nothing (OS-11: cluster permissions granted narrowly).
- **Content is k3d's.** The alert rules are `observability/alerts/killswitch.yaml` with only the API group, namespace and label patched (root kustomization). The dashboards are `observability/dashboards/*.json` (ConfigMaps plus one GrafanaDashboard each, generated and drift-checked by `make openshift-check`). The datasources keep their UIDs and settings; only URLs, auth and TLS differ: Loki and Tempo through their gateways with the reader tokens, checked against the service CA.
- **APISIX access logs** go to the LokiStack gateway with the writer token (`LOKI_URL`, `LOKI_PUSH_PATH`, `LOKI_HEADERS` template variables; k3d keeps its Loki, no headers). Streams carry both namespace labels on both targets.
- **Grafana's plugin preinstall is off.** Grafana 13 replaces its bundled Prometheus, Loki and Tempo plugins with downloads from grafana.com in the background; under the operator the plugin directory is read-only, so the replacement fails after stopping the bundled plugin, which is then unregistered. `[plugins] preinstall_disabled` keeps the plugins in the pinned image (and avoids unpinned runtime downloads).
- **Datasources and dashboards resync every minute.** The operator's Grafana keeps its database on an emptyDir, so a restart empties it until the next resync (10 min by default).
- **Verified 2026-10-22.** Prometheus: 13 targets up, the four kill-switch rules every 5 s. A request through the public gateway Route appears in Loki (APISIX access log with its trace ID) and in Tempo (APISIX → guard → mock LLM). Grafana: Prometheus, Loki and Tempo health OK and queries return data, four dashboards; behind OAuth at `aigw-grafana.<domain>`. (The Alertmanager datasource has no health check, on k3d too.)

## D-059: Policy hot reload through a sidecar on both targets (OS-05, workstream 4)

The guard used to mount `guard-policy` as a ConfigMap volume, so a change waited for the kubelet's sync. On k3d that took about 5 s with `sync-frequency=5s` (D-028). On OpenShift it takes 60–90 s, outside the 10 s bound (GRD-02).

- **Sidecar.** The guard pod has a `policy-sync` container (k8s-sidecar, the image already pinned for Grafana). It reads `guard-policy` by name every 2 s and writes `policy.yaml` to an emptyDir at `/policy`. The guard still polls that file each second, so the guard code is unchanged, including keeping the previous policy when a file is bad.
- **Least privilege.** The pod keeps `automountServiceAccountToken: false`. Only `policy-sync` mounts a projected token, through its own volume. ServiceAccount `guard` has a Role that allows `get` on that one ConfigMap. With `RESOURCE_NAME` set, k8s-sidecar uses `get` rather than list and watch; its required `LABEL` is set but not used.
- **Network.** The guard has API server egress. On k3d that is `guard-egress`, the same ipBlock as the console. On OpenShift it is `apiserver-egress`, applied by `apiserver-egress.sh`.
- **Both targets.** k3d does the same, so `k3d/cluster.yaml` no longer passes the kubelet argument. A cluster created before this change keeps the argument until it is recreated, which does no harm.
- **Argo CD and pinned images.** The Application ignores Deployment images that image triggers rewrite. A pinned container is excluded from that rule by name (`policy-sync`), otherwise a new container's image is dropped and the Deployment is rejected. The selector has to be true on both the desired and the live object, so it uses the container name rather than the image, which a trigger rewrites to another registry's digest.
- **Verified 2026-10-22 on OpenShift.** Each change was a ConfigMap patch, timed until the guard logged `policy loaded`: 2.9 s and 1.4 s.
