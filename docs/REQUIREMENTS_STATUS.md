# Requirements status

Every requirement ID in `SPEC.md`, with status `TODO`, `DONE` or `DEVIATION` and the test that proves it. Updated at the end of each phase.

**Current phase:** P0 (scaffold and pin).

| ID | Requirement | Status | Test | Notes |
| --- | --- | --- | --- | --- |
| GW-01 | Identity | TODO | — |  |
| GW-02 | Routes | TODO | — |  |
| GW-03 | Kill-switch hooks | TODO | — |  |
| GW-04 | LLM route | TODO | — |  |
| GW-05 | MCP routes | TODO | — |  |
| GW-06 | Gateway telemetry | TODO | — |  |
| GW-07 | Admin API | TODO | — |  |
| GW-08 | Config as code | TODO | — |  |
| GW-09 | Custom plugin `ai-injection-guard` | TODO | — |  |
| GRD-01 | Classifier | TODO | — |  |
| GRD-02 | Policy | TODO | — |  |
| GRD-03 | Tool allowlist | TODO | — |  |
| GRD-04 | Argument rules | TODO | — |  |
| GRD-05 | Decision records | TODO | — |  |
| GRD-06 | Auto-trip | TODO | — |  |
| GRD-07 | MCP proxy | TODO | — |  |
| GRD-08 | Readiness | TODO | — |  |
| ATK-01 | Direct injection fixtures | TODO | — |  |
| ATK-02 | Poisoned document | TODO | — |  |
| ATK-03 | Cascade | TODO | — |  |
| ATK-04 | Runaway agent (stretch) | TODO | — |  |
| OBS-01 | One trace | TODO | — |  |
| OBS-02 | Gateway spans | TODO | — |  |
| OBS-03 | Metrics | TODO | — |  |
| OBS-04 | Logs | TODO | — |  |
| OBS-05 | Dashboards | TODO | — |  |
| OBS-06 | Freshness | TODO | — |  |
| OBS-07 | Sampling and retention | TODO | — |  |
| KS-01 | Scopes | TODO | — |  |
| KS-02 | API | TODO | — |  |
| KS-03 | Idempotence | TODO | — |  |
| KS-04 | Verification | TODO | — |  |
| KS-05 | Time bounds | TODO | — |  |
| KS-06 | Responses | TODO | — |  |
| KS-07 | Audit log | TODO | — |  |
| KS-08 | Control panel | TODO | — |  |
| KS-09 | Auth | TODO | — |  |
| KS-10 | Alert rules | TODO | — |  |
| KS-11 | In-flight requests | TODO | — |  |
| KS-12 | Incidents | TODO | — |  |
| EVD-01 | Command | TODO | — |  |
| EVD-02 | Bundle contents | TODO | — |  |
| EVD-03 | Report content | TODO | — |  |
| EVD-04 | Redaction | TODO | — |  |
| EVD-05 | Determinism | TODO | — |  |
| EVD-06 | Verification | TODO | — |  |
| EVD-07 | Framework placeholder | TODO | — |  |
| REP-01 | Single entry point | TODO | — |  |
| REP-02 | Pinned versions | TODO | `make pin-check` | Images, charts, k3s, model and classifier revision pinned in `versions.env` (P0). Python `uv.lock` files arrive with each service (P1–P4). |
| REP-03 | Built images | TODO | — |  |
| REP-04 | Multi-arch | TODO | — | Pins are multi-arch index digests; classifier arm64 load test pending (D-005). |
| REP-05 | Prerequisite check | DONE | `make prereqs` | Live mode also checks the host Ollama (D-002). |
| REP-06 | Prefetch | TODO | — |  |
| REP-07 | Idempotence | TODO | — |  |
| REP-08 | Determinism | TODO | — | Live mode uses the host Ollama (D-002). |
| REP-09 | Secrets | TODO | — |  |
| REP-10 | Clean-room proof | TODO | — |  |
| REP-11 | CI | TODO | — |  |
| NFR-01 | Cold `make up` after prefetch, mock mode | TODO | — |  |
| NFR-02 | `make test` duration, mock mode | TODO | — |  |
| NFR-03 | Gateway overhead added by guard on the LLM route, p95 | TODO | — |  |
| NFR-04 | Host resources, live mode | TODO | — | Ollama runs on the host (D-002); prereqs warns under 16 GB host RAM. |
| NFR-05 | Ollama model | TODO | P0 smoke test (D-006) | `qwen3:4b` pinned; tool calling verified; thinking to be disabled in P4. |
| NFR-06 | Every service exposes `/healthz`, `/readyz` and `/metrics` | TODO | — |  |
| NFR-07 | Resource requests and limits on every workload | TODO | — |  |
| NFR-08 | No data leaves the machine at runtime | TODO | — | Egress exception only for `gateway` → host:11434 (D-002). |
