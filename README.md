# AI Gateway Security Demo

Apache APISIX as an AI gateway between two cooperating agents and their LLM and MCP tools, on a local k3d cluster. It shows four things: prompt-injection attacks, observability, a kill switch, and a tamper-evident evidence bundle. The full design is in [`SPEC.md`](SPEC.md). Decisions and deviations are in [`docs/DECISIONS.md`](docs/DECISIONS.md), and progress is in [`docs/REQUIREMENTS_STATUS.md`](docs/REQUIREMENTS_STATUS.md).

> **Status: P3 done** (cluster, gateway, observability, guard, MCP tools, NetworkPolicies). Grafana is at `http://<host>:3000` by default; `make up` prints the exact URLs, including the LAN ones. You can view it without logging in; to log in, click **Sign in** and use user `admin` with the password in `.secrets/grafana-admin-password`. dashboards: AI Gateway Overview, Agent Behaviour, Incident. `make tools`, `prereqs`, `prefetch`, `up`, `down`, `test`, `logs` and `pin-check` work. Other targets exit and name the phase that implements them.

## Prerequisites

- **Container runtime:** Docker, or rootless Podman 5 or later. Rootless Podman needs a one-time setup, which `make prereqs` explains if it's missing.
- **Tools:** uv, jq, make and curl. `make tools` installs pinned, checksum-verified k3d, kubectl and helm into `.tools/bin`.
- **Resources:** at least 8 GB of memory for containers and 20 GB of free disk.
- **Free host ports:** 8080, 3000, 8090 and 5000. If one is taken, put an override in `.env.local`, for example `HOST_PORT_GRAFANA=3001`; scripts read that file and it isn't committed.

For live LLM mode, you also need [Ollama](https://ollama.com) on this machine (the demo doesn't run it inside the cluster). On Linux it must listen on all interfaces (`OLLAMA_HOST=0.0.0.0:11434`) so the cluster can reach it.

```bash
make tools                   # k3d, kubectl, helm into .tools/bin
make prereqs                 # mock mode checks
make prereqs LLM_MODE=live   # also checks the host Ollama and pinned model
```

## Quick start

```bash
make prefetch
make up            # LLM_MODE=mock by default
make demo SCENARIO=s0
make test
```
