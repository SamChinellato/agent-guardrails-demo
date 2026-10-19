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
