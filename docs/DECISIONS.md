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
