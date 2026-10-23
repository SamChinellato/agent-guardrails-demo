# Configuration reference

Everything that configures the demo lives in git, except secrets and machine-local settings. This page covers the gateway config, the guard policy, version pins, secrets, settings, and the `make` targets.

## Gateway config (`gateway/`)

APISIX's whole data-plane config is JSON in `gateway/`, one file per object, applied by `scripts/gateway-apply.sh` (GW-08).

| Directory | Objects |
| --- | --- |
| `consumers/` | `demo-user`, `researcher`, `executor`, `probe`: key-auth consumers |
| `routes/` | `agent-researcher`, `agent-executor`, `llm`, `llm-scripted`, `mcp-docs`, `mcp-actions`, `console`, `console-root`, `demo-documents`, `guard-policy` |
| `plugin_configs/` | `pc-llm`, `pc-mcp`, `pc-agents`: the shared observability plugins, plus the pre-staged, disabled `fault-injection` the kill switch turns on |
| `upstreams/` | `guard` (MCP routes go through it); `llm.mock.json` and `llm.live.json`, the LLM provider for each mode |
| `plugin_metadata/` | `opentelemetry`: the collector address and batching (D-020) |
| `plugins/` | The custom Lua plugins `ai-injection-guard` (calls the guard) and `agent-kill-switch` (per-agent kill, D-013) |

**How it's applied.** Each object is PUT on its fixed ID only if it differs from the live one, then every object is read back, and any drift fails the run. An engaged kill switch is preserved: its markers on plugin configs and consumers survive a re-apply (D-016, D-039). k3d applies through a port-forward to the Admin API; OpenShift through the `gateway-bootstrap` Job in each Argo CD sync (D-055).

**Template variables** are filled in at apply time, so the files hold no secrets and no per-target addresses:

| Variable | Filled with |
| --- | --- |
| `CONSUMER_KEY_*` | The consumer keys, from Secrets |
| `NS_AI`, `NS_GATEWAY`, `NS_OBS` | `ai`, `gateway`, `observability` on k3d; `aigw-`-prefixed on OpenShift |
| `LLM_PROVIDER`, `LLM_PROVIDER_MOCK` | The LLM upstream for the current mode, and the mock one for `llm-scripted` |
| `OLLAMA_URL`, `OLLAMA_MODEL`, `OLLAMA_SEED` | The live model: the host's Ollama on k3d, llama.cpp on OpenShift |
| `LOKI_URL`, `LOKI_PUSH_PATH`, `LOKI_HEADERS` | Where APISIX's access logs go, with the writer token on OpenShift |

To change the gateway, edit the JSON and run `make up` (k3d) or push (OpenShift). A change made by hand in the APISIX dashboard is reported as drift by the next apply.

## Guard policy

