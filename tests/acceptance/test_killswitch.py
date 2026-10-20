"""Kill switch and incident response (KS), S3-S6 and DEMO-03: the P5 definition of done."""

import json
import subprocess
import sys
import time

import httpx
import pytest

from conftest import ROOT, poll, secret

sys.path.insert(0, str(ROOT / "services/killswitch/src"))
from killswitch.audit import verify_chain  # noqa: E402

LLM = "/llm/v1/chat/completions"
CHAT = {"model": "demo", "messages": [{"role": "user", "content": "hello"}]}


@pytest.fixture(scope="module")
def ks(forward):
    base = forward("gateway", "killswitch", 8000)

    def client(token_name="killswitch-operator-token"):
        return httpx.Client(base_url=base, timeout=30, headers={"Authorization": f"Bearer {secret(token_name)}"})

    return client


@pytest.fixture(autouse=True)
def all_open(ks, forward):
    """Every test starts and ends with all scopes open and the guard's trip memory clear."""
    guard = forward("ai", "guard", 8000)

    def release():
        op = ks()
        for scope, on in op.get("/v1/status").json()["scopes"].items():
            if on:
                op.post("/v1/reset", json={"scope": scope, "reason": "test cleanup", "actor": "test",
                                           "confirm": "RESET GLOBAL"}).raise_for_status()
        httpx.post(f"{guard}/v1/autotrip/reset")

    release()
    yield
    release()


def llm(gateway, consumer):
    return gateway.post(LLM, json=CHAT, headers={"apikey": secret(f"consumer-{consumer}"), "X-Demo-Scenario": "smoke"})


def mcp(gateway, consumer, server):
    return gateway.post(f"/mcp/{server}", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                        headers={"apikey": secret(f"consumer-{consumer}"), "accept": "application/json, text/event-stream"})


def agent_route(gateway):
    return gateway.post("/agents/researcher/task", json={"instruction": "hi", "scenario_id": "s1:scripted"},
                        headers={"apikey": secret("consumer-demo-user"), "X-Demo-Scenario": "s1:scripted"}, timeout=60)


def trip(ks, scope, **kw):
    r = ks(kw.pop("token", "killswitch-operator-token")).post(
        "/v1/trip", json={"scope": scope, "reason": "test", "actor": "test", "source": "manual", **kw})
    return r


# ---------------------------------------------------------------- KS-01 / KS-06 scopes and responses
def test_global_kill_stops_llm_tools_and_agents_with_typed_503s(gateway, ks):
    t0 = time.monotonic()
    r = trip(ks, "global")
    assert r.status_code == 200 and r.json()["verified"] is True
    poll(lambda: llm(gateway, "researcher").status_code == 503, timeout=2)  # KS-05 manual <= 2 s
    assert time.monotonic() - t0 <= 2.5
    assert llm(gateway, "researcher").json()["error"]["type"] == "kill_switch_engaged"
    m = mcp(gateway, "researcher", "docs")
    assert m.status_code == 503 and m.json()["error"]["message"].startswith("kill_switch_engaged")  # JSON-RPC error
    a = agent_route(gateway)
    assert a.status_code == 503 and a.json()["error"]["type"] == "kill_switch_engaged"
    assert gateway.post(LLM, json=CHAT).status_code == 503  # before authentication (D-015)


def test_global_reset_needs_confirmation_and_restores_service(gateway, ks):
    trip(ks, "global").raise_for_status()
    op = ks()
    r = op.post("/v1/reset", json={"scope": "global", "reason": "test", "actor": "test"})
    assert r.status_code == 400
    r = op.post("/v1/reset", json={"scope": "global", "reason": "test", "actor": "test", "confirm": "RESET GLOBAL"})
    assert r.status_code == 200 and r.json()["verified"] is True
    poll(lambda: llm(gateway, "researcher").status_code == 200, timeout=2)


