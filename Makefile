# AI Gateway Security Demo — the only operator interface (REP-01).
# Targets marked "(Pn)" are implemented in that phase and fail clearly until then.

SHELL := bash
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

LLM_MODE ?= mock
SCENARIO ?=
SCOPE    ?=
REASON   ?=
CHECK    ?=
MODE     ?=
INCIDENT ?=
TRACE    ?=
DIR      ?=
SVC      ?=
PYTEST_ARGS ?=

export LLM_MODE HOST_BIND HOST_PORT_GATEWAY HOST_PORT_GRAFANA HOST_PORT_PANEL CONTAINER_RUNTIME

define not_yet
	@echo "make $@: not implemented yet (phase $(1)). See docs/REQUIREMENTS_STATUS.md." >&2; exit 2
endef

.PHONY: help tools prereqs prefetch up down reset status demo demo-all trip kill-reset \
        policy-set evidence evidence-verify test clean-room logs pin-check

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

tools: ## Install pinned k3d, kubectl and helm into .tools/bin (checksum-verified)
	@scripts/install-tools.sh

prereqs: ## Check runtime (Docker/Podman), tools, memory, disk, ports, (live) host Ollama
	@scripts/prereqs.sh $(LLM_MODE)

pin-check: ## Fail if upstream image digests drifted from versions.env
	@scripts/pin-digests.sh --check

prefetch: ## Pull every pinned chart, image and (LLM_MODE=live) the host Ollama model
	@scripts/prefetch.sh

up: ## Create cluster, build images, install charts, bootstrap gateway
	@scripts/up.sh

down: ## Delete the cluster and registry; keep the prefetch cache and secrets
	@scripts/down.sh

reset: ## Return to a clean demo state without rebuilding (P5)
	$(call not_yet,P5)

status: ## Pod health, kill-switch state, policy modes, alerts (P5)
	$(call not_yet,P5)

demo: ## Run one scenario: make demo SCENARIO=s3 (P4)
	$(call not_yet,P4)

demo-all: ## Run S0–S5 with resets between them (P5)
	$(call not_yet,P5)

trip: ## Manual kill: make trip SCOPE=global REASON="drill" (P5)
	$(call not_yet,P5)

kill-reset: ## Reset a kill: make kill-reset SCOPE=global REASON="..." (P5)
	$(call not_yet,P5)

policy-set: ## Change a guard check mode: make policy-set CHECK=... MODE=... (P3)
	$(call not_yet,P3)

evidence: ## Build an evidence bundle: make evidence INCIDENT=...|TRACE=... (P6)
	$(call not_yet,P6)

evidence-verify: ## Verify bundle hashes and hash chain: make evidence-verify DIR=... (P6)
	$(call not_yet,P6)

test: ## Run the acceptance suite in mock mode
	@cd tests && uv run --frozen pytest $(PYTEST_ARGS)

clean-room: ## Double clean rebuild proof (P7)
	$(call not_yet,P7)

logs: ## Tail one service's logs: make logs SVC=apisix|etcd|mock-llm|...
	@scripts/logs.sh "$(SVC)"
