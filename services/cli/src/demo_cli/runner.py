"""make demo SCENARIO=<id>: run one scenario through the gateway with narrated output.

Each scenario first sets its guard policy (every check back to policy/policy.yaml, then the
scenario's overrides) and waits until the guard reports the new version. Requests go in as
the operator would send them: demo-user -> APISIX -> researcher (F1).
"""

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[4]
KUBECTL = str(ROOT / ".tools/bin/kubectl") if (ROOT / ".tools/bin/kubectl").exists() else "kubectl"
KUBECONFIG = os.environ.get("KUBECONFIG_DEMO") or str(ROOT / ".kube/config")


def env_local(name: str, default: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    f = ROOT / ".env.local"
    if f.exists():
        for line in f.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return default


GATEWAY = f"http://127.0.0.1:{env_local('HOST_PORT_GATEWAY', '8080')}"
GRAFANA = f"http://localhost:{env_local('HOST_PORT_GRAFANA', '3000')}"
BOLD, DIM, RED, GREEN, BLUE, RESET = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[34m", "\033[0m"


def say(text: str = "", colour: str = "") -> None:
    print(f"{colour}{text}{RESET}" if colour else text, flush=True)


def kubectl(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([KUBECTL, "--kubeconfig", KUBECONFIG, *args], capture_output=True, text=True, **kw)


def find_scenario(sid: str) -> dict:
    for f in sorted((ROOT / "scenarios").glob("s*.yaml")):
        s = yaml.safe_load(f.read_text())
        if s["id"] == sid or f.stem == sid:
            return s
    sys.exit(f"no scenario {sid!r} in scenarios/")


class GuardPort:
    """Port-forward to the guard (NetworkPolicy admits only APISIX; port-forward is the
    deliberate operator path, as for the Admin API)."""

    def __enter__(self):
        self.proc = subprocess.Popen([KUBECTL, "--kubeconfig", KUBECONFIG, "-n", "ai", "port-forward", "svc/guard", ":8000"],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        m = re.search(r"127\.0\.0\.1:(\d+)", self.proc.stdout.readline())
        if not m:
            self.proc.terminate()
            sys.exit("could not port-forward to the guard")
        self.url = f"http://127.0.0.1:{m.group(1)}"
        return self

    def __exit__(self, *exc):
        self.proc.terminate()

    def version(self) -> str | None:
        try:
            return httpx.get(f"{self.url}/v1/policy", timeout=5).json()["version"]
        except (httpx.HTTPError, KeyError, ValueError):
            return None


def set_policy(overrides: dict) -> None:
    """policy/policy.yaml with the scenario's overrides, applied and waited for (GRD-02)."""
    policy = yaml.safe_load((ROOT / "policy/policy.yaml").read_text())
    for check, mode in overrides.items():
        policy["checks"][check]["mode"] = mode
    text = yaml.safe_dump(policy, sort_keys=False, default_flow_style=None)
    import hashlib
    want = hashlib.sha256(text.encode()).hexdigest()[:12]
    cm = kubectl("-n", "ai", "create", "configmap", "guard-policy", f"--from-literal=policy.yaml={text}",
                 "--dry-run=client", "-o", "yaml")
    subprocess.run([KUBECTL, "--kubeconfig", KUBECONFIG, "apply", "-f", "-"], input=cm.stdout,
                   capture_output=True, text=True, check=True)
    changes = ", ".join(f"{c} -> {m}" for c, m in overrides.items()) or "all checks enforce"
    deadline = time.monotonic() + 20
    with GuardPort() as guard:
        while guard.version() != want:
            if time.monotonic() > deadline:
                sys.exit("guard did not load the scenario policy within 20 s")
            time.sleep(1)
    say(f"Policy: {changes}", DIM)


def trace_url(trace_id: str) -> str:
    import json
    import urllib.parse
    panes = {"a": {"datasource": "tempo", "queries": [{"refId": "A", "queryType": "traceql", "query": trace_id}],
                   "range": {"from": "now-1h", "to": "now"}}}
    return f"{GRAFANA}/explore?schemaVersion=1&panes={urllib.parse.quote(json.dumps(panes))}"


def header(scripted: bool, sid: str) -> str:
    return f"{sid}:scripted" if scripted else sid


def run_task(s: dict, scripted: bool, key: str) -> dict:
    req = s["request"]
    say(f"\nYou → {req['agent']}: {req['instruction']}", BOLD)
    t0 = time.monotonic()
    r = httpx.post(f"{GATEWAY}/agents/{req['agent']}/task", timeout=900,
                   json={"instruction": req["instruction"], "scenario_id": header(scripted, s["id"])},
                   headers={"apikey": key, "X-Demo-Scenario": header(scripted, s["id"])})
    if r.status_code != 200:
        say(f"Gateway answered {r.status_code}: {r.text[:200]}", RED)
        return {}
    body = r.json()
    for e in body["events"]:
        who = e.get("agent", req["agent"])
        if e["type"] == "tool":
            colour = RED if e["outcome"] in ("denied", "quarantined", "halted") else ""
            extra = f"  {DIM}{e.get('decision_id', '')}{RESET}" if e.get("decision_id") else ""
            say(f"  {who:<11}{e['tool']:<34} {e['outcome']}{extra}", colour)
        elif e["type"] == "agent":
            say(f"  {who:<11}finished: {e['status']}", GREEN if e["status"] == "completed" else RED)
        elif e["type"] in ("blocked", "halted"):
            say(f"  {who:<11}{e['type']}: {e.get('reason') or e.get('detail')}", RED)
    status_colour = GREEN if body["status"] == "completed" else RED
    say(f"\n{req['agent']} ({body['status']}, {time.monotonic() - t0:.1f}s): {body['reply']}", status_colour)
    say(f"Trace {body['trace_id']}  {DIM}{trace_url(body['trace_id'])}{RESET}", BLUE)
    return body


def run_attacks(s: dict, key: str) -> None:
    attacks = yaml.safe_load((ROOT / "scenarios" / s["attacks"]).read_text())
    for kind in ("crude", "subtle", "benign"):
        say(f"\n{kind.capitalize()} prompts", BOLD)
        for text in attacks[kind]:
            r = httpx.post(f"{GATEWAY}/agents/researcher/chat", timeout=300,
                           json={"messages": [{"role": "user", "content": text}], "scenario_id": "s1:scripted"},
                           headers={"apikey": key, "X-Demo-Scenario": "s1:scripted"})
            try:
                err = r.json().get("error") or {}
            except ValueError:
                err = {}
            if r.status_code == 200:
                verdict, colour = "passed to the agent", GREEN
            elif r.status_code == 400:
                verdict, colour = "stopped: known attack pattern", RED
            elif r.status_code == 403:
                verdict, colour = f"stopped: prompt injection  {DIM}{err.get('decision_id', '')}{RESET}", RED
            else:
                verdict, colour = f"gateway answered {r.status_code}", RED
            say(f"  {r.status_code}  {text[:64]:<64}  {colour}{verdict}{RESET}")


def main() -> None:
    p = argparse.ArgumentParser(prog="demo")
    p.add_argument("scenario")
    p.add_argument("--scripted", action="store_true",
                   help="use the mock LLM script even in live mode (deterministic)")
    a = p.parse_args()
    s = find_scenario(a.scenario)
    mode = env_local("LLM_MODE", "mock")
    scripted = a.scripted or mode == "mock"
    say(f"{s['id'].upper()}  {s['title']}", BOLD)
    say(s["story"], DIM)
    set_policy(s.get("policy") or {})
    key = (ROOT / ".secrets/consumer-demo-user").read_text().strip()
    if "attacks" in s:
        run_attacks(s, key)
    else:
        run_task(s, scripted, key)
    if s.get("show"):
        say(f"\nIn Grafana ({GRAFANA}): " + ", ".join(s["show"]), DIM)


if __name__ == "__main__":
    main()
