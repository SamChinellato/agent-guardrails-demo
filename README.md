# AI Gateway Security Demo

Apache APISIX as an AI gateway between two cooperating agents and their LLM and MCP tools, on a local k3d cluster. It shows four things: prompt-injection attacks, observability, a kill switch, and a tamper-evident evidence bundle. The full design is in [`SPEC.md`](SPEC.md). Decisions and deviations are in [`docs/DECISIONS.md`](docs/DECISIONS.md), and progress is in [`docs/REQUIREMENTS_STATUS.md`](docs/REQUIREMENTS_STATUS.md).

> **Status: phase P0 (scaffold and pin).** Only `make prereqs` and `make pin-check` work so far. Other targets exit with the phase that implements them.

## Prerequisites

docker, k3d ≥ 5.9, kubectl, helm, uv, jq, make and curl. Docker needs at least 8 GB of memory, and there must be at least 20 GB of free disk. Your user must be able to reach the Docker daemon.

For live LLM mode, you also need [Ollama](https://ollama.com) on this machine (the demo doesn't run it inside the cluster). On Linux it must listen on all interfaces (`OLLAMA_HOST=0.0.0.0:11434`) so the cluster can reach it.

```bash
make prereqs                 # mock mode checks
make prereqs LLM_MODE=live   # also checks the host Ollama and pinned model
```

## Quick start (from P1 onward)

```bash
make prefetch
make up            # LLM_MODE=mock by default
make demo SCENARIO=s0
make test
```
