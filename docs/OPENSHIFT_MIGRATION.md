# OpenShift migration plan

**Status: draft, round 1 answered (OS-01 to OS-04), round 2 pending.** Branch `openshift-migration`. Questions are marked **Q-n**; answers are recorded under [Decisions](#decisions) and folded into the plan as they come in.

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
| Cluster lifecycle | `up.sh` creates a k3d cluster and registry | Cluster exists; `up.sh` only logs in and converges namespaces. `down` deletes the demo namespaces, not a cluster | S |
| Kubeconfig | `.kube/config` written by k3d | Same file, written by `oc login` (or a service-account token) | S |
| Images | Built locally, pushed to the k3d registry | In-cluster binary `BuildConfig`s (Docker strategy) in a build namespace, into ImageStreams; workloads pull by ImageStreamTag digest (**OS-02**) | M |
| Pod security | Fixed `runAsUser` 1000/10001, `fsGroup` | `restricted-v2` assigns a namespace UID; either drop fixed UIDs (images must run as any UID in group 0) or bind `nonroot-v2` to each service account (see **Q-6**) | M |
| Exposure | NodePorts mapped to host ports 8080/3000/8090/9090/9093/8099 | Edge-TLS Routes; demo UIs behind OpenShift OAuth proxy, the rest by port-forward (**OS-04**) | M |
| Console → Grafana link | Built from `GRAFANA_PORT` | Built from a full `GRAFANA_URL` (small code change) | S |
| CLI and tests | Reach `127.0.0.1:<port>` | Read base URLs from settings (Route hosts), falling back to localhost so k3d still works | M |
| Evidence viewer | `hostPath` from the laptop's `evidence/` | No hostPath. Options: PVC + `oc cp` after `make evidence`, or drop the in-cluster viewer and serve locally (see **Q-7**) | S |
| NetworkPolicies | k3s kube-router; DNS in `kube-system` | OVN-K. DNS selectors move to `openshift-dns` (port **5353**); allow ingress from the router (`policy-group.network.openshift.io/ingress`); API server egress to the control-plane IPs on 6443 | M |
| APISIX Admin allow-list | `10.42.0.0/16` | `10.128.0.0/14` | S |
| Policy hot reload (GRD-02, 10 s) | kubelet `sync-frequency=5s` | Default kubelet sync is ~60–90 s. Options: a `KubeletConfig` for workers (rolls the nodes), or have the guard read the ConfigMap through the API (see **Q-5**) | M |
| Live LLM | Ollama on the host | vLLM (CPU backend) in-cluster serving Granite 4.0 Micro (**OS-03**) | L |
| Telemetry | kube-prometheus-stack (own operator + CRDs), Tempo, Loki, OTel Collector, Grafana via Helm | Red Hat operators: COO, Tempo, Loki, OpenTelemetry; Grafana Operator (**OS-01**, [Telemetry](#telemetry)) | L |
| Version pins | `versions.env` | Add new images/charts/operator channels; k3d-only pins stay for the local path | S |

## Telemetry

### The problem with today's chart

`kube-prometheus-stack` installs the `monitoring.coreos.com` CRDs and its own prometheus-operator (v0.94.1). On OpenShift those CRDs are owned by the cluster version operator at the platform's older operator version. Installing ours would overwrite CVO-managed CRDs (the CVO reverts them, and both fight). Leaving our CRDs out and running a v0.94 operator against older CRD schemas is unsupported and can silently prune fields.

### Chosen design: Red Hat operators (OS-01)

Three options were considered: (A) COO for metrics with the Helm charts for the rest, (B) kube-prometheus-stack scoped to our namespaces, and (C) Red Hat operators throughout. **C was chosen.** Grafana comes from the community Grafana Operator, since Red Hat doesn't ship one.

| Signal | Today | OpenShift | Notes |
|---|---|---|---|
| Metrics + alerts | kps Prometheus + Alertmanager | **Cluster Observability Operator** `MonitoringStack` in `observability` | Own operator and own API group `monitoring.rhobs`, so it never touches the platform's `monitoring.coreos.com` CRDs or needs UWM. Our `ServiceMonitor`s and `PrometheusRule` move to `monitoring.rhobs/v1`. The APISIX and collector charts' own ServiceMonitors (`monitoring.coreos.com`) are switched off and replaced by ours. |
| Alert routing (F6) | kps `alertmanager.config` | `MonitoringStack` with its Alertmanager configured from our own `alertmanager-<stack>` Secret (route + killswitch webhook with bearer token file), or an `AlertmanagerConfig` (`monitoring.rhobs/v1alpha1`) | Spike: confirm COO lets us supply the full config and mount the token Secret. |
| Traces | Tempo Helm chart | **Tempo Operator** `TempoMonolithic`, multitenancy off, OTLP gRPC/HTTP receivers, PVC storage on RBD | Query API on :3200 as today, so the Grafana datasource and the console's trace reader (D-046) only change host name. |
| Logs | Loki single binary on RBD | **Loki Operator** `LokiStack` size `1x.demo`, object storage on a NooBaa `ObjectBucketClaim` | Spike: tenancy. Red Hat's documented path is `openshift-logging` tenancy with the collector writing OTLP to the `application` tenant through the LokiStack gateway with a service-account bearer token, and Grafana reading with a token that has `cluster-logging-application-view`. Structured metadata (`trace_id`) and retention 24 h must still work. |
| Collector | OTel Collector Helm chart | **Red Hat build of OpenTelemetry** `OpenTelemetryCollector` (deployment mode) | Same pipelines; Loki exporter gains `bearertokenauth`. Prometheus exporter on :8889 scraped by our `monitoring.rhobs` ServiceMonitor. |
| APISIX access logs | `loki-logger` plugin → Loki :3100 push API | `loki-logger` → LokiStack gateway, or switch APISIX to send logs to the collector | Spike: `loki-logger` can't present a rotating SA token easily; sending through the collector is likely simpler. |
| Dashboards | Grafana in kps, sidecar ConfigMaps | **Grafana Operator** `Grafana` + `GrafanaDatasource` + `GrafanaDashboard` (from the same JSON in ConfigMaps) | Datasource UIDs stay fixed (`prometheus`, `loki`, `tempo`, `alertmanager`) so dashboards are unchanged. Anonymous viewer behind OAuth proxy (OS-04). |

Content is unchanged: dashboards, datasource UIDs, alert expressions, the 5 s scrape and evaluation intervals, and the 24 h retention.

Operators are installed from `redhat-operators` (and `community-operators` for Grafana) by `Subscription`s pinned to a channel with `installPlanApproval: Manual`, and the approved CSV versions are recorded in `versions.env` (REP-02).

## Live LLM on vLLM (OS-03)

The gateway's live provider is already OpenAI-compatible (`gateway/upstreams/llm.live.json`), so only the URL and model name change.

- **Server.** vLLM's CPU backend as a Deployment in `ai`, OpenAI API on :8000, `--enable-auto-tool-choice` with the Granite tool-call parser. The workers have AVX-512F and VNNI but no AVX512-BF16 or AMX (checked 2026-10-22), so `--dtype` is a spike item (bf16 falls back to slower kernels; fp32 doubles memory).
- **Model.** `ibm-granite/granite-4.0-micro` from Hugging Face at a pinned revision (the same model Ollama's `granite4:micro` packages). Weights go into a PVC by an init step that verifies SHA-256, as the guard does for its classifier (GRD-01), so the server makes no network calls at runtime.
- **Not byte-identical to today.** Ollama serves a 4-bit GGUF; vLLM serves full-precision weights. Live answers will differ from the k3d runs, but `temperature: 0` and `seed` still apply.
- **Sizing.** About 3B parameters: roughly 7 GiB of weights in bf16 or 13 GiB in fp32, plus KV cache. Request 8–12 CPUs and 16–24 GiB on one worker. Spike: tokens per second, and whether the 120 s gateway timeout and the agents' 6 steps still fit a live scenario on stage.
- **NetworkPolicy.** Only APISIX reaches vLLM (it joins the `from-gateway` selector); the init step needs egress to `huggingface.co` once, or the weights are staged by a build-namespace Job.
- **Version pins.** vLLM CPU image by digest, model revision and file SHA-256s in `versions.env`.

## Images: in-cluster builds (OS-02)

- A build namespace (no default-deny NetworkPolicy, so the guard build can fetch its classifier) holds one binary `BuildConfig` per service, Docker strategy, using `services/Dockerfile` with the same `--build-arg`s as `build-images.sh` (pinned base images, classifier SHA-256s).
- `build-images.sh` on OpenShift runs `oc start-build <svc> --from-dir=services --wait` and tags the ImageStream `<svc>:<git-tag>` as today. It skips any service whose `ImageStreamTag` already exists (REP-07).
- Workloads in `ai` and `gateway` pull from `image-registry.openshift-image-registry.svc:5000/<build-ns>/<svc>@sha256:…` with a `system:image-puller` RoleBinding. Manifests get the digest, not the tag.
- No local container runtime is needed on OpenShift.

## Exposure (OS-04)

| Endpoint | Route | Auth |
|---|---|---|
| Console (`/console` on APISIX) | `console-…` edge TLS → oauth-proxy → APISIX | OpenShift OAuth; plus the console's own controls as today |
| Gateway API (CLI, tests) | `gateway-…` edge TLS → APISIX directly | Consumer keys (key-auth) as today. Not behind OAuth, so the CLI and tests keep working. |
| Grafana | `grafana-…` edge TLS → oauth-proxy → Grafana | OpenShift OAuth, then anonymous Viewer |
| Kill-switch panel | `killswitch-…` edge TLS → oauth-proxy → panel | OpenShift OAuth, then the operator token as today |
| Prometheus, Alertmanager, evidence, APISIX Admin | none | `make ui UI=…` / `make admin-ui` port-forward |

The oauth-proxy sidecars limit access with `--openshift-sar` to users who can `get` Services in the demo namespaces, so cluster-admin and invited presenters get in and other tenants don't. The NetworkPolicies allow ingress from the router namespace only to the oauth-proxy ports (and APISIX :9080 for the gateway Route).

## Workstreams

Phased so the demo keeps working on k3d throughout: OpenShift is a second **target**, chosen with `TARGET=openshift` (default `k3d`).

1. **Plumbing.** `TARGET` in `lib.sh`; `oc login` check in `prereqs`; namespace creation without cluster creation; image build and push to the internal registry; `down` deletes namespaces only.
2. **Platform fit.** SCC strategy, NetworkPolicies for OVN-K, Admin allow-list, Routes, evidence viewer replacement.
3. **Telemetry.** Operator subscriptions, then the CRs in [Telemetry](#telemetry), with dashboards, datasources and alert rules unchanged in content. Spikes first: LokiStack tenancy and OTLP ingest, COO Alertmanager config, APISIX logs.
4. **Policy hot reload.** The option chosen in **Q-5**.
5. **Live LLM.** vLLM spike (dtype, tokens per second, tool calling with Granite), then the Deployment, model staging and the `llm.live` upstream.
6. **Operator tooling.** CLI and tests read Route URLs; console Grafana link; `make status` / `logs` / `ui` on OpenShift.
7. **Verification.** `make up` twice (idempotent), `make test` green on OpenShift, `make demo-all`, an evidence bundle that verifies, `verify-netpol.sh` passing on OVN-K. Record deviations as D-0xx in `DECISIONS.md`.

## Risks

- **Shared cluster.** Generic namespace names (`gateway`, `ai`, `observability`) may collide or confuse other tenants.
- **Public exposure.** The ingress domain is likely internet-reachable; today's anonymous Grafana and open Prometheus/Alertmanager would be too.
- **Image pulls.** Docker Hub rate limits from the cluster's egress IP; pin by digest as today, optionally mirror into the internal registry.
- **Demo timings.** 5 s scrape/evaluation and the 10 s reload bound must still hold.
- **TechZone lifetime.** The reservation expires; the plan should rebuild from scratch on a fresh cluster with one command.

## Open questions

- **Q-5** How the 10 s policy hot reload is met.
- **Q-6** SCC strategy: arbitrary UIDs or `nonroot-v2`.
- **Q-7** Evidence viewer.
- **Q-8** Namespace names on a shared cluster.
- **Q-9** Keep k3d as a supported target, or replace it?
- **Q-10** Deployment mechanism: keep `make` + scripts, or move to GitOps/Tekton (OpenShift Pipelines is installed)?

## Decisions

- **OS-01 (Q-1) Telemetry: Red Hat operators.** COO `MonitoringStack`, Tempo Operator, Loki Operator, Red Hat build of OpenTelemetry; Grafana Operator (community) for dashboards. Chosen over keeping the Helm charts for supportability. Nothing is registered with platform monitoring.
- **OS-02 (Q-2) Images: in-cluster `BuildConfig`s** into ImageStreams in a build namespace; workloads pull by digest.
- **OS-03 (Q-3) Live LLM: vLLM on the cluster (CPU) with Granite 4.0 Micro**, instead of the host Ollama. Outputs will differ from the 4-bit Ollama build.
- **OS-04 (Q-4) Exposure: demo UIs on edge-TLS Routes behind OpenShift OAuth proxy**; the gateway API Route stays key-auth only; Prometheus, Alertmanager, evidence and the Admin API by port-forward.
