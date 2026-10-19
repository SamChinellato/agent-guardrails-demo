"""Observability requirements (OBS, GW-06) and the P2 definition of done: a request through
the gateway shows up as a trace in Tempo, linked logs in Loki and metrics in Prometheus,
all within 15 s (OBS-06)."""

import secrets
import time

import httpx
import pytest

from conftest import poll, secret, setting

FRESHNESS_S = 15  # OBS-06
GRAFANA = f"http://127.0.0.1:{setting('HOST_PORT_GRAFANA', '3000')}"
MARKER = "obs-marker-" + secrets.token_hex(4)  # request content that must never be logged


@pytest.fixture(scope="module")
def traced_request(gateway, key):
    """One LLM call with a known W3C trace id; returns (trace_id, sent_at)."""
    trace_id = secrets.token_hex(16)
    r = gateway.post(
        "/llm/v1/chat/completions",
        json={"model": "demo", "messages": [{"role": "user", "content": f"hello {MARKER}"}]},
        headers={
            "apikey": key("researcher"),
            "X-Demo-Scenario": "smoke",
            "traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01",
        },
    )
    assert r.status_code == 200, r.text
    return trace_id, time.time()


@pytest.fixture(scope="module")
def tempo(forward):
    return httpx.Client(base_url=forward("observability", "tempo", 3200), timeout=10)


@pytest.fixture(scope="module")
def loki(forward):
    return httpx.Client(base_url=forward("observability", "loki", 3100), timeout=10)


@pytest.fixture(scope="module")
def prometheus(forward):
    return httpx.Client(base_url=forward("observability", "kps-prometheus", 9090), timeout=10)


def trace_services(tempo, trace_id) -> dict[str, list[dict]]:
    r = tempo.get(f"/api/traces/{trace_id}")
    if r.status_code != 200:
        return {}
    out: dict[str, list[dict]] = {}
    for batch in r.json().get("batches", []):
        svc = next(a["value"]["stringValue"] for a in batch["resource"]["attributes"] if a["key"] == "service.name")
        out.setdefault(svc, []).extend(s for ss in batch["scopeSpans"] for s in ss["spans"])
    return out


def loki_streams(loki, query, since_s=300) -> list[dict]:
    r = loki.get(
        "/loki/api/v1/query_range",
        params={"query": query, "start": str(int((time.time() - since_s) * 1e9)), "limit": 500},
    )
    return r.json()["data"]["result"] if r.status_code == 200 else []


# ---------------------------------------------------------------- traces (OBS-01/02/06/07)
def test_trace_reaches_tempo_within_15s_with_gateway_as_parent(traced_request, tempo):
    trace_id, sent = traced_request
    poll(lambda: {"apisix", "mock-llm"} <= trace_services(tempo, trace_id).keys(), timeout=FRESHNESS_S)
    spans = trace_services(tempo, trace_id)
    gateway_ids = {s["spanId"] for s in spans["apisix"]}
    # mock-llm's server span is a child of the APISIX span (OBS-02)
    assert any(s.get("parentSpanId") in gateway_ids for s in spans["mock-llm"])
    print(f"trace visible {time.time() - sent:.1f}s after the request")


# ---------------------------------------------------------------- logs (OBS-04, GW-06)
def test_logs_for_the_trace_reach_loki_within_15s(traced_request, loki):
    trace_id, _ = traced_request
    query = f'{{service_name=~".+"}} | json | trace_id="{trace_id}"'
    poll(
        lambda: {"apisix", "mock-llm"} <= {s["stream"]["service_name"] for s in loki_streams(loki, query)},
        timeout=FRESHNESS_S,
    )


def test_gateway_access_log_has_required_fields_and_no_bodies(traced_request, loki):
    trace_id, _ = traced_request
    query = f'{{service_name="apisix"}} | json | trace_id="{trace_id}"'
    poll(lambda: bool(loki_streams(loki, query)), timeout=FRESHNESS_S)
    stream = loki_streams(loki, query)[0]
    fields = stream["stream"]
    for f in ("trace_id", "span_id", "consumer", "route_id", "status", "latency_s", "upstream"):
        assert fields.get(f), f"access log is missing {f}"
    assert fields["consumer"] == "researcher" and fields["route_id"] == "llm"
    # GW-06: request and response bodies are never logged
    everything = loki_streams(loki, f'{{service_name="apisix"}} |= "{MARKER}"')
    assert everything == []


def test_service_logs_are_json_with_trace_context(traced_request, loki):
    trace_id, _ = traced_request
    query = f'{{service_name="mock-llm"}} | trace_id="{trace_id}"'
    poll(lambda: bool(loki_streams(loki, query)), timeout=FRESHNESS_S)
    assert loki_streams(loki, query)[0]["stream"].get("span_id")


# ---------------------------------------------------------------- metrics (OBS-03)
def test_gateway_metrics_by_route_and_consumer(traced_request, prometheus):
    def has_series():
        r = prometheus.get(
            "/api/v1/query", params={"query": 'apisix_http_status{route="llm", consumer="researcher", code="200"}'}
        )
        return bool(r.json()["data"]["result"])

    poll(has_series, timeout=FRESHNESS_S)
    latency = prometheus.get("/api/v1/query", params={"query": 'apisix_http_latency_count{route="llm"}'}).json()
    assert latency["data"]["result"]


def test_gateway_measures_llm_tokens_per_agent(traced_request, prometheus):
    r = prometheus.get("/api/v1/query", params={"query": 'sum by (consumer) (apisix_llm_prompt_tokens)'}).json()
    assert "researcher" in {s["metric"].get("consumer") for s in r["data"]["result"]}


def test_scrape_targets_healthy(prometheus):
    targets = prometheus.get("/api/v1/targets").json()["data"]["activeTargets"]
    jobs = {t["labels"]["job"]: t["health"] for t in targets}
    for job in ("apisix-prometheus-metrics", "mock-llm", "otel-collector"):
        assert jobs.get(job) == "up", f"{job} target not up: {jobs}"


# ---------------------------------------------------------------- Grafana (OBS-04/05)
@pytest.fixture(scope="module")
def grafana():
    return httpx.Client(base_url=GRAFANA, auth=("admin", secret("grafana-admin-password")), timeout=10)


@pytest.mark.parametrize("uid", ["ai-gateway-overview", "agent-behaviour", "incident"])
def test_dashboards_provisioned_with_5s_refresh(grafana, uid):
    d = grafana.get(f"/api/dashboards/uid/{uid}")
    assert d.status_code == 200, f"dashboard {uid} not provisioned"
    assert d.json()["dashboard"]["refresh"] == "5s"


def test_datasources_link_logs_and_traces(grafana):
    loki = grafana.get("/api/datasources/uid/loki").json()
    tempo = grafana.get("/api/datasources/uid/tempo").json()
    assert any(f["datasourceUid"] == "tempo" for f in loki["jsonData"]["derivedFields"])  # log -> trace
    assert tempo["jsonData"]["tracesToLogsV2"]["datasourceUid"] == "loki"  # trace -> logs
    for uid in ("prometheus", "loki", "tempo"):
        assert grafana.get(f"/api/datasources/uid/{uid}/health").json()["status"] == "OK"
