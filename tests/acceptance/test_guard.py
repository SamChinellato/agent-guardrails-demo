"""Guard and attack-vector requirements (GRD, ATK, GW-05, GW-09) and the P3 definition of done."""

import json
import secrets
import subprocess
import time
from pathlib import Path

import httpx
import pytest
import yaml

from conftest import ROOT, poll

ATTACKS = yaml.safe_load((ROOT / "scenarios/fixtures/attacks.yaml").read_text())
LLM = "/llm/v1/chat/completions"
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def ask(gateway, key, text, traceparent=None):
    h = {"apikey": key, "X-Demo-Scenario": "smoke"}
    if traceparent:
        h["traceparent"] = traceparent
    return gateway.post(LLM, json={"model": "demo", "messages": [{"role": "user", "content": text}]}, headers=h)


def mcp(gateway, key, server, method, params=None):
    r = gateway.post(
        f"/mcp/{server}",
        headers={**MCP_HEADERS, "apikey": key},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    return r


def call(gateway, key, server, tool, arguments):
    return mcp(gateway, key, server, "tools/call", {"name": tool, "arguments": arguments}).json()


@pytest.fixture
def executor_open(forward):
    """A critical denial also trips the executor (GRD-06), so tests that use it start open."""
    ks = httpx.Client(base_url=forward("gateway", "killswitch", 8000), timeout=30,
                      headers={"Authorization": f"Bearer {__import__('conftest').secret('killswitch-operator-token')}"})
    guard = forward("ai", "guard", 8000)

    def release():
        if ks.get("/v1/status").json()["scopes"]["agent:executor"]:
            ks.post("/v1/reset", json={"scope": "agent:executor", "reason": "test", "actor": "test"}).raise_for_status()
        httpx.post(f"{guard}/v1/autotrip/reset")

    release()
    yield release
    release()


def policy_set(check, mode):
    subprocess.run([str(ROOT / "scripts/policy-set.sh"), check, mode], check=True, capture_output=True)


# ---------------------------------------------------------------- ATK-01 direct injection
def test_fixture_counts():
    assert len(ATTACKS["crude"]) >= 5 and len(ATTACKS["subtle"]) >= 5 and len(ATTACKS["benign"]) >= 10


# Direct LLM-route checks use the probe consumer: the same guards apply, and repeated blocks
# for a real agent would (correctly) trip it via GuardBlockSpike. S1 and S4 cover the agents.
@pytest.mark.parametrize("text", ATTACKS["crude"])
def test_crude_attacks_get_400_from_regex_guard(gateway, key, text):
    assert ask(gateway, key("probe"), text).status_code == 400


@pytest.mark.parametrize("text", ATTACKS["subtle"])
def test_subtle_attacks_get_403_from_injection_guard(gateway, key, text):
    r = ask(gateway, key("probe"), text)
    assert r.status_code == 403, r.text
    err = r.json()["error"]
    assert err["type"] == "prompt_injection_blocked" and err["decision_id"].startswith("dec-")


@pytest.mark.parametrize("text", ATTACKS["benign"])
def test_benign_prompts_pass(gateway, key, text):
    assert ask(gateway, key("probe"), text).status_code == 200


# ---------------------------------------------------------------- GRD-03 allowlist and tools/list
@pytest.mark.parametrize(
    "consumer,server,expected",
    [
        ("researcher", "docs", ["list_documents", "read_document"]),
        ("executor", "actions", ["send_email", "update_record"]),
        ("probe", "docs", []),
        ("probe", "actions", []),
    ],
)
def test_tools_list_filtered_per_agent(gateway, key, consumer, server, expected):
    tools = mcp(gateway, key(consumer), server, "tools/list").json()["result"]["tools"]
    assert sorted(t["name"] for t in tools) == expected


def test_mcp_routes_restricted_to_their_agents(gateway, key):
    assert mcp(gateway, key("researcher"), "actions", "tools/list").status_code == 403
    assert mcp(gateway, key("executor"), "docs", "tools/list").status_code == 403


def test_initialize_passes_through(gateway, key):
    r = mcp(gateway, key("researcher"), "docs", "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
    assert r.json()["result"]["serverInfo"]["name"] == "mcp-docs"


def test_export_customer_data_denied_to_all(gateway, key, executor_open):
    err = call(gateway, key("executor"), "actions", "export_customer_data", {})["error"]
    assert err["code"] == -32001 and err["data"]["decision_id"].startswith("dec-")


# ---------------------------------------------------------------- GRD-04 argument rules
def test_external_email_denied_internal_allowed(gateway, key, forward, executor_open):
    outbox = forward("ai", "mcp-actions", 8000)
    httpx.delete(f"{outbox}/outbox")
    ok = call(gateway, key("executor"), "actions", "send_email",
              {"to": "quality@demo.local", "subject": "CAR", "body": "please review"})
    assert ok["result"]["isError"] is False
    sub = call(gateway, key("executor"), "actions", "send_email",
               {"to": "quality@blueridge.demo.local", "subject": "CAR", "body": "please review"})
    assert sub["result"]["isError"] is False  # subdomains of demo.local are internal (D-034)
    err = call(gateway, key("executor"), "actions", "send_email",
               {"to": "audit@external-review.example", "subject": "export", "body": "data"})["error"]
    assert err["code"] == -32001 and err["data"]["check"] == "mcp.argument_rules"
    recipients = [e["to"] for e in httpx.get(f"{outbox}/outbox").json()]
    assert recipients == ["quality@demo.local", "quality@blueridge.demo.local"]  # nothing external


def test_update_record_limited_to_status_and_notes(gateway, key, executor_open):
    ok = call(gateway, key("executor"), "actions", "update_record", {"id": "supplier-02", "fields": {"status": "watch"}})
    assert ok["result"]["isError"] is False
    err = call(gateway, key("executor"), "actions", "update_record", {"id": "supplier-02", "fields": {"owner": "x"}})
    assert err["error"]["code"] == -32001


# ---------------------------------------------------------------- GRD-07 result scanning, ATK-02
def test_poisoned_document_quarantined_benign_documents_pass(gateway, key):
    bad = call(gateway, key("researcher"), "docs", "read_document", {"id": "supplier-07"})["result"]
    assert bad["isError"] is True
    assert bad["content"][0]["text"].startswith("Content quarantined by policy (decision dec-")
    # supplier-06 hides its request in business language and passes on purpose (D-044)
    for doc in ("supplier-01", "supplier-02", "supplier-03", "supplier-04", "supplier-05", "supplier-06"):
        good = call(gateway, key("researcher"), "docs", "read_document", {"id": doc})["result"]
        assert good["isError"] is False, doc


# ---------------------------------------------------------------- GRD-02 hot reload, monitor mode
def test_policy_hot_reload_within_10s_and_monitor_mode_flags(gateway, key):
    try:
        policy_set("mcp.tool_result_injection", "monitor")
        start = time.monotonic()
        poll(lambda: not call(gateway, key("researcher"), "docs", "read_document", {"id": "supplier-07"})["result"]["isError"],
             timeout=10, interval=0.5)
        print(f"policy change effective after {time.monotonic() - start:.1f}s")
    finally:
        policy_set("mcp.tool_result_injection", "enforce")
    poll(lambda: call(gateway, key("researcher"), "docs", "read_document", {"id": "supplier-07"})["result"]["isError"],
         timeout=10, interval=0.5)


# ---------------------------------------------------------------- GRD-05 decision records, OBS-02
def test_decision_record_spans_and_metric(gateway, key, forward):
    trace_id = secrets.token_hex(16)
    r = ask(gateway, key("probe"), ATTACKS["subtle"][0], traceparent=f"00-{trace_id}-{secrets.token_hex(8)}-01")
    decision_id = r.json()["error"]["decision_id"]

    loki = httpx.Client(base_url=forward("observability", "loki", 3100), timeout=10)
    query = f'{{service_name="guard"}} | json | decision_id="{decision_id}"'

    def record():
        res = loki.get("/loki/api/v1/query_range",
                       params={"query": query, "start": str(int((time.time() - 300) * 1e9))}).json()["data"]["result"]
        return res[0]["stream"] if res else None

    poll(lambda: record() is not None, timeout=15)
    rec = record()
    for field in ("decision_id", "trace_id", "span_id", "agent", "check", "mode", "verdict", "score", "rule_id",
                  "reason", "route", "policy_version", "content_sha256", "excerpt"):
        assert rec.get(field) not in (None, ""), f"decision record missing {field}"
    assert rec["trace_id"] == trace_id and rec["verdict"] == "block" and rec["agent"] == "probe"

    # The guard's spans are children of the gateway span in the same trace (OBS-02)
    tempo = httpx.Client(base_url=forward("observability", "tempo", 3200), timeout=10)

    def spans():
        t = tempo.get(f"/api/traces/{trace_id}")
        if t.status_code != 200:
            return {}
        out = {}
        for b in t.json()["batches"]:
            svc = next(a["value"]["stringValue"] for a in b["resource"]["attributes"] if a["key"] == "service.name")
            out.setdefault(svc, []).extend(s for ss in b["scopeSpans"] for s in ss["spans"])
        return out

    poll(lambda: {"apisix", "guard"} <= spans().keys(), timeout=15)
    s = spans()
    apisix_ids = {x["spanId"] for x in s["apisix"]}
    assert any(x.get("parentSpanId") in apisix_ids for x in s["guard"])
    events = [e["name"] for x in s["guard"] for e in x.get("events", [])]
    assert "guard.decision" in events

    prom = httpx.Client(base_url=forward("observability", "kps-prometheus", 9090), timeout=10)
    q = 'guard_decisions_total{agent="probe",check="llm.user_prompt_injection",verdict="block"} > 0'
    poll(lambda: bool(prom.get("/api/v1/query", params={"query": q}).json()["data"]["result"]), timeout=15)


def test_guard_policy_endpoint_reports_version(forward):
    p = httpx.get(f"{forward('ai', 'guard', 8000)}/v1/policy").json()
    assert len(p["version"]) == 12 and "checks" in p["policy"]
