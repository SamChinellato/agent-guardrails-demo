# OpenShift migration plan

**Status: draft, all planning questions answered (OS-01 to OS-14); spikes and implementation next.** Branch `openshift-migration`. Questions are marked **Q-n**; answers are recorded under [Decisions](#decisions) and folded into the plan as they come in.

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
| Console → Grafana link | Built from `GRAFANA_PORT` | Built from a full `GRAFANA_URL` (small code change) | S |
| CLI and tests | Reach `127.0.0.1:<port>` | Read base URLs from settings (Route hosts), falling back to localhost so k3d still works | M |
| Evidence viewer | `hostPath` from the laptop's `evidence/` | No in-cluster viewer. `make ui UI=evidence` serves `evidence/` read-only from the presenter's machine; bundles never leave it (**OS-13**) | S |
| NetworkPolicies | k3s kube-router; DNS in `kube-system` | OVN-K. DNS selectors move to `openshift-dns` (port **5353**); allow ingress from the router (`policy-group.network.openshift.io/ingress`); API server egress to the control-plane IPs on 6443 | M |
| APISIX Admin allow-list | `10.42.0.0/16` | `10.128.0.0/14` | S |
| Policy hot reload (GRD-02, 10 s) | kubelet `sync-frequency=5s` | `k8s-sidecar` in the guard pod watches the ConfigMap through the API and writes it to a shared `emptyDir`; guard code unchanged (**OS-05**, [Policy hot reload](#policy-hot-reload-os-05)) | S |
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
| Traces | Tempo Helm chart | **Tempo Operator** `TempoMonolithic` (Tempo 2.10), multi-tenant in `openshift` mode with one tenant `demo`, PVC on RBD (**OS-15**) | **Verified.** Single-tenant is unsupported on OpenShift (the operator warns). The collector writes through the gateway with its token; readers (SA `tempo-reader`) query `https://tempo-tempo-gateway:8080/api/traces/v1/demo/tempo/api/...`. Fetch by ID and TraceQL search both work. The console's trace reader (D-046), Grafana and the evidence CLI need a bearer token and the new URL. |
| Logs | Loki single binary on RBD | **Loki Operator** `LokiStack` size `1x.demo`, `openshift-logging` tenancy, object storage on a NooBaa `ObjectBucketClaim` | **Verified (trial 1).** The Red Hat build always deploys the token-checking gateway. The collector (SA `otel-collector`) writes OTLP to the `application` tenant. Grafana and the evidence CLI read as SA `loki-reader`: a cluster-scope `get` on the tenant plus `get` on only the demo namespaces, so queries are limited to them (another namespace is refused). `service_name` stays a stream label and record attributes are structured metadata, so today's queries work unchanged. Every record must also carry `kubernetes.namespace_name`, which the gateway filters on; the collector adds it. Retention 1 d. |
| Collector | OTel Collector Helm chart | **Red Hat build of OpenTelemetry** `OpenTelemetryCollector` (deployment mode), service `otel-collector` | **Verified.** Same pipelines, plus `k8s_attributes` (namespace, pod, deployment) and a transform adding `kubernetes.namespace_name`. Exports to both gateways with `bearertokenauth` and the service CA. Prometheus exporter on :8889 scraped by our `monitoring.rhobs` ServiceMonitor. |
| APISIX access logs | `loki-logger` plugin → Loki :3100 push API | `loki-logger` → the LokiStack gateway push API with a bearer token for SA `apisix-log-writer` (write-only) | **Verified.** The Red Hat collector has no Loki receiver. `loki-logger` supports `headers`, which APISIX encrypts at rest. The token is a long-lived service-account token Secret (a plugin config can't rotate one), rendered by the gateway bootstrap. Labels add both namespace labels; `| json | trace_id=` queries work. |
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
- Workloads in `ai` and `gateway` pull from `image-registry.openshift-image-registry.svc:5000/<build-ns>/<svc>` with a `system:image-puller` RoleBinding. With GitOps (OS-08) the digest can't be written into the manifests at apply time any more; see [GitOps](#gitops-os-08).
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

## Policy hot reload (OS-05)

The guard decides on content; APISIX only enforces its verdict (see `docs/DETECTION.md`). The guard's policy lives in the `guard-policy` ConfigMap, and `PolicyStore` (`services/guard/src/guard/policy.py`) re-reads `/policy/policy.yaml` every second. On OpenShift the kubelet takes 60–90 s to update a mounted ConfigMap, which misses the 10 s bound.

```
guard pod
├── k8s-sidecar   watches ConfigMap guard-policy through the API ──► writes /policy/policy.yaml (emptyDir)
└── guard         PolicyStore polls /policy/policy.yaml every 1 s (unchanged)
```

- **Guard code unchanged.** Parsing, validation and "keep the previous policy on a bad file" stay where they are; the guard doesn't know about Kubernetes.
- **Least privilege.** `automountServiceAccountToken: false` stays on the pod. Only the sidecar container mounts a projected service-account token. Its Role allows `get`/`list`/`watch` on `configmaps` named `guard-policy` in `ai`.
- **NetworkPolicy.** The guard pod gains egress to the API server on 6443 (the control-plane IPs), like the console.
- **Both targets.** The same on k3d, so the kubelet `sync-frequency=5s` argument can leave `k3d/cluster.yaml`.
- **Image.** `K8S_SIDECAR_IMAGE` in `versions.env`, already pinned for Grafana.
- **Readiness.** The guard is ready only once a policy is parsed (GRD-08), so it waits for the sidecar's first write.

Patterns considered: the mounted file alone (kubelet latency), restart on change (Reloader or hashed ConfigMaps; the guard fails closed while it reloads the model), the guard watching the API itself (couples the guard to Kubernetes and gives it a token), and a `GuardPolicy` CRD. **Possible later phase:** the CRD, which adds validation when a policy is written (CEL), a `status.appliedVersion` the console and tests can wait on, and per-object RBAC and audit.

## GitOps (OS-08)

OpenShift GitOps (Argo CD) syncs the OpenShift overlay from this repository; `make` keeps the operator verbs (`demo`, `test`, `evidence`, `trip`, `reset`, `status`). What moves where:

| Today (`up.sh`) | Under Argo CD |
|---|---|
| `sed` substitution into manifests (`${GUARD_IMAGE}`, `${LLM_MODEL}`, ...) | Kustomize overlays (`deploy/overlays/k3d`, `deploy/overlays/openshift`) with the values in git; k3d keeps rendering the same base through `up.sh` |
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

## Workstreams

Phased so the demo keeps working on k3d throughout: OpenShift is a second **target**, chosen with `TARGET=openshift` (default `k3d`).

1. **Plumbing.** `TARGET` in `lib.sh`; `oc login` check in `prereqs`; manifests reorganised into a Kustomize base plus `k3d` and `openshift` overlays, with k3d behaviour unchanged; OpenShift GitOps install and the root Application; BuildConfigs and ImageStreams.
2. **Platform fit.** `restricted-v2` (image runs as any UID in group 0), NetworkPolicies for OVN-K, Admin allow-list, Routes with oauth-proxy, evidence viewer replacement.
3. **Telemetry.** Operator subscriptions, then the CRs in [Telemetry](#telemetry), with dashboards, datasources and alert rules unchanged in content. Spikes first: LokiStack tenancy and OTLP ingest, COO Alertmanager config, APISIX logs.
4. **Policy hot reload.** `k8s-sidecar` in the guard pod on both targets; drop `sync-frequency=5s` from k3d; acceptance test for the 10 s bound on both.
5. **Live LLM.** vLLM spike (dtype, tokens per second, tool calling with Granite), then the Deployment, model staging and the `llm.live` upstream.
6. **Operator tooling.** CLI and tests read Route URLs; console Grafana link; `make status` / `logs` / `ui` on OpenShift.
7. **Verification.** `make up` twice (idempotent), `make test` green on OpenShift, `make demo-all`, an evidence bundle that verifies, `verify-netpol.sh` passing on OVN-K. Record deviations as D-0xx in `DECISIONS.md`.

## Risks

- **Shared cluster.** Generic namespace names (`gateway`, `ai`, `observability`) may collide or confuse other tenants.
- **Public exposure.** The ingress domain is likely internet-reachable; today's anonymous Grafana and open Prometheus/Alertmanager would be too.
- **Image pulls.** Docker Hub rate limits from the cluster's egress IP; pin by digest as today, optionally mirror into the internal registry.
- **Demo timings.** 5 s scrape/evaluation and the 10 s reload bound must still hold.
- **TechZone lifetime.** The reservation expires; the plan should rebuild from scratch on a fresh cluster with one command.

## Spikes before implementation

Each is short, on the cluster, in a scratch `aigw-spike` namespace deleted afterwards:

1. **Done.** **LokiStack + OTLP:** `openshift-logging` tenancy on a NooBaa bucket; the collector writes OTLP to the `application` tenant with a bearer token; Grafana queries with `trace_id` structured metadata; 24 h retention.
2. **Next.** **COO Alertmanager:** supply our full route and the killswitch webhook with the bearer token file; 5 s rule evaluation.
3. **Done.** **APISIX logs:** `loki-logger` to the LokiStack gateway with a write-only token (the Red Hat collector has no Loki receiver).
4. **vLLM CPU:** dtype on AVX-512 without BF16, tokens per second, Granite tool calling through the gateway's `ai-proxy`.
5. **Done.** **BuildConfig:** the shared Dockerfile with `ARG`-based `FROM` lines, and the guard's classifier download, under a Docker-strategy binary build.
6. **Settled.** **openshift-gitops permissions:** operators and namespaces are installed by `make` as cluster-admin (`scripts/openshift/install-operators.sh`); Argo CD only manages namespaced objects in the `aigw-` namespaces.

## Open questions

None at the moment. New ones will be added here as spikes turn them up.

## Decisions

- **OS-01 (Q-1) Telemetry: Red Hat operators.** COO `MonitoringStack`, Tempo Operator, Loki Operator, Red Hat build of OpenTelemetry; Grafana Operator (community) for dashboards. Chosen over keeping the Helm charts for supportability. Nothing is registered with platform monitoring.
- **OS-02 (Q-2) Images: in-cluster `BuildConfig`s** into ImageStreams in a build namespace; workloads pull by digest.
- **OS-03 (Q-3) Live LLM: vLLM on the cluster (CPU) with Granite 4.0 Micro**, instead of the host Ollama. Outputs will differ from the 4-bit Ollama build.
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
