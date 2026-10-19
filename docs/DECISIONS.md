# Decisions and verified-API notes

Every deviation from `SPEC.md`, every version choice and every API discrepancy found while verifying (spec rule 2) is recorded here. Entries are append-only; superseded entries are marked, not deleted.

## D-001 Repository root is `api-days-demo/` (P0)

The spec's layout calls the root `ai-gateway-demo/`. The repo lives in the existing `api-days-demo/` directory. All paths inside the layout are unchanged.

## D-002 Ollama runs on the host, not in the cluster (P0)

**Change.** The operator's request. The spec (sections 3 and 7.5) puts Ollama in the `ai` namespace as a Deployment with a PVC and a pull Job. Instead, live mode uses the Ollama already installed on the host machine.

**How it works.**

- APISIX `ai-proxy` sends requests to `OLLAMA_HOST_URL=http://host.k3d.internal:11434` in live mode. k3d adds `host.k3d.internal` to the cluster's CoreDNS, and it resolves to the gateway of the k3d Docker network.
- On Linux, Ollama must listen on all interfaces (`OLLAMA_HOST=0.0.0.0:11434`), because a loopback-only listener can't be reached from containers. `scripts/prereqs.sh live` checks this. On Docker Desktop (macOS), `host.k3d.internal` reaches the host's loopback, so the default bind works.
- The model is still pinned by tag and digest (REP-02). The digest is the one the host's Ollama reports in `/api/tags` after `ollama pull`. `make prefetch LLM_MODE=live` pulls it on the host, and `prereqs` fails if the digest differs.
- The Ollama server version isn't pinned, because it's host software. `OLLAMA_MIN_VERSION` sets a floor instead.

**Consequences.**

- No Ollama image, Deployment, PVC or pull Job. The ollama row in the component table and section 7.5's Ollama bullet don't apply.
- NFR-08 still holds: no data leaves the machine. The `ai` namespace keeps egress limited to cluster services. Only the APISIX pods in `gateway` get an egress exception, to the host gateway IP on TCP 11434. Agents still reach the LLM only through APISIX.
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
