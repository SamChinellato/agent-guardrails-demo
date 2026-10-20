"""Demo runner (make demo / demo-all / trip / kill-reset / reset / status).

A scenario first resets its own preconditions (kill switches open, guard memory clear,
its policy modes), then talks to the gateway exactly as an operator would: demo-user ->
APISIX -> researcher (F1). Output is a short narration plus a trace id per run.
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse

import httpx
import yaml

from demo_cli import ops

ROOT = ops.ROOT


def setting(name: str, default: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    f = ROOT / ".env.local"
    if f.exists():
        for line in f.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return default


GATEWAY = f"http://127.0.0.1:{setting('HOST_PORT_GATEWAY', '8080')}"
GRAFANA = f"http://localhost:{setting('HOST_PORT_GRAFANA', '3000')}"
BOLD, DIM, RED, GREEN, BLUE, RESET = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[34m", "\033[0m"


def say(text: str = "", colour: str = "") -> None:
    print(f"{colour}{text}{RESET}" if colour else text, flush=True)


def note(text: str) -> None:
    say(text, DIM)


def trace_url(trace_id: str) -> str:
    panes = {"a": {"datasource": "tempo", "queries": [{"refId": "A", "queryType": "traceql", "query": trace_id}],
                   "range": {"from": "now-1h", "to": "now"}}}
    return f"{GRAFANA}/explore?schemaVersion=1&panes={urllib.parse.quote(json.dumps(panes))}"


def find_scenario(sid: str) -> dict:
    for f in sorted((ROOT / "scenarios").glob("s*.yaml")):
        s = yaml.safe_load(f.read_text())
        if s["id"] == sid or f.stem == sid:
            return s
    sys.exit(f"no scenario {sid!r} in scenarios/")


def demo_key() -> str:
    return ops.secret("consumer-demo-user")


def header(sid: str, scripted: bool) -> str:
    return f"{sid}:scripted" if scripted else sid


# ---------------------------------------------------------------------------- output
def show_events(result: dict, first_agent: str) -> None:
    for e in result["events"]:
        who = e.get("agent", first_agent)
        if e["type"] == "tool":
            colour = RED if e["outcome"] in ("denied", "quarantined", "halted") else ""
            extra = f"  {DIM}{e['decision_id']}{RESET}" if e.get("decision_id") else ""
            say(f"  {who:<11}{e['tool']:<34} {e['outcome']}{extra}", colour)
        elif e["type"] == "agent":
            say(f"  {who:<11}finished: {e['status']}", GREEN if e["status"] == "completed" else RED)
        elif e["type"] in ("blocked", "halted"):
            say(f"  {who:<11}{e['type']}: {e.get('reason') or e.get('detail')}", RED)


def show_result(agent: str, result: dict, elapsed: float) -> None:
    colour = GREEN if result["status"] == "completed" else RED
    say(f"\n{agent} ({result['status']}, {elapsed:.1f}s): {result['reply']}", colour)
    say(f"Trace {result['trace_id']}  {DIM}{trace_url(result['trace_id'])}{RESET}", BLUE)


def show_kills(since: float) -> list[dict]:
    with ops.Forward("gateway", "killswitch", 8000) as ks_url:
        events = [e for e in ops.KillSwitchApi(ks_url).events(50) if e["ts"] >= since and e["type"] == "trip"]
    for e in events:
        who = {"guard": "the guard", "alertmanager": "an alert", "manual": e["actor"]}.get(e["source"], e["source"])
        say(f"  kill switch {e['scope']} tripped by {who}: {e['reason']}  {DIM}{e['incident_id']}, "
            f"verified in {e['latency_ms']:.0f} ms{RESET}", RED)
    return events


# ---------------------------------------------------------------------------- requests
def task(agent: str, instruction: str, scenario: str, max_steps: int | None = None, chat: bool = False) -> tuple[dict, float]:
    body = {"scenario_id": scenario}
    if max_steps:
        body["max_steps"] = max_steps
    if chat:
        body["messages"] = [{"role": "user", "content": instruction}]
    else:
        body["instruction"] = instruction
    t0 = time.monotonic()
    r = httpx.post(f"{GATEWAY}/agents/{agent}/{'chat' if chat else 'task'}", json=body, timeout=900,
                   headers={"apikey": demo_key(), "X-Demo-Scenario": scenario})
    elapsed = time.monotonic() - t0
    if r.status_code == 503:
        return {"status": "halted_by_kill_switch", "reply": "The gateway refused the request: kill switch engaged.",
                "trace_id": "", "events": []}, elapsed
    if r.status_code != 200:
        sys.exit(f"gateway answered {r.status_code}: {r.text[:200]}")
    return r.json(), elapsed


def wait_tripped(scope: str, timeout: float) -> float | None:
    """Seconds until scope is tripped, or None."""
    t0 = time.monotonic()
    with ops.Forward("gateway", "killswitch", 8000) as ks_url:
        ks = ops.KillSwitchApi(ks_url)
        while time.monotonic() - t0 < timeout:
            if ks.status()["scopes"].get(scope):
                return time.monotonic() - t0
            time.sleep(1)
    return None


# ---------------------------------------------------------------------------- scenario kinds
def run_task(s: dict, scripted: bool) -> None:
    req = s["request"]
    since = time.time()
    say(f"\nYou → {req['agent']}: {req['instruction']}", BOLD)
    result, elapsed = task(req["agent"], req["instruction"], header(s["id"], scripted), req.get("max_steps"))
    show_events(result, req["agent"])
    show_result(req["agent"], result, elapsed)
    if s.get("expect_trip"):
        time.sleep(1)
        kills = show_kills(since)
        if kills:
            say(f"\nEvidence: make evidence INCIDENT={kills[0]['incident_id']}", DIM)


def run_attacks(s: dict) -> None:
    attacks = yaml.safe_load((ROOT / "scenarios" / s["attacks"]).read_text())
    for kind in ("crude", "subtle", "benign"):
        say(f"\n{kind.capitalize()} prompts", BOLD)
        for text in attacks[kind]:
            r = httpx.post(f"{GATEWAY}/agents/researcher/chat", timeout=300,
                           json={"messages": [{"role": "user", "content": text}], "scenario_id": "s1:scripted"},
                           headers={"apikey": demo_key(), "X-Demo-Scenario": "s1:scripted"})
            err = (r.json().get("error") or {}) if r.headers.get("content-type", "").startswith("application/json") else {}
            verdict = {200: ("passed to the agent", GREEN), 400: ("stopped: known attack pattern", RED),
                       403: (f"stopped: prompt injection  {DIM}{err.get('decision_id', '')}{RESET}", RED)}.get(
                r.status_code, (f"gateway answered {r.status_code}", RED))
            say(f"  {r.status_code}  {text[:64]:<64}  {verdict[1]}{verdict[0]}{RESET}")


def run_alert_attacks(s: dict) -> None:
    """S4: an agent keeps sending injection attempts; the GuardBlockSpike alert trips it."""
    ops.wait_alerts_clear(note)
    ops.preconditions(s.get("policy") or {}, note)
    attacks = yaml.safe_load((ROOT / "scenarios" / s["attacks"]).read_text())["subtle"][: s["count"]]
    agent = s["as"]
    key = ops.secret(f"consumer-{agent}")
    since = time.time()
    say(f"\n{agent} sends {len(attacks)} injection attempts to the LLM", BOLD)
    for text in attacks:
        r = httpx.post(f"{GATEWAY}/llm/v1/chat/completions", timeout=60,
                       json={"model": "demo", "messages": [{"role": "user", "content": text}]},
                       headers={"apikey": key, "X-Demo-Scenario": f"{s['id']}:scripted"})
        say(f"  {r.status_code}  {text[:70]}", RED if r.status_code != 200 else "")
    say(f"\nWaiting for the alert to trip agent:{agent} (up to {s['within']} s)", BOLD)
    took = wait_tripped(f"agent:{agent}", s["within"])
    if took is None:
        say("Not tripped in time.", RED)
        return
    say(f"  tripped {took:.0f} s after the last attempt", RED)
    show_kills(since)
    other = "executor" if agent == "researcher" else "researcher"
    r = httpx.post(f"{GATEWAY}/llm/v1/chat/completions", timeout=60,
                   json={"model": "demo", "messages": [{"role": "user", "content": "status check"}]},
                   headers={"apikey": ops.secret(f"consumer-{other}"), "X-Demo-Scenario": "smoke"})
    say(f"  {other} is unaffected: LLM call answered {r.status_code}", GREEN if r.status_code == 200 else RED)


def run_manual_kill(s: dict, scripted: bool) -> None:
    """S5: start a task, trip global mid-way, then reset with confirmation."""
    req = s["request"]
    out: dict = {}

    def worker():
        out["result"], out["elapsed"] = task(req["agent"], req["instruction"], header(s["id"], scripted))

    say(f"\nYou → {req['agent']}: {req['instruction']}", BOLD)
    t = threading.Thread(target=worker)
    t.start()
    time.sleep(s.get("trip_after_s", 2.5))
    with ops.Forward("gateway", "killswitch", 8000) as ks_url:
        ks = ops.KillSwitchApi(ks_url)
        r = ks.trip("global", "drill", actor="operator")
        say(f"  operator tripped global: verified in {r['latency_ms']:.0f} ms ({r['incident_id']})", RED)
        t.join()
        show_events(out["result"], req["agent"])
        show_result(req["agent"], out["result"], out["elapsed"])
        r = ks.reset("global", "drill over", actor="operator", confirm="RESET GLOBAL")
        say(f"\n  operator reset global: verified in {r['latency_ms']:.0f} ms", GREEN)
    result, elapsed = task(req["agent"], req["instruction"], header(s["id"], scripted))
    say(f"  service restored: {req['agent']} {result['status']} in {elapsed:.1f}s",
        GREEN if result["status"] == "completed" else RED)


def run_rogue(s: dict) -> None:
    """S6 / DEMO-03: a runaway agent is stopped by the anomaly alerts."""
    ops.wait_alerts_clear(note)
    ops.preconditions(s.get("policy") or {}, note)
    req = s["request"]
    since = time.time()
    say(f"\nYou → {req['agent']}: {req['instruction']}  {DIM}(rogue: up to {req['max_steps']} steps){RESET}", BOLD)
    result, elapsed = task(req["agent"], req["instruction"], f"{s['id']}:scripted", req["max_steps"], chat=True)
    tools = [e for e in result["events"] if e["type"] == "tool"]
    say(f"  {req['agent']} made {len(tools)} tool calls in {elapsed:.0f} s before it stopped", RED)
    show_result(req["agent"], result, elapsed)
    show_kills(since)


def run(sid: str, live: bool) -> None:
    """Scenarios are scripted unless --live: the story plays out the same way every time,
    while the console's free chat uses the real model in live mode."""
    s = find_scenario(sid)
    scripted = not live
    say(f"{s['id'].upper()}  {s['title']}", BOLD)
    note(s["story"])
    ops.preconditions(s.get("policy") or {}, note)
    kind = s.get("kind", "task")
    {"task": lambda: run_task(s, scripted), "attacks": lambda: run_attacks(s),
     "alert-attacks": lambda: run_alert_attacks(s), "manual-kill": lambda: run_manual_kill(s, scripted),
     "rogue": lambda: run_rogue(s)}[kind]()
    if s.get("show"):
        note(f"\nIn Grafana ({GRAFANA}): " + ", ".join(s["show"]))