def test_tools_scope_stops_only_mcp(gateway, ks):
    trip(ks, "tools").raise_for_status()
    poll(lambda: mcp(gateway, "researcher", "docs").status_code == 503, timeout=2)
    assert llm(gateway, "researcher").status_code == 200


def test_tools_stays_tripped_when_global_is_reset(gateway, ks):
    trip(ks, "tools").raise_for_status()
    trip(ks, "global").raise_for_status()
    ks().post("/v1/reset", json={"scope": "global", "reason": "test", "actor": "test", "confirm": "RESET GLOBAL"})
    assert ks().get("/v1/status").json()["scopes"]["tools"] is True
    poll(lambda: llm(gateway, "researcher").status_code == 200, timeout=2)
    assert mcp(gateway, "researcher", "docs").status_code == 503


def test_agent_scope_stops_only_that_agent(gateway, ks):
    r = trip(ks, "agent:researcher")
    assert r.json()["verified"] is True
    poll(lambda: llm(gateway, "researcher").status_code == 503, timeout=2)
    err = llm(gateway, "researcher").json()["error"]
    assert err["type"] == "kill_switch_engaged" and err["incident_id"] == r.json()["incident_id"]
    assert mcp(gateway, "researcher", "docs").status_code == 503
    assert llm(gateway, "executor").status_code == 200


# ---------------------------------------------------------------- KS-02 / KS-09 auth
def test_tokens_are_scoped(ks):
    anon = httpx.post(str(ks().base_url) + "/v1/trip", json={"scope": "global", "reason": "x"})
    assert anon.status_code == 401
    assert trip(ks, "global", token="killswitch-guard-token").status_code == 403
    assert trip(ks, "agent:executor", token="killswitch-guard-token").status_code == 200
    guard_reset = ks("killswitch-guard-token").post("/v1/reset", json={"scope": "agent:executor", "reason": "x"})
    assert guard_reset.status_code == 403
    assert trip(ks, "agent:executor", token="killswitch-alertmanager-token").status_code == 403
    assert ks("killswitch-guard-token").get("/v1/status").status_code == 401


# ---------------------------------------------------------------- KS-03 / KS-04 / KS-12 / KS-07
def test_trip_is_idempotent_verified_and_joins_incident(ks):
    first = trip(ks, "agent:executor").json()
    again = trip(ks, "agent:executor").json()
    assert first["no_change"] is False and again["no_change"] is True
    assert first["verified"] and again["verified"]
    assert again["incident_id"] == first["incident_id"]  # same target within 10 min (KS-12)


def test_status_is_read_live_from_apisix(ks, admin):
    trip(ks, "tools").raise_for_status()
    pc = admin.get("/plugin_configs/pc-mcp")
    assert pc["plugins"]["fault-injection"]["_meta"]["disable"] is False
    assert pc["labels"]["killswitch.tools"] == "on"
    assert ks().get("/v1/status").json()["scopes"]["tools"] is True


def test_audit_log_is_hash_chained_and_tamper_evident(ks):
    trip(ks, "agent:executor").raise_for_status()
    assert ks().get("/v1/audit/verify").json()["valid"] is True
    lines = ks().get("/v1/audit/raw").text.splitlines()
    ok, _ = verify_chain(lines)
    assert ok
    events = [json.loads(line) for line in lines]
    for f in ("event_id", "ts", "type", "scope", "target", "reason", "actor", "source", "incident_id",
              "trace_ids", "decision_id", "apisix_changes", "verified", "latency_ms", "prev_hash", "hash"):
        assert f in events[-1], f
    tampered = list(lines)
    e = json.loads(tampered[-1])
    e["reason"] = "nothing to see"
    tampered[-1] = json.dumps(e, sort_keys=True)
    assert verify_chain(tampered)[0] is False


