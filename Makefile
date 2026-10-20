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
SCRIPTED ?=
PYTEST_ARGS ?=

export LLM_MODE HOST_BIND HOST_PORT_GATEWAY HOST_PORT_GRAFANA HOST_PORT_PANEL CONTAINER_RUNTIME

define not_yet
	@echo "make $@: not implemented yet (phase $(1)). See docs/REQUIREMENTS_STATUS.md." >&2; exit 2
endef

.PHONY: help tools admin-ui prereqs prefetch up down reset status demo demo-all trip kill-reset \
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

reset: ## Clean demo state without rebuilding: kill switches, policy, outbox, audit log (REP-07)
	@cd services && uv run --frozen --package cli demo reset

status: ## Pod health, kill-switch state, policy modes, firing alerts
	@cd services && uv run --frozen --package cli demo status

demo: ## Run one scenario: make demo SCENARIO=s3 [SCRIPTED=1 in live mode]
	@test -n "$(SCENARIO)" || { echo "usage: make demo SCENARIO=s0|s1|s2|s3|s4|s5|s6" >&2; exit 2; }
	@cd services && uv run --frozen --package cli demo run "$(SCENARIO)" $(if $(SCRIPTED),--scripted,)

demo-all: ## Run S0–S5 with a reset after each
	@cd services && uv run --frozen --package cli demo all

trip: ## Manual kill: make trip SCOPE=global|tools|agent:researcher|agent:executor REASON="drill"
	@test -n "$(SCOPE)" -a -n "$(REASON)" || { echo 'usage: make trip SCOPE=... REASON="..."' >&2; exit 2; }
	@cd services && uv run --frozen --package cli demo trip "$(SCOPE)" "$(REASON)"

kill-reset: ## Reset a kill: make kill-reset SCOPE=... REASON="..." (global asks for confirmation)
	@test -n "$(SCOPE)" -a -n "$(REASON)" || { echo 'usage: make kill-reset SCOPE=... REASON="..."' >&2; exit 2; }
	@cd services && uv run --frozen --package cli demo kill-reset "$(SCOPE)" "$(REASON)"

policy-set: ## Change a guard check mode: make policy-set CHECK=... MODE=enforce|monitor|off
	@scripts/policy-set.sh "$(CHECK)" "$(MODE)"

evidence: ## Build an evidence bundle: make evidence INCIDENT=...|TRACE=...
	@cd services && uv run --frozen --package cli demo evidence $(if $(INCIDENT),--incident "$(INCIDENT)",) $(if $(TRACE),--trace "$(TRACE)",)

evidence-verify: ## Verify bundle hashes and the audit hash chain: make evidence-verify DIR=evidence/<id>
	@test -n "$(DIR)" || { echo "usage: make evidence-verify DIR=evidence/<id>" >&2; exit 2; }
	@cd services && uv run --frozen --package cli demo evidence-verify "$(abspath $(DIR))"

test: ## Run the acceptance suite in mock mode
	@cd tests && uv run --frozen pytest $(PYTEST_ARGS)

clean-room: ## Double clean rebuild proof (P7)
	$(call not_yet,P7)

admin-ui: ## Open the APISIX admin UI on localhost:9180/ui via port-forward (read-only key shown)
	@scripts/admin-ui.sh

logs: ## Tail one service's logs: make logs SVC=apisix|etcd|mock-llm|...
	@scripts/logs.sh "$(SVC)"