`policy/policy.yaml` is the baseline; the live copy is the `guard-policy` ConfigMap, which the guard reloads within seconds ([RUNBOOK.md](RUNBOOK.md#changing-guard-policy)). The file as shipped:

```yaml
version: 1
checks:
  llm.user_prompt_injection:   { mode: enforce, threshold: 0.90, severity: high }
  llm.tool_message_injection:  { mode: enforce, threshold: 0.85, severity: high }
  mcp.tool_result_injection:   { mode: enforce, threshold: 0.85, severity: high }
  mcp.tool_allowlist:          { mode: enforce, severity: critical }
  mcp.argument_rules:          { mode: enforce, severity: critical }
  agent.user_prompt_injection: { mode: enforce, threshold: 0.90, severity: high }
agents:
  researcher:
    tools: [docs.list_documents, docs.read_document]
  executor:
    tools: [actions.update_record, actions.send_email]
    argument_rules:
      actions.send_email:    { to_domain_allow: [demo.local] }
      actions.update_record: { fields_allow: [status, notes] }
  probe:
    tools: []
deny_tools_all: [actions.export_customer_data]
auto_trip: { on_severity: critical, scope: agent, dedupe_seconds: 60 }
```

### Checks

| Check | Where | Judges |
| --- | --- | --- |
| `agent.user_prompt_injection` | `agent-researcher` route | What you type, before the agent sees it |
| `llm.user_prompt_injection` | `llm` routes | User messages an agent sends to the model |
| `llm.tool_message_injection` | `llm` routes | Tool messages: the classifier for tool results the MCP proxy inspected, heuristics only for text the agent wrote itself (D-034) |
| `mcp.tool_result_injection` | MCP proxy | What a tool returned; each paragraph and HTML comment is scored separately (D-026) |
| `mcp.tool_allowlist` | MCP proxy | Whether this agent may call this tool; also filters `tools/list`, so agents only see their own tools |
| `mcp.argument_rules` | MCP proxy | The call's arguments, against the agent's rules |

Each check has:

- **`mode`:** `enforce` blocks, `monitor` records a flag and lets the call through, `off` skips the check
- **`threshold`:** for the injection checks, the score (0 to 1) at which text counts as an injection
- **`severity`:** `low`, `medium`, `high` or `critical`. A critical block in enforce mode trips the agent (`auto_trip`)

### Agents and rules

- **`agents.<name>.tools`:** the tools the agent may call, as `server.tool`. Anything else is a `mcp.tool_allowlist` violation.
- **`argument_rules`**, per tool:
  - `to_domain_allow: [domains]`: every recipient must be at one of these domains
  - `fields_allow: [fields]`: only these fields may be changed
- **`deny_tools_all`:** tools no agent may call, whatever its allowlist says.
- **`auto_trip`:** trip the agent on a block of this severity; repeat trips within `dedupe_seconds` are skipped while the agent is still stopped.

Unknown check names, modes or severities, and thresholds outside 0–1, make the file invalid; the guard keeps the previous policy and logs why.

## Version pins (`versions.env`)

Every external input is pinned (REP-02):

- **Host tools:** k3d, kubectl and helm (with SHA-256 sums per platform), uv, Python, minimum Docker and Podman versions
- **Cluster:** the k3s image and the registry
- **Helm charts**, by version
- **Images**, by tag and digest
- **Models:** the Ollama model by digest; the classifier by Hugging Face revision and file SHA-256s; the GGUF by repository, revision and file SHA-256
- **OpenShift operators**, by channel and exact CSV

| Command | What it does |
| --- | --- |
| `make pin-check` | Fails if an upstream digest has drifted from its tag, or the OpenShift overlay's written-out pins differ from `versions.env` |
| `make openshift-check` | The overlay check, plus a stale repo-root `kustomization.yaml`; needs no cluster |
| `scripts/pin-digests.sh` | Resolves `*_DIGEST` lines from the matching `*_IMAGE` tags, after a deliberate version change |

Built images are tagged with the git SHA (plus a content hash for uncommitted changes, D-012), so a deployed image always names the code it came from.

## Secrets

`scripts/gen-secrets.sh` generates every secret once into `.secrets/` (gitignored, mode 600) and into Kubernetes Secrets; re-runs reuse the files, so both targets share the same values.

| File | Used for |
| --- | --- |
| `consumer-demo-user`, `consumer-researcher`, `consumer-executor`, `consumer-probe` | API keys for the console, the agents and the kill-switch probe |
| `apisix-admin-key`, `apisix-viewer-key` | The Admin API: full and read-only |
| `killswitch-operator-token` | The control panel and `make trip` |
| `killswitch-guard-token`, `killswitch-alertmanager-token` | The guard's and Alertmanager's calls to the kill switch (KS-09) |
| `grafana-admin-password` | Grafana's `admin` user |
| `oauth-proxy-cookie` | The OpenShift OAuth proxies' cookie secret |
| `argocd-deploy-key`, `.pub` | Argo CD's read-only access to the repository (OpenShift) |

Argo CD never manages Secrets (OS-09). `make evidence` scans every bundle for every value in `.secrets/` before sealing it, and the acceptance suite checks that it does (`test_no_secrets_and_excerpts_redacted`).

## Settings

Set these in the environment or, for machine-local values, in `.env.local` (gitignored, read by the scripts and tests; the environment wins).

| Setting | Default | Meaning |
| --- | --- | --- |
| `TARGET` | `k3d` | `openshift` selects `~/.kube/config` and the `aigw-` namespaces |
| `LLM_MODE` | `mock` | `live` uses the real model (k3d `make up`; `make llm-mode` switches at runtime) |
| `KUBECONFIG_DEMO` | `.kube/config` (k3d), `~/.kube/config` (OpenShift) | Use a different kubeconfig |
| `GITOPS_REVISION` | the branch Argo CD already follows, else `main` | The branch `scripts/openshift/gitops.sh` points Argo CD at |
| `CONTAINER_RUNTIME` | detected | Force `docker` or `podman` |

### Machine-local settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `HOST_BIND` | `0.0.0.0` | The address host ports listen on; `127.0.0.1` keeps the demo off the LAN (D-023) |
| `HOST_PORT_GATEWAY` | `8080` | Gateway and console |
| `HOST_PORT_GRAFANA` | `3000` | Grafana |
| `HOST_PORT_PANEL` | `8090` | Kill-switch panel |
| `HOST_PORT_PROMETHEUS` | `9090` | Prometheus |
| `HOST_PORT_ALERTMANAGER` | `9093` | Alertmanager |
| `HOST_PORT_EVIDENCE` | `8099` | Evidence viewer |

k3d fixes port mappings when it creates the cluster; changing a port or `HOST_BIND` needs `make down && make up`.

## Make targets

| Target | What it does | OpenShift |
| --- | --- | --- |
| `make help` | List targets | |
| `make tools` | Install pinned k3d, kubectl and helm into `.tools/bin` | |
| `make prereqs [LLM_MODE=live]` | Check the runtime, tools, memory, disk, ports, and (live) the host Ollama | k3d only |
| `make prefetch` | Pull every pinned image and chart, so `make up` works offline | k3d only |
| `make up [LLM_MODE=…]` | Create or repair the cluster and deploy everything | see [DEPLOYMENT.md](DEPLOYMENT.md#openshift) |
| `make down` | Delete the cluster and registry; keep the cache and secrets | k3d only |
| `make status` | Pods, kill-switch state, policy modes, firing alerts | k3d only |
| `make reset` | Clean demo state without rebuilding | k3d only; use **New chat** |
| `make demo SCENARIO=s0…s6 [LIVE=1]` | Run one scenario from the CLI | k3d only |
| `make demo-all` | Run S0–S5 with a reset after each | k3d only |
| `make trip SCOPE=… REASON=…` / `make kill-reset …` | Operate the kill switch | k3d only; use the panel |
| `make policy-set CHECK=… MODE=…` | Change one check's mode | k3d only |
| `make llm-mode MODE=mock\|live` | Switch the LLM without a redeploy | `TARGET=openshift` |
| `make evidence INCIDENT=…\|TRACE=…` | Build an evidence bundle | k3d only |
| `make evidence-verify DIR=…` | Verify a bundle's hashes and audit chain | works on any bundle |
| `make test [PYTEST_ARGS=…]` | The acceptance suite, in mock mode | k3d only |
| `make logs SVC=…` | Tail one Deployment's or StatefulSet's logs | `TARGET=openshift` |
| `make ui UI=…` | Print or open a secondary UI | k3d only |
| `make admin-ui` | The APISIX dashboard through a port-forward | use `aigw-apisix.<domain>` |
| `make diagram` | Render both architecture images | |
| `make pin-check`, `make openshift-check` | Version and overlay drift checks | |