def main() -> None:
    p = argparse.ArgumentParser(prog="demo")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("scenario")
    r.add_argument("--live", action="store_true", help="use the real model instead of the scenario's script")
    sub.add_parser("all")
    t = sub.add_parser("trip")
    t.add_argument("scope")
    t.add_argument("reason")
    k = sub.add_parser("kill-reset")
    k.add_argument("scope")
    k.add_argument("reason")
    sub.add_parser("reset")
    sub.add_parser("status")
    e = sub.add_parser("evidence")
    e.add_argument("--incident")
    e.add_argument("--trace")
    v = sub.add_parser("evidence-verify")
    v.add_argument("dir")
    rb = sub.add_parser("evidence-rebuild")
    rb.add_argument("dir")
    a = p.parse_args()

    if a.cmd == "run":
        run(a.scenario, a.live)
    elif a.cmd == "all":
        for sid in ("s0", "s1", "s2", "s3", "s4", "s5"):
            run(sid, False)
            say("")
            ops.full_reset(note)
            say("")
    elif a.cmd in ("trip", "kill-reset"):
        if a.scope not in ops.SCOPES:
            sys.exit(f"SCOPE must be one of {ops.SCOPES}")
        with ops.Forward("gateway", "killswitch", 8000) as ks_url:
            ks = ops.KillSwitchApi(ks_url)
            if a.cmd == "trip":
                res = ks.trip(a.scope, a.reason)
                say(f"{a.scope} tripped ({res['incident_id']}), verified in {res['latency_ms']:.0f} ms", RED)
            else:
                confirm = ""
                if a.scope == "global":
                    confirm = input('Type RESET GLOBAL to confirm: ').strip()
                res = ks.reset(a.scope, a.reason, confirm=confirm)
                say(f"{a.scope} reset, verified in {res['latency_ms']:.0f} ms", GREEN)
    elif a.cmd == "reset":
        ops.full_reset(note)
    elif a.cmd == "status":
        ops.status(say)
    elif a.cmd == "evidence":
        from demo_cli import evidence

        if bool(a.incident) == bool(a.trace):
            sys.exit("give exactly one of INCIDENT=... or TRACE=...")
        evidence.make(a.incident, a.trace, say)
    elif a.cmd == "evidence-verify":
        from demo_cli import evidence

        sys.exit(0 if evidence.verify(ops.ROOT / a.dir if not a.dir.startswith("/") else __import__("pathlib").Path(a.dir), say) else 1)
    elif a.cmd == "evidence-rebuild":
        from demo_cli import evidence

        evidence.rebuild(__import__("pathlib").Path(a.dir).resolve())


if __name__ == "__main__":
    main()