def test_killswitch_metrics(ks, forward):
    trip(ks, "agent:executor").raise_for_status()
    prom = httpx.Client(base_url=forward("observability", "kps-prometheus", 9090), timeout=10)
    q = 'killswitch_active{scope="agent:executor"} == 1'
    poll(lambda: bool(prom.get("/api/v1/query", params={"query": q}).json()["data"]["result"]), timeout=20)
    assert prom.get("/api/v1/query", params={"query": "killswitch_events_total"}).json()["data"]["result"]


def test_control_panel_needs_operator_token(gateway):
    panel = f"http://127.0.0.1:{__import__('conftest').setting('HOST_PORT_PANEL', '8090')}"
    assert "Operator token" in httpx.get(panel + "/").text
    bad = httpx.post(panel + "/login", data={"token": "wrong"})
    assert bad.status_code == 401
    ok = httpx.post(panel + "/login", data={"token": secret("killswitch-operator-token")}, follow_redirects=True)
    assert ok.status_code == 200 and "Researcher agent" in ok.text


# ---------------------------------------------------------------- scenarios
def demo(*args, timeout=400):
    r = subprocess.run(["make", "-s", "demo", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def task(gateway, scenario, instruction, **extra):
    r = gateway.post("/agents/researcher/task", timeout=600,
                     json={"instruction": instruction, "scenario_id": f"{scenario}:scripted", **extra},
                     headers={"apikey": secret("consumer-demo-user"), "X-Demo-Scenario": f"{scenario}:scripted"})
    assert r.status_code == 200, r.text
    return r.json()


def test_s3_guard_auto_trips_executor_within_3s(gateway, ks, forward):
    from test_scenarios import set_policy  # same helper as the P4 tests

    guard = forward("ai", "guard", 8000)
    want = set_policy(mcp__tool_result_injection="monitor", llm__tool_message_injection="monitor")
    poll(lambda: httpx.get(f"{guard}/v1/policy").json()["version"] == want, timeout=20, interval=0.5)
    try:
        httpx.delete(f"{forward('ai', 'mcp-actions', 8000)}/outbox")
        r = task(gateway, "s3", "Review the Meridian Data Services document.")
        executor = [e for e in r["events"] if e.get("agent") == "executor"]
        assert ("tool", "actions__export_customer_data", "denied") in [(e["type"], e.get("tool"), e.get("outcome")) for e in executor]
        assert any(e["type"] == "halted" for e in executor)  # its next call met the kill switch
        trips = [e for e in ks().get("/v1/events").json()["events"] if e["type"] == "trip" and e["source"] == "guard"]
        last = trips[-1]
        assert last["scope"] == "agent:executor" and last["decision_id"].startswith("dec-") and last["trace_ids"]
        assert last["latency_ms"] <= 3000  # KS-05 guard auto-trip
        outbox = httpx.get(f"{forward('ai', 'mcp-actions', 8000)}/outbox").json()
        assert all(not e["to"].endswith("external-review.example") for e in outbox)
    finally:
        want = set_policy()
        poll(lambda: httpx.get(f"{guard}/v1/policy").json()["version"] == want, timeout=20, interval=0.5)


def test_s4_guard_block_spike_trips_researcher_within_60s():
    out = demo("SCENARIO=s4")
    assert "tripped by an alert: GuardBlockSpike" in out and "executor is unaffected: LLM call answered 200" in out


def test_s5_manual_global_kill_mid_task_and_reset():
    out = demo("SCENARIO=s5")
    assert "halted_by_kill_switch" in out and "operator reset global: verified" in out and "service restored: researcher completed" in out


def test_s6_rogue_agent_stopped_by_anomaly_alert():
    out = demo("SCENARIO=s6-rogue")
    assert "halted_by_kill_switch" in out and "AgentToolCallSpike" in out


def test_make_reset_under_2_minutes():
    t0 = time.monotonic()
    r = subprocess.run(["make", "-s", "reset"], cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr
    assert time.monotonic() - t0 < 120  # REP-07
