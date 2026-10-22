# OpenShift migration plan

**Status: all planning questions answered (OS-01 to OS-16) and all spikes done; implementation next, starting with workstream 1.** Branch `openshift-migration`. Questions are marked **Q-n**; answers are recorded under [Decisions](#decisions) and folded into the plan as they come in.

## Goal

Run the demo on the TechZone OpenShift cluster instead of a local k3d cluster, keeping what the demo shows (attacks, observability, kill switch, evidence) and the operator interface (`make up`, `make demo`, `make test`, ...).

Constraint: the telemetry stack (metrics, alerts, traces, logs, dashboards) must be **self-contained**. We have cluster-admin, but **user workload monitoring cannot be enabled** (the platform `cluster-monitoring-config` is managed from an ACM hub).

## Target cluster (surveyed 2026-10-22)

| Item | Value |
|---|---|
| Version | OpenShift 4.19.38, Kubernetes 1.32.13 |
| Network | OVN-Kubernetes; pods 10.128.0.0/14, services 172.30.0.0/16 |
| Nodes | 3 control plane, 5 workers at 15.5 CPU / 64 GiB each, x86_64, **no GPUs** |
| Storage | ODF: `ocs-storagecluster-ceph-rbd` (default, RWO), `ocs-storagecluster-cephfs` (RWX), NooBaa object storage |
| Registry | Internal image registry, managed, `default-route` exposed |
| Ingress domain | `apps.itz-0x5jw5.infra01-lb.syd05.techzone.ibm.com` |
| Monitoring | Platform monitoring only; remote alerting to an ACM hub; no UWM |
| Operators installed | cert-manager, External Secrets, OpenShift Pipelines, ODF |
| In catalog | Cluster Observability Operator, Tempo, Loki, OpenTelemetry (Red Hat), Grafana Operator (community), NFD, GPU operators |
| Shared? | Other tenants' namespaces exist (`confused-deputy`, `tools`, `uptycs`, ...) |

## What changes, component by component

| Area | Today (k3d) | On OpenShift | Effort |
|---|---|---|---|
| Cluster lifecycle | `up.sh` creates a k3d cluster and registry | Cluster exists. `make up TARGET=openshift` installs Argo CD and the root Application, then triggers builds and waits for sync (**OS-08**). `down` deletes the Applications and demo namespaces, not a cluster | M |
| Kubeconfig | `.kube/config` written by k3d | Same file, written by `oc login` (or a service-account token) | S |
| Images | Built locally, pushed to the k3d registry | In-cluster binary `BuildConfig`s (Docker strategy) in a build namespace, into ImageStreams; workloads pull by ImageStreamTag digest (**OS-02**) | M |
| Pod security | Fixed `runAsUser` 1000/10001, `fsGroup` | `restricted-v2` everywhere: fixed UIDs and `fsGroup` removed in the OpenShift overlay, our image made to run as any UID in group 0. Exceptions get `nonroot-v2` on their own service account, recorded (**OS-06**) | M |
| Exposure | NodePorts mapped to host ports 8080/3000/8090/9090/9093/8099 | Edge-TLS Routes; demo UIs behind OpenShift OAuth proxy, the rest by port-forward (**OS-04**) | M |
| Console → Grafana link | Built from `GRAFANA_PORT` | `GRAFANA_SUBDOMAIN=aigw-grafana`: the console's own domain with Grafana's Route subdomain, so no cluster domain in git. **Done** | S |
| CLI and tests | Reach `127.0.0.1:<port>` | Read base URLs from settings (Route hosts), falling back to localhost so k3d still works | M |
| Evidence viewer | `hostPath` from the laptop's `evidence/` | No in-cluster viewer. `make ui UI=evidence` serves `evidence/` read-only from the presenter's machine; bundles never leave it (**OS-13**) | S |
| NetworkPolicies | k3s kube-router; DNS in `kube-system` | OVN-K. DNS selectors move to `openshift-dns` (port **5353**); allow ingress from the router (`policy-group.network.openshift.io/ingress`); API server egress to the control-plane IPs on 6443 | M |
| APISIX Admin allow-list | `10.42.0.0/16` | `10.128.0.0/14` | S |
| Policy hot reload (GRD-02, 10 s) | kubelet `sync-frequency=5s` | `k8s-sidecar` in the guard pod reads the ConfigMap through the API every 2 s and writes it to a shared `emptyDir`; guard code unchanged (**OS-05**, D-059, [Policy hot reload](#policy-hot-reload-os-05)) | S |
| Live LLM | Ollama on the host | llama.cpp in-cluster serving the 4-bit Granite 4.0 Micro GGUF on CPU (**OS-16**, superseding vLLM in OS-03) | L |
| Telemetry | kube-prometheus-stack (own operator + CRDs), Tempo, Loki, OTel Collector, Grafana via Helm | Red Hat operators: COO, Tempo, Loki, OpenTelemetry; Grafana Operator (**OS-01**, [Telemetry](#telemetry)) | L |
| Version pins | `versions.env` | Add new images/charts/operator channels; k3d-only pins stay for the local path | S |

## Telemetry

### The problem with today's chart

`kube-prometheus-stack` installs the `monitoring.coreos.com` CRDs and its own prometheus-operator (v0.94.1). On OpenShift those CRDs are owned by the cluster version operator at the platform's older operator version. Installing ours would overwrite CVO-managed CRDs (the CVO reverts them, and both fight). Leaving our CRDs out and running a v0.94 operator against older CRD schemas is unsupported and can silently prune fields.

### Chosen design: Red Hat operators (OS-01)

Three options were considered: (A) COO for metrics with the Helm charts for the rest, (B) kube-prometheus-stack scoped to our namespaces, and (C) Red Hat operators throughout. **C was chosen.** Grafana comes from the community Grafana Operator, since Red Hat doesn't ship one.

| Signal | Today | OpenShift | Notes |
|---|---|---|---|
| Metrics + alerts | kps Prometheus + Alertmanager | **Cluster Observability Operator** `MonitoringStack` `aigw` in `aigw-observability` (services `aigw-prometheus`, `aigw-alertmanager`); **verified** | Own operator and own API group `monitoring.rhobs`, so it never touches the platform's `monitoring.coreos.com` CRDs or needs UWM. Our `ServiceMonitor`s and `PrometheusRule` move to `monitoring.rhobs/v1`. The APISIX and collector charts' own ServiceMonitors (`monitoring.coreos.com`) are switched off and replaced by ours. |
| Alert routing (F6) | kps `alertmanager.config` | An `AlertmanagerConfig` (`monitoring.rhobs/v1alpha1`) in `aigw-observability`: route on `killswitch_scope`, webhook with the bearer token from Secret `killswitch-alertmanager` | **Verified.** A test alert fired at 5 s evaluation and was delivered to `/v1/alertmanager` with the bearer token. `matcherStrategy: OnNamespaceExceptForAlertmanagerNamespace`, because our alerts carry no namespace label. |
| Traces | Tempo Helm chart | **Tempo Operator** `TempoMonolithic` (Tempo 2.10), multi-tenant in `openshift` mode with one tenant `demo`, PVC on RBD (**OS-15**) | **Verified.** Single-tenant is unsupported on OpenShift (the operator warns). The collector writes through the gateway with its token; readers (SA `tempo-reader`) query `https://tempo-tempo-gateway:8080/api/traces/v1/demo/tempo/api/...`. Fetch by ID and TraceQL search both work. The console's trace reader (D-046), Grafana and the evidence CLI need a bearer token and the new URL. |
| Logs | Loki single binary on RBD | **Loki Operator** `LokiStack` size `1x.demo`, `openshift-logging` tenancy, object storage on a NooBaa `ObjectBucketClaim` | **Verified (trial 1).** The Red Hat build always deploys the token-checking gateway. The collector (SA `otel-collector`) writes OTLP to the `application` tenant. Grafana and the evidence CLI read as SA `loki-reader`: a cluster-scope `get` on the tenant plus `get` on only the demo namespaces, so queries are limited to them (another namespace is refused). `service_name` stays a stream label and record attributes are structured metadata, so today's queries work unchanged. Every record must also carry `kubernetes.namespace_name`, which the gateway filters on; the collector adds it. Retention 1 d. |
| Collector | OTel Collector Helm chart | **Red Hat build of OpenTelemetry** `OpenTelemetryCollector` (deployment mode), service `otel-collector` | **Verified.** Same pipelines, plus `k8s_attributes` (namespace, pod, deployment) and a transform adding `kubernetes.namespace_name`. Exports to both gateways with `bearertokenauth` and the service CA. Prometheus exporter on :8889 scraped by our `monitoring.rhobs` ServiceMonitor. |
| APISIX access logs | `loki-logger` plugin → Loki :3100 push API | `loki-logger` → the LokiStack gateway push API with a bearer token for SA `apisix-log-writer` (write-only) | **Verified.** The Red Hat collector has no Loki receiver. `loki-logger` supports `headers`, which APISIX encrypts at rest. The token is a long-lived service-account token Secret (a plugin config can't rotate one), rendered by the gateway bootstrap. Labels add both namespace labels; `| json | trace_id=` queries work. |
| Dashboards | Grafana in kps, sidecar ConfigMaps | **Grafana Operator** `Grafana` + `GrafanaDatasource` + `GrafanaDashboard` (from the same JSON in ConfigMaps) | Datasource UIDs stay fixed (`prometheus`, `loki`, `tempo`, `alertmanager`) so dashboards are unchanged. Anonymous viewer behind OAuth proxy (OS-04). |

Content is unchanged: dashboards, datasource UIDs, alert expressions, the 5 s scrape and evaluation intervals, and the 24 h retention.

Operators are installed from `redhat-operators` (and `community-operators` for Grafana) by `Subscription`s pinned to a channel with `installPlanApproval: Manual`, and the approved CSV versions are recorded in `versions.env` (REP-02).

## Live LLM on llama.cpp (OS-03, OS-16)

The gateway's live provider is already OpenAI-compatible (`gateway/upstreams/llm.live.json`), so only the endpoint and model name change: `${OLLAMA_URL}` and `${OLLAMA_MODEL}` become the llama.cpp Service and its alias on OpenShift. The original choice was vLLM (OS-03); on these CPU-only workers it was too slow at full precision ([trial 4](#trial-4-results-2026-10-22)), and llama.cpp with the 4-bit model was the fastest of the options measured ([trial 5](#trial-5-results-2026-10-22-4-bit-granite-on-cpu)).

- **Server.** llama.cpp's `llama-server` (`LLAMACPP_IMAGE` by digest) as a Deployment `llamacpp` in `aigw-ai`, OpenAI API on :8000, `--jinja` so the GGUF's own chat template handles tool calls, `--alias granite-4.0-micro`, 16k context. `enableServiceLinks: false` and `readOnlyRootFilesystem`, under `restricted-v2`.
- **Model.** IBM's official `Q4_K_M` GGUF of Granite 4.0 Micro (`ibm-granite/granite-4.0-micro-GGUF` at `GGUF_MODEL_REVISION`), about 2 GB. The `gguf-fetch` Job stages it on the CephFS (RWX) `models` volume and checks it against `deploy/openshift/llm/granite-4.0-micro-gguf.sha256`; the server's init container re-verifies it with no network, so the server makes none at runtime (as GRD-01 does for the classifier).
- **Sizing.** 12 CPUs with `--threads 12` (threads equal to the CPU limit; more throttles), 6–8 GiB. The most that fits beside other tenants' pods: no worker had 14 CPUs free on 2026-10-22.
- **Speed (measured).** About 72 tokens/s reading prompts and 7.5 tokens/s generating; a two-step agent task in 23–32 s against 18–27 s on the presenter's laptop. Long answers stream at half the laptop's speed; generation is bound by memory bandwidth, so more threads won't fix that.
- **Not byte-identical to k3d.** Same 4-bit weights as the host's `granite4:micro`, but llama.cpp applies the GGUF's template and Ollama its own (501 vs 452 prompt tokens for the same messages), and the thread count changes the arithmetic. `temperature: 0` and `seed` still apply, and the thread count is pinned with the model.
- **Prompt cache stays on**, as on the k3d host's Ollama. A step served from the cache can differ slightly from the same step computed fresh. This affects no requirement: `make test` and evidence (EVD-05) use the mock LLM.
- **Under Argo CD** (D-060): `deploy/openshift/apps/ai/llamacpp.yaml` holds the `models` PVC (`Prune=false,Delete=false`), the Deployment and Service, and `gguf-fetch` as a Sync hook. Each sync it re-checks the staged file (about 20 s) and downloads only what is missing or wrong. ConfigMap `model-fetch` comes from the root kustomization.
- **Mode switch** (D-060): the ConfigMap `llm-mode` in `aigw-gateway` (`mode: live` on a fresh install) is read by the gateway-bootstrap Job. Argo CD creates it once and then ignores its data. `make llm-mode MODE=mock|live TARGET=openshift` sets it and runs a copy of the bootstrap Job, which takes about 10 s. llama.cpp stays running in both modes, so switching back is instant.
- **NetworkPolicy.** Only APISIX reaches `llamacpp` (it joins the `from-gateway` selector). Policy `gguf-fetch` gives that Job egress to public addresses on 443, the one exception to NFR-08.
- **Version pins.** `LLAMACPP_IMAGE`/`LLAMACPP_DIGEST`, `GGUF_MODEL_REPO`/`GGUF_MODEL_REVISION` in `versions.env`, written out in the overlay and checked by `make openshift-check`, with the file SHA-256 in the manifest file.
- **Trial leftovers.** `vllm.yaml` and `ollama.yaml` stay in `deploy/openshift/llm/` (with `fetch_model.py` and the hash manifests, which the overlay uses) as the record of trials 4 and 5, scaled to zero, and are not part of the overlay.

### Trial 4 results (2026-10-22)

Deployed with vLLM v0.31.0 CPU (`deploy/openshift/llm/`), 12 CPUs and 24–32 GiB, model staged and verified on a CephFS volume.

| | Result |
|---|---|
| Startup | Weights 23 s; compile and warm-up 3.5–4.7 min (cache in `/tmp`, lost on restart) |
| Tool calling (`hermes` parser) | **Works**: a correct `search_documents` call |
| Same seed, temperature 0 | **Identical** output on repeat |
| Generation speed | **1.6–1.9 tokens/s** in bf16 and in fp32 |
| Prompt reading | ~65 tokens/s (a 2,600-token prompt: 40 s before the first token) |

**Too slow for a live demo.** CPU generation is limited by memory bandwidth: each token reads all weights (about 7 GB in bf16, 13 GB in fp32). One agent step with documents would take minutes and break the gateway's 120 s timeout. Ollama's 4-bit GGUF reads about 2 GB per token. vLLM is scaled to zero; see **OS-16**.

### Trial 5 results (2026-10-22): 4-bit Granite on CPU

Two servers for the same 4-bit Granite 4.0 Micro, each with 8 CPUs on its own worker, models staged and verified on the CephFS volume: **llama.cpp** `server-b11312` with IBM's `Q4_K_M` GGUF (`--jinja`, 16k context, 8 threads) and **Ollama** 0.12.10 with `granite4:micro` (the k3d host's digest). Measured with `scripts/openshift/bench_llm.py` through port-forwards: a cold ~3,200-token prompt (cache-busting nonce, streamed), then the researcher agent's loop as the agents call it (its system prompt, `docs__` tools answered from the fixtures, temperature 0, seed 42), run twice.

| | llama.cpp | Ollama as deployed | Ollama, 8 threads, 16k context |
|---|---|---|---|
| Prompt reading | **57 tokens/s** | 20 tokens/s | 23–24 tokens/s |
| Time to first token, 3,200-token prompt | **55–56 s** | 156–157 s (over 120 s) | 133–138 s (over 120 s) |
| Generation | 5.7–5.8 tokens/s | 3.1–3.2 tokens/s | **6.3–6.6 tokens/s** |
| Agent task (2 steps: list, answer) | **27–34 s**, slowest step 25 s | 63–71 s, slowest step 59 s | 42–53 s, slowest step 40 s |
| Tool call | `docs__list_documents` | same | same |
| Answer | Both below-target suppliers; one gap wrong (7.5 for 6.5) | Missed Summit (94.0 %); run 2 calls it above target | same as as-deployed |

For comparison, vLLM at full precision (trial 4) generated 1.6–1.9 tokens/s.

**Scaled up, against the k3d host.** llama.cpp at 12 CPUs and 12 threads (the most that fits beside other tenants' pods; no worker had 14 CPUs free), on worker-5, compared with the presenter's laptop (i9-12900H, CPU only, host Ollama with the same `granite4:micro`):

| | llama.cpp, 8 threads | llama.cpp, 12 threads | Laptop (k3d host Ollama) |
|---|---|---|---|
| Prompt reading | 57 tokens/s | **72 tokens/s** | 42–45 tokens/s |
| Time to first token, 3,200-token prompt | 55–56 s | **44 s** | 71–76 s |
| Generation | 5.7–5.8 tokens/s | 7.4–7.6 tokens/s | **15.4–15.6 tokens/s** |
| Agent task (2 steps) | 27–34 s | 23–32 s | **18–27 s** |

At 12 threads agent tasks feel close to the laptop and long prompts are faster; long answers stream at half the laptop's speed. Generation reads all ~2 GB of weights per token, so it is bound by memory bandwidth (about 15 GB/s here against about 30 GB/s on the laptop) and gains little from more threads. The laptop shows the same model errors (a wrong gap, the 94 % supplier missed, a different answer on repeat), so they aren't caused by the cluster. Changing the thread count also changes the answers (summation order), so the thread count belongs in the pinned configuration.

- **llama.cpp is the faster server overall.** It reads prompts 2.4× faster than Ollama, which is what dominates agent steps (documents and tool results are input, answers are short). Ollama generates slightly faster at 8 threads.
- **Ollama as deployed oversubscribes its CPUs.** Its runner starts one thread per node vCPU (16) under an 8-CPU limit; the CFS throttling halves generation. Ollama has no environment variable for the thread count (only per request or in a Modelfile), and its default 4,096-token context would truncate longer agent prompts (`OLLAMA_CONTEXT_LENGTH=16384` added to `ollama.yaml`).
- **Determinism holds per prompt-cache state, not across it.** Both servers repeat exactly when the cache state is the same (Ollama's two endpoints gave identical run-1 and run-2 transcripts), but a step served from the prompt cache differs from the same step computed fresh: one token for llama.cpp, a different answer for Ollama. Evidence and `make test` comparisons need the cache disabled (`cache_prompt: false` on llama.cpp) or a restart between runs.
- **The two servers apply different chat templates.** The same messages are 501 prompt tokens on llama.cpp (the GGUF's own Jinja template) and 452 on Ollama (its packaged template), so answers differ between them as well as from k3d's host Ollama.
- **Model quality limits.** Both ignore the "three plain sentences, no headings" instruction and can get simple arithmetic wrong.
- **Timeout headroom.** A cold 3,200-token step takes about 56 s on llama.cpp, under the 120 s gateway timeout; on Ollama it exceeds it. Port-forward drops pooled idle connections, so the benchmark opens one connection per request.

Pitfalls fixed on the way: the Service `vllm` injects `VLLM_PORT=tcp://…`, which vLLM rejects (`enableServiceLinks: false`); a single-node (RWO) volume can't be shared by the fetch Job and the server (moved to CephFS RWX, with a verify-only init container).

## Images: in-cluster builds (OS-02)

- A build namespace (no default-deny NetworkPolicy, so the guard build can fetch its classifier) holds one binary `BuildConfig` per service, Docker strategy, using `services/Dockerfile` with the same `--build-arg`s as `build-images.sh` (pinned base images, classifier SHA-256s).
- `build-images.sh` on OpenShift runs `oc start-build <svc> --from-dir=services --wait` and tags the ImageStream `<svc>:<git-tag>` as today. It skips any service whose `ImageStreamTag` already exists (REP-07).
- Workloads in `ai` and `gateway` pull from `image-registry.openshift-image-registry.svc:5000/<build-ns>/<svc>` with a `system:image-puller` RoleBinding. With GitOps (OS-08) the digest can't be written into the manifests at apply time any more; see [GitOps](#gitops-os-08).
- No local container runtime is needed on OpenShift.

## Exposure (OS-04)

| Endpoint | Route | Auth |
|---|---|---|
| Console (`/console` on APISIX) | `console-…` edge TLS → oauth-proxy → APISIX | OpenShift OAuth; plus the console's own controls as today |
| Gateway API (CLI, tests) | `gateway-…` edge TLS → APISIX directly | Consumer keys (key-auth) as today. Not behind OAuth, so the CLI and tests keep working. |
| Grafana | `aigw-grafana.<domain>` edge TLS → oauth-proxy → Grafana | OpenShift OAuth, then anonymous Viewer (**done**, D-058) |
| Kill-switch panel | `killswitch-…` edge TLS → oauth-proxy → panel | OpenShift OAuth, then the operator token as today |
| Prometheus, Alertmanager, evidence, APISIX Admin | none | `make ui UI=…` / `make admin-ui` port-forward |

Hosts (from `subdomain`, so the same on any cluster): `aigw-gateway.<domain>` (only `/llm`, `/mcp`, `/agents`, `/demo`), `aigw-console.<domain>/console`, `aigw-killswitch.<domain>`. Verified 2026-10-22 (D-056).

The oauth-proxy sidecars limit access with `--openshift-sar` to users who can `get` Services in the demo namespaces, so cluster-admin and invited presenters get in and other tenants don't. The NetworkPolicies allow ingress from the router namespace only to the oauth-proxy ports (and APISIX :9080 for the gateway Route).

## Policy hot reload (OS-05)

The guard decides on content; APISIX only enforces its verdict (see `docs/DETECTION.md`). The guard's policy lives in the `guard-policy` ConfigMap, and `PolicyStore` (`services/guard/src/guard/policy.py`) re-reads `/policy/policy.yaml` every second. On OpenShift the kubelet takes 60–90 s to update a mounted ConfigMap, which misses the 10 s bound.

```
guard pod
├── policy-sync   (k8s-sidecar) gets ConfigMap guard-policy every 2 s ──► writes /policy/policy.yaml (emptyDir)
└── guard         PolicyStore polls /policy/policy.yaml every 1 s (unchanged)
```

- **Guard code unchanged.** Parsing, validation and "keep the previous policy on a bad file" stay where they are; the guard doesn't know about Kubernetes.
- **Least privilege.** `automountServiceAccountToken: false` stays on the pod. Only the sidecar container mounts a projected service-account token. Its Role allows `get` on the ConfigMap `guard-policy` only: with `RESOURCE_NAME` and `NAMESPACE` set, k8s-sidecar reads that one object by name every `SLEEP_TIME` seconds instead of listing and watching by label, so no label and no `list`/`watch` are needed.
- **NetworkPolicy.** The guard pod gains egress to the API server on 6443 (the control-plane IPs), like the console.
- **Both targets.** The same on k3d, so the kubelet `sync-frequency=5s` argument can leave `k3d/cluster.yaml`.
- **Image.** `K8S_SIDECAR_IMAGE` in `versions.env`, already pinned for Grafana.
- **Readiness.** The guard is ready only once a policy is parsed (GRD-08), so it waits for the sidecar's first write.

Patterns considered: the mounted file alone (kubelet latency), restart on change (Reloader or hashed ConfigMaps; the guard fails closed while it reloads the model), the guard watching the API itself (couples the guard to Kubernetes and gives it a token), and a `GuardPolicy` CRD. **Possible later phase:** the CRD, which adds validation when a policy is written (CEL), a `status.appliedVersion` the console and tests can wait on, and per-object RBAC and audit.

## GitOps (OS-08)

OpenShift GitOps (Argo CD) syncs the OpenShift overlay from this repository; `make` keeps the operator verbs (`demo`, `test`, `evidence`, `trip`, `reset`, `status`). What moves where:

| Today (`up.sh`) | Under Argo CD |
|---|---|
| `sed` substitution into manifests (`${GUARD_IMAGE}`, `${LLM_MODEL}`, ...) | Kustomize: the shared base in `deploy/base/{ai,gateway}`, and the OpenShift overlay in `deploy/openshift/apps/{ai,gateway}` with every value in git. k3d keeps applying the base files through `up.sh` and `sed` (no k3d overlay); its own files are in `deploy/k3d/`. See [Repository layout](#repository-layout) |
| Helm charts with `--set` digests | Replaced by operator CRs on OpenShift (OS-01); APISIX chart rendered by Argo's Helm support with values in git |
| Image digests from `build-images.sh` | ImageStreams with `lookupPolicy: local` in each consuming namespace and an `image.openshift.io/triggers` annotation, so a new build rolls the Deployment and the pod spec carries the digest. Argo ignores the trigger-managed `image` field |
| `gen-secrets.sh` → Secrets | Unchanged: `make up` runs `gen-secrets.sh` against the cluster before the first sync. Argo never manages Secrets, and they never reach git (**OS-09**) |
| `bootstrap-gateway.sh` (Admin API via port-forward, drift check, kill-state preservation) | An Argo `PostSync` hook Job in `aigw-gateway` running the same logic in-cluster (**OS-10**). `gateway/` is shipped to it as a ConfigMap generated by Kustomize; the admin key and consumer keys come from the existing Secrets. A NetworkPolicy lets only pods labelled `app: gateway-bootstrap` reach the Admin API on 9180, alongside the killswitch. A failed drift check fails the sync, so Argo shows it. On k3d the port-forward path stays (GW-07 rule 6); the OpenShift deviation is recorded as a D-entry |
| `guard-policy` seeded once, then changed at runtime | Argo manages `guard-policy-baseline`; it creates `guard-policy` once and then ignores it (`ignoreDifferences` on `data`), so console and `make policy-set` changes aren't reverted |
| Sync waves | 0: namespaces, operators, CRDs · 1: storage, etcd, telemetry CRs · 2: builds · 3: APISIX, services · 4: gateway bootstrap, NetworkPolicies, Routes |

### Argo CD instance (OS-11)

The cluster-wide `openshift-gitops` instance, installed by the OpenShift GitOps operator:
- The demo namespaces carry `argocd.argoproj.io/managed-by: openshift-gitops`.
- The default instance can't create everything we need cluster-wide (operator `Subscription`s, CRDs, `ClusterRole`s, SCC bindings). Two options: a small `ClusterRole` bound to its application controller for just those kinds, or a cluster-admin bootstrap step by `make up` (subscriptions and the GitOps operator itself) before Argo takes over. Decided in workstream 1.
- One root Application per target environment (app-of-apps): `aigw-platform` (operators, namespaces), `aigw-telemetry`, `aigw-workloads`, `aigw-gateway-config`.
- Argo pulls from this repository's GitHub remote; access for a private repository is a read-only deploy key added by `make` as a repository Secret (out of band, like other secrets). Branch: `main` (**OS-14**). While this work is on `openshift-migration`, `make up TARGET=openshift GITOPS_REVISION=<branch>` points the root Application at the branch for testing.

### Namespaces (OS-12)

| k3d | OpenShift |
|---|---|
| `gateway` | `aigw-gateway` |
| `ai` | `aigw-ai` |
| `observability` | `aigw-observability` |
| — | `aigw-build` (BuildConfigs, ImageStreams) |

Service addresses (`*.gateway.svc.cluster.local`, ...) appear in manifests, gateway JSON, the OTel and Grafana config, the CLI and the tests. They become variables: Kustomize replacements in manifests, template variables in `gateway/` (`bootstrap-gateway.sh` already supports `${VAR}`), and a namespace setting in the CLI and tests. NetworkPolicy `namespaceSelector`s already match `kubernetes.io/metadata.name`, so only their values change.

## Repository layout

| Path | What | Used by |
|---|---|---|
| `deploy/base/ai`, `deploy/base/gateway` | Workloads shared by both targets, with `${VAR}` placeholders, and a `kustomization.yaml` each | k3d (`up.sh` applies the files with `sed`), OpenShift (as the base of the overlay) |
| `deploy/k3d/` | Namespaces, ServiceMonitors (`monitoring.coreos.com`), NetworkPolicies (kube-router) and the evidence viewer | k3d only; OpenShift replaces each (`namespaces.yaml`, `monitoring.rhobs`, OVN-K policies, OS-13) |
| `deploy/openshift/apps/{ai,gateway}` | Overlay: `aigw-` namespaces, endpoints, images from `aigw-build` with image triggers, fixed UIDs removed (`restricted-v2`), pinned refs written out | OpenShift (Argo CD) |
| `kustomization.yaml` (repo root) | What Argo CD builds: the overlays plus the ConfigMaps from `scenarios/` and `policy/`; generated by `scripts/openshift/gen-root-kustomization.sh` (D-054) | OpenShift (Argo CD) |
| `deploy/openshift/gitops/` | The `aigw` AppProject and the `aigw-workloads` and `aigw-apisix` Applications, applied by `scripts/openshift/gitops.sh` | OpenShift |
| `deploy/openshift/apps/apisix/values.yaml` | APISIX values layered over `deploy/helm-values/apisix.yaml`: ClusterIP, OVN pod CIDR in the Admin allow-list, etcd in `aigw-gateway`, no `monitoring.coreos.com` ServiceMonitor | OpenShift (`aigw-apisix`) |
| `deploy/openshift/bootstrap/`, `scripts/gateway-apply.sh` | The gateway-bootstrap image and the apply/drift-check code it shares with k3d (D-055) | both (the script), OpenShift (the image) |
| `deploy/openshift/{operators,telemetry,build,llm}` | OpenShift-only platform pieces | OpenShift |
| `deploy/helm-values/` | Chart values (APISIX on both; kps, Loki, Tempo, collector on k3d only) | both |

- **No k3d overlay.** k3d isn't deployed through Argo CD, and its values (registry tags, the host Ollama IP, the API server address) are only known when `up.sh` runs, so it keeps substituting them. Behaviour is unchanged: the files moved without edits.
- **Patches name the base namespace** (`ai`, `gateway`): Kustomize matches them before it renames the namespace.
- **Images are set in the patches**, not with Kustomize's `images:`, which doesn't match `${...}` names. Each workload also carries an `image.openshift.io/triggers` annotation on `aigw-build/<svc>:latest`, so the running pod spec carries the digest; the Argo Application ignores that field.
- **Pinned refs are written out in git** (etcd's `image@digest`). `scripts/openshift/check-overlay-pins.sh`, run by `make pin-check`, fails if any of them isn't the `versions.env` pair (REP-02).
- **`make openshift-check`** runs both drift checks (overlay pins, root kustomization) with no cluster.
- **Secrets** come from `gen-secrets.sh`, which now uses `NS_GATEWAY`/`NS_AI`/`NS_OBS` from `lib.sh` (prefixed `aigw-` on OpenShift); both targets share the values in `.secrets/`.
- **First sync (2026-10-22, `8a6900b`).** Synced from `openshift-migration` with the deploy key. Every `aigw-ai` pod and etcd ready under `restricted-v2` (UIDs assigned by OpenShift); image triggers write the build digest into the pod spec and Argo stays Synced. The kill switch stays unready until APISIX exists (`apisix-admin` doesn't resolve).
- **APISIX and the bootstrap (2026-10-22).** `aigw-apisix` runs the pinned chart under `restricted-v2` with no exception needed (OS-06). The `gateway-bootstrap` Job (a wave-1 Sync hook) applies `gateway/` (D-055), whose service addresses are now `${NS_AI}`/`${NS_OBS}` template variables on both targets. **`smoke.sh` passes against the OpenShift gateway** (through a port-forward): no key 401, demo-user on the LLM 403, the probe gets a mock completion through the guard 200. After a plugin change APISIX needs a restart: k3d rolls it with a checksum annotation, which the Helm values in git can't compute.
- **Live LLM** is in the overlay (workstream 5, D-060). The agents' `LLM_MODEL` stays `demo`; it's cosmetic, because the gateway's llm upstream sets the model.

## Workstreams

Phased so the demo keeps working on k3d throughout: OpenShift is a second **target**, chosen with `TARGET=openshift` (default `k3d`).

1. **Plumbing.** `TARGET` in `lib.sh`; `oc login` check in `prereqs`; manifests reorganised into a Kustomize base plus the OpenShift overlay, with k3d behaviour unchanged (**done**, see [Repository layout](#repository-layout)); OpenShift GitOps install (**done**: operator 1.22.2 and the `openshift-gitops` instance) and the Application (**done**: `aigw-workloads`, first sync verified 2026-10-22); BuildConfigs and ImageStreams (**done**). An app-of-apps root comes when there is more than one Application.
2. **Platform fit.** `restricted-v2` (**done**: every workload runs with an assigned UID, no exceptions so far), NetworkPolicies for OVN-K (**done**, verified by `scripts/openshift/verify-netpol.sh`), Admin allow-list (**done**), Routes with oauth-proxy (**done** for the gateway API, console and kill-switch panel; Grafana with workstream 3), evidence viewer replacement (OS-13: `make ui UI=evidence`, workstream 6). See D-056.
3. **Telemetry.** **Done** (D-058): every signal under Argo CD with k3d's content (alert rules, dashboards, datasource UIDs); APISIX access logs to the LokiStack; Grafana behind OAuth at `aigw-grafana.<domain>`. Remaining for workstream 6: the evidence CLI's readers. The console's Grafana link and trace view ("Where it went") are done: it reads tenant `demo` through the Tempo gateway with its own pod token and the service CA, allowed by ClusterRoleBinding `aigw-console-tempo-reader` (`platform.sh`).
4. **Policy hot reload.** **Done** (D-059): the `policy-sync` sidecar on both targets; k3d no longer sets `sync-frequency=5s`. OpenShift reloads in 1.4–2.9 s (was 60–90 s).
5. **Live LLM.** **Done** (D-060): llama.cpp under Argo CD, always running; fresh installs start live; `make llm-mode MODE=mock|live` switches at runtime on both targets.
6. **Operator tooling.** CLI and tests read Route URLs; `make status` / `logs` / `ui` on OpenShift.
7. **Verification.** `make up` twice (idempotent), `make test` green on OpenShift, `make demo-all`, an evidence bundle that verifies, `verify-netpol.sh` passing on OVN-K. Record deviations as D-0xx in `DECISIONS.md`.

## Risks

- **Shared cluster.** Generic namespace names (`gateway`, `ai`, `observability`) may collide or confuse other tenants.
- **Public exposure.** The ingress domain is likely internet-reachable; today's anonymous Grafana and open Prometheus/Alertmanager would be too.
- **Image pulls.** Docker Hub rate limits from the cluster's egress IP; pin by digest as today, optionally mirror into the internal registry.
- **Demo timings.** 5 s scrape/evaluation and the 10 s reload bound must still hold.
- **TechZone lifetime.** The reservation expires; the plan should rebuild from scratch on a fresh cluster with one command.

## Gotchas found on the cluster

- **Always name `monitoring.rhobs` kinds in full.** `oc get/delete prometheusrule` resolves to the platform's `monitoring.coreos.com` type, so a short name silently acts on the wrong API. Scripts use `prometheusrules.monitoring.rhobs`, `servicemonitors.monitoring.rhobs`, and so on.
- **Collector ServiceMonitors must exclude the headless Service**, or every metric is scraped twice.
- **`wait_for` hides its command's output** (`scripts/lib.sh`), so log after it, not inside it.
- **An Argo PostSync hook waits for every resource to be healthy.** The kill switch is only ready once the bootstrap has written the gateway config, so a PostSync bootstrap deadlocked the first sync. It is a Sync hook in wave 1 with the kill switch in wave 2.
- **APISIX upstream nodes are keyed by `host:port`.** Template variables must be interpolated in object keys too, or a node key keeps a literal `${NS_AI}` and the drift check still passes (desired and live agree).
- **Tempo's webhook checks the requester's own rights.** Enabling `openshift` tenancy needs the applier (Argo CD's controller) to be able to create TokenReviews (D-058).
- **Grafana 13 under the operator: disable plugin preinstall.** Otherwise the bundled Prometheus, Loki and Tempo plugins end up unregistered ("Plugin not registered") after a failed background update on the read-only plugin directory.
- **The operator's Grafana forgets its datasources on restart** (emptyDir) until the next resync; the CRs set `resyncPeriod: 1m`.
- **Set `TARGET` before sourcing `lib.sh`.** It picks the kubeconfig when sourced; setting it afterwards still targets k3d.

## Spikes before implementation

Each is short, on the cluster, in a scratch `aigw-spike` namespace deleted afterwards:

1. **Done.** **LokiStack + OTLP:** `openshift-logging` tenancy on a NooBaa bucket; the collector writes OTLP to the `application` tenant with a bearer token; Grafana queries with `trace_id` structured metadata; 24 h retention.
2. **Done.** **COO Alertmanager:** supply our full route and the killswitch webhook with the bearer token file; 5 s rule evaluation.
3. **Done.** **APISIX logs:** `loki-logger` to the LokiStack gateway with a write-only token (the Red Hat collector has no Loki receiver).
4. **Done, too slow.** **vLLM CPU:** works (tool calls, determinism) but 1.6–1.9 tokens/s; see [Trial 4 results](#trial-4-results-2026-10-22) and **OS-16**.
7. **Done.** **4-bit Granite on CPU:** llama.cpp and Ollama; llama.cpp fits a two-step agent task in about 30 s; see [Trial 5 results](#trial-5-results-2026-10-22-4-bit-granite-on-cpu).
5. **Done.** **BuildConfig:** the shared Dockerfile with `ARG`-based `FROM` lines, and the guard's classifier download, under a Docker-strategy binary build.
6. **Settled.** **openshift-gitops permissions:** operators and namespaces are installed by `make` as cluster-admin (`scripts/openshift/install-operators.sh`); Argo CD only manages namespaced objects in the `aigw-` namespaces.

## Open questions

None. Q-16 is answered by OS-16.

## Decisions

- **OS-01 (Q-1) Telemetry: Red Hat operators.** COO `MonitoringStack`, Tempo Operator, Loki Operator, Red Hat build of OpenTelemetry; Grafana Operator (community) for dashboards. Chosen over keeping the Helm charts for supportability. Nothing is registered with platform monitoring.
- **OS-02 (Q-2) Images: in-cluster `BuildConfig`s** into ImageStreams in a build namespace; workloads pull by digest.
- **OS-03 (Q-3) Live LLM: vLLM on the cluster (CPU) with Granite 4.0 Micro**, instead of the host Ollama. Outputs will differ from the 4-bit Ollama build. *Superseded by OS-16.*
- **OS-04 (Q-4) Exposure: demo UIs on edge-TLS Routes behind OpenShift OAuth proxy**; the gateway API Route stays key-auth only; Prometheus, Alertmanager, evidence and the Admin API by port-forward.
- **OS-05 (Q-5) Policy hot reload: `k8s-sidecar` in the guard pod**, writing the ConfigMap to a shared `emptyDir` the guard already polls. Guard code unchanged; only the sidecar holds an API token. A `GuardPolicy` CRD is a possible later phase.
- **OS-06 (Q-6) Pod security: `restricted-v2` with arbitrary UIDs.** Fixed UIDs come out of the OpenShift overlay; exceptions get `nonroot-v2` on their own service account and are recorded.
- **OS-07 (Q-9) Both targets stay supported**, `TARGET=k3d|openshift`, default `k3d`.
- **OS-08 (Q-10) OpenShift deploys through OpenShift GitOps (Argo CD).** `make` keeps the operator verbs.
- **OS-09 (Q-11) Secrets stay out of band.** `gen-secrets.sh` creates them with `oc` before the first sync; Argo CD doesn't manage Secrets.
- **OS-10 (Q-12) Gateway bootstrap runs as an Argo `PostSync` Job** in-cluster, keeping the drift check and kill-state preservation. Its Admin API access is limited by NetworkPolicy to that Job and the killswitch. k3d keeps the port-forward.
- **OS-11 (Q-13) The cluster-wide `openshift-gitops` Argo CD instance**, with demo namespaces labelled `managed-by` and the extra cluster-scoped permissions it needs granted narrowly.
- **OS-12 (Q-8) OpenShift namespaces are prefixed `aigw-`**; k3d keeps the short names.
- **OS-13 (Q-7) Evidence is served locally** by `make ui UI=evidence`; no in-cluster viewer on OpenShift.
- **OS-14 (Q-14) Argo CD tracks `main`** of the private GitHub repository through a read-only deploy key added out of band; the branch can be overridden for testing.
- **OS-15 Tempo is multi-tenant in `openshift` mode**, the only mode Red Hat supports. Readers (Grafana, console, evidence CLI) use a service-account bearer token.
- **OS-16 (Q-16) Live LLM: llama.cpp in the cluster with IBM's 4-bit `Q4_K_M` GGUF of Granite 4.0 Micro**, 12 CPUs and 12 threads, supersedes vLLM (OS-03). vLLM at full precision generated under 2 tokens/s; llama.cpp reads prompts 2.4× faster than Ollama in the cluster, and its agent tasks come within a few seconds of the presenter's laptop ([trial 5](#trial-5-results-2026-10-22-4-bit-granite-on-cpu)). The k3d target keeps the host Ollama (D-002). Recorded as D-052.
