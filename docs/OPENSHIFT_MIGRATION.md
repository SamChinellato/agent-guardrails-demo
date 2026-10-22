# OpenShift migration plan

**Status: draft, open questions pending.** Branch `openshift-migration`. Questions are marked **Q-n**; answers are recorded under [Decisions](#decisions) and folded into the plan as they come in.

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
| Images | Built locally, pushed to the k3d registry | Built locally with Podman, pushed to the internal registry via its route (see **Q-2**) | M |
| Pod security | Fixed `runAsUser` 1000/10001, `fsGroup` | `restricted-v2` assigns a namespace UID; either drop fixed UIDs (images must run as any UID in group 0) or bind `nonroot-v2` to each service account (see **Q-6**) | M |
| Exposure | NodePorts mapped to host ports 8080/3000/8090/9090/9093/8099 | Routes with edge TLS on the ingress domain (see **Q-4**) | M |
| Console → Grafana link | Built from `GRAFANA_PORT` | Built from a full `GRAFANA_URL` (small code change) | S |
| CLI and tests | Reach `127.0.0.1:<port>` | Read base URLs from settings (Route hosts), falling back to localhost so k3d still works | M |
| Evidence viewer | `hostPath` from the laptop's `evidence/` | No hostPath. Options: PVC + `oc cp` after `make evidence`, or drop the in-cluster viewer and serve locally (see **Q-7**) | S |
| NetworkPolicies | k3s kube-router; DNS in `kube-system` | OVN-K. DNS selectors move to `openshift-dns` (port **5353**); allow ingress from the router (`policy-group.network.openshift.io/ingress`); API server egress to the control-plane IPs on 6443 | M |
| APISIX Admin allow-list | `10.42.0.0/16` | `10.128.0.0/14` | S |
| Policy hot reload (GRD-02, 10 s) | kubelet `sync-frequency=5s` | Default kubelet sync is ~60–90 s. Options: a `KubeletConfig` for workers (rolls the nodes), or have the guard read the ConfigMap through the API (see **Q-5**) | M |
| Live LLM | Ollama on the host | No host. In-cluster Ollama on CPU, an external endpoint, or mock only (see **Q-3**) | M |
| Telemetry | kube-prometheus-stack (own operator + CRDs), Tempo, Loki, OTel Collector, Grafana via Helm | Self-contained stack that does not touch the platform's CRDs (see [Telemetry](#telemetry) and **Q-1**) | L |
| Version pins | `versions.env` | Add new images/charts/operator channels; k3d-only pins stay for the local path | S |

## Telemetry

### The problem with today's chart

`kube-prometheus-stack` installs the `monitoring.coreos.com` CRDs and its own prometheus-operator (v0.94.1). On OpenShift those CRDs are owned by the cluster version operator at the platform's older operator version. Installing ours would overwrite CVO-managed CRDs (the CVO reverts them, and both fight). Leaving our CRDs out and running a v0.94 operator against older CRD schemas is unsupported and can silently prune fields.

### Options

| | A. Cluster Observability Operator + existing Helm charts (recommended) | B. Keep kube-prometheus-stack, scoped | C. All Red Hat operators |
|---|---|---|---|
| Metrics and alerts | COO `MonitoringStack` (Prometheus + Alertmanager) in `observability`, API group `monitoring.rhobs` | kps with `crds.enabled: false`, operator watching only our namespaces | COO `MonitoringStack` |
| Traces | Tempo Helm chart (as today) | Tempo Helm chart | Tempo Operator `TempoMonolithic` |
| Logs | Loki Helm chart, single binary on RBD (as today) | Loki Helm chart | Loki Operator `LokiStack` on NooBaa object storage |
| Collector | OTel Collector Helm chart (as today) | OTel Collector Helm chart | Red Hat build of OpenTelemetry |
| Dashboards | Grafana Helm chart (standalone, sidecar provisioning as today) | Grafana inside kps (as today) | Grafana Operator (community) |
| Supported by Red Hat | Metrics only | No | Mostly (not Grafana) |
| Change to repo | ServiceMonitor/PrometheusRule move to `monitoring.rhobs/v1`; chart-made ServiceMonitors replaced by our own | Smallest | Largest: LokiStack tenancy/auth and gateway, Tempo CR, collector CR, Grafana CRs |
| Risk | Low | CRD version skew with the platform | Most moving parts for a demo |

With option A, alert routing to the killswitch (F6) stays in Alertmanager config, Grafana keeps reading datasources and dashboards from ConfigMaps, and nothing is registered with platform monitoring.

## Workstreams

Phased so the demo keeps working on k3d throughout: OpenShift is a second **target**, chosen with `TARGET=openshift` (default `k3d`).

1. **Plumbing.** `TARGET` in `lib.sh`; `oc login` check in `prereqs`; namespace creation without cluster creation; image build and push to the internal registry; `down` deletes namespaces only.
2. **Platform fit.** SCC strategy, NetworkPolicies for OVN-K, Admin allow-list, Routes, evidence viewer replacement.
3. **Telemetry.** The option chosen in **Q-1**, with dashboards, datasources and alert rules unchanged in content.
4. **Policy hot reload.** The option chosen in **Q-5**.
5. **Live LLM.** The option chosen in **Q-3**.
6. **Operator tooling.** CLI and tests read Route URLs; console Grafana link; `make status` / `logs` / `ui` on OpenShift.
7. **Verification.** `make up` twice (idempotent), `make test` green on OpenShift, `make demo-all`, an evidence bundle that verifies, `verify-netpol.sh` passing on OVN-K. Record deviations as D-0xx in `DECISIONS.md`.

## Risks

- **Shared cluster.** Generic namespace names (`gateway`, `ai`, `observability`) may collide or confuse other tenants.
- **Public exposure.** The ingress domain is likely internet-reachable; today's anonymous Grafana and open Prometheus/Alertmanager would be too.
- **Image pulls.** Docker Hub rate limits from the cluster's egress IP; pin by digest as today, optionally mirror into the internal registry.
- **Demo timings.** 5 s scrape/evaluation and the 10 s reload bound must still hold.
- **TechZone lifetime.** The reservation expires; the plan should rebuild from scratch on a fresh cluster with one command.

## Open questions

- **Q-1** Telemetry stack: A, B or C?
- **Q-2** Where images are built and stored.
- **Q-3** Live LLM mode on OpenShift.
- **Q-4** What is exposed through Routes, and to whom.
- **Q-5** How the 10 s policy hot reload is met.
- **Q-6** SCC strategy: arbitrary UIDs or `nonroot-v2`.
- **Q-7** Evidence viewer.
- **Q-8** Namespace names on a shared cluster.
- **Q-9** Keep k3d as a supported target, or replace it?
- **Q-10** Deployment mechanism: keep `make` + scripts, or move to GitOps/Tekton (OpenShift Pipelines is installed)?

## Decisions

_None yet._
