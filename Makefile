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

export LLM_MODE

define not_yet
	@echo "make $@: not implemented yet (phase $(1)). See docs/REQUIREMENTS_STATUS.md." >&2; exit 2
endef

.PHONY: help prereqs prefetch up down reset status demo demo-all trip kill-reset \
        policy-set evidence evidence-verify test clean-room logs pin-check

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

prereqs: ## Check tool versions, Docker memory, disk, and (live) host Ollama
	@scripts/prereqs.sh $(LLM_MODE)

pin-check: ## Fail if upstream image digests drifted from versions.env
	@scripts/pin-digests.sh --check

prefetch: ## Pull every pinned image and model (P1)
	$(call not_yet,P1)

up: ## Create cluster, build images, install charts, bootstrap gateway (P1)
	$(call not_yet,P1)

down: ## Delete the cluster, keep the prefetch cache (P1)
	$(call not_yet,P1)

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

test: ## Run the acceptance suite in mock mode (P7)
	$(call not_yet,P7)

clean-room: ## Double clean rebuild proof (P7)
	$(call not_yet,P7)

logs: ## Tail one service's logs: make logs SVC=guard (P1)
	$(call not_yet,P1)
