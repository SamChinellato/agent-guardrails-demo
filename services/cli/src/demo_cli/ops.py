"""Operator helpers shared by the runner and the make targets: port-forwards, the killswitch
API (operator token), Prometheus queries and the full demo reset (REP-07)."""

import hashlib
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
SCOPES = ["global", "tools", "agent:researcher", "agent:executor"]
KS_ALERTS = ("GuardBlockSpike", "TokenBurn", "AgentToolCallSpike")


def secret(name: str) -> str:
    return (ROOT / ".secrets" / name).read_text().strip()


def kubectl(*args: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([KUBECTL, "--kubeconfig", KUBECONFIG, *args], capture_output=True, text=True, **kw)


class Forward:
    """kubectl port-forward to a Service on a free local port (the deliberate operator path)."""

    def __init__(self, namespace: str, service: str, port: int):
        self.args = [KUBECTL, "--kubeconfig", KUBECONFIG, "-n", namespace, "port-forward", f"svc/{service}", f":{port}"]

    def __enter__(self) -> str:
        self.proc = subprocess.Popen(self.args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        m = re.search(r"127\.0\.0\.1:(\d+)", self.proc.stdout.readline())
        if not m:
            self.proc.terminate()
            sys.exit(f"could not port-forward: {' '.join(self.args[3:])}")
        return f"http://127.0.0.1:{m.group(1)}"

    def __exit__(self, *exc) -> None:
        self.proc.terminate()


class KillSwitchApi:
    def __init__(self, base: str):
        self.http = httpx.Client(base_url=base, timeout=30,
                                 headers={"Authorization": f"Bearer {secret('killswitch-operator-token')}"})

    def status(self) -> dict:
        return self.http.get("/v1/status").raise_for_status().json()

    def trip(self, scope: str, reason: str, actor: str = "operator") -> dict:
        r = self.http.post("/v1/trip", json={"scope": scope, "reason": reason, "actor": actor, "source": "manual"})
        if r.status_code != 200:
            sys.exit(f"trip failed ({r.status_code}): {r.text}")
        return r.json()

    def reset(self, scope: str, reason: str, actor: str = "operator", confirm: str = "") -> dict:
        r = self.http.post("/v1/reset", json={"scope": scope, "reason": reason, "actor": actor, "confirm": confirm})
        if r.status_code != 200:
            sys.exit(f"reset failed ({r.status_code}): {r.text}")
        return r.json()

    def events(self, limit: int = 100) -> list[dict]:
        return self.http.get("/v1/events", params={"limit": limit}).raise_for_status().json()["events"]

    def rotate(self) -> str | None:
        return self.http.post("/v1/rotate").raise_for_status().json()["archived"]


def active_alerts(prom: str, names=KS_ALERTS) -> list[dict]:
    """Kill-switch alerts that are firing or pending (about to fire)."""
    r = httpx.get(f"{prom}/api/v1/alerts", timeout=10).json()["data"]["alerts"]
    return [a for a in r if a["state"] in ("firing", "pending") and a["labels"].get("alertname") in names]


def apply_policy(guard: str, overrides: dict | None = None, timeout: float = 20) -> str:
    """policy/policy.yaml plus overrides into the guard-policy ConfigMap; waits until served."""
    policy = yaml.safe_load((ROOT / "policy/policy.yaml").read_text())
    for check, mode in (overrides or {}).items():
        policy["checks"][check]["mode"] = mode
    text = yaml.safe_dump(policy, sort_keys=False, default_flow_style=None)
    want = hashlib.sha256(text.encode()).hexdigest()[:12]
    cm = kubectl("-n", "ai", "create", "configmap", "guard-policy", f"--from-literal=policy.yaml={text}",
                 "--dry-run=client", "-o", "yaml")
    subprocess.run([KUBECTL, "--kubeconfig", KUBECONFIG, "apply", "-f", "-"], input=cm.stdout,
                   capture_output=True, text=True, check=True)
    end = time.monotonic() + timeout
    while True:
        try:
            if httpx.get(f"{guard}/v1/policy", timeout=5).json().get("version") == want:
                return want
        except (httpx.HTTPError, ValueError):
            pass
        if time.monotonic() > end:
            sys.exit("the guard did not load the policy in time")
        time.sleep(0.5)


def release_all(ks: KillSwitchApi, reason: str) -> list[str]:
    released = []
    for scope, on in ks.status()["scopes"].items():
        if on:
            ks.reset(scope, reason, confirm="RESET GLOBAL" if scope == "global" else "")
            released.append(scope)
    return released


def preconditions(overrides: dict | None, say) -> None:
    """What every scenario does first: kill switches open, guard memory clear, its policy."""
    with Forward("gateway", "killswitch", 8000) as ks_url, Forward("ai", "guard", 8000) as guard:
        released = release_all(KillSwitchApi(ks_url), "scenario preconditions")
        httpx.post(f"{guard}/v1/autotrip/reset", timeout=10)
        apply_policy(guard, overrides)
    changes = ", ".join(f"{c} -> {m}" for c, m in (overrides or {}).items()) or "all checks enforce"
    say(f"Policy: {changes}" + (f"; released {', '.join(released)}" if released else ""))


def wait_alerts_clear(say, names=KS_ALERTS, timeout: float = 150) -> None:
    """Wait until no kill-switch alert is firing or pending, so leftovers from earlier
    activity can't trip an agent during the next run (D-037)."""
    with Forward("observability", "kps-prometheus", 9090) as prom:
        end = time.monotonic() + timeout
        shown = False
        while alerts := active_alerts(prom, names):
            if not shown:
                say("Waiting for earlier alerts to clear: " + ", ".join(sorted({a['labels']['alertname'] for a in alerts})))
                shown = True
            if time.monotonic() > end:
                say("Alerts still firing; continuing anyway")
                return
            time.sleep(3)


def full_reset(say) -> None:
    """make reset (REP-07): clean demo state without recreating the cluster."""
    t0 = time.monotonic()
    with Forward("gateway", "killswitch", 8000) as ks_url, Forward("ai", "guard", 8000) as guard, \
            Forward("ai", "mcp-actions", 8000) as actions:
        ks = KillSwitchApi(ks_url)
        released = release_all(ks, "make reset")
        say(f"Kill switches open{' (released ' + ', '.join(released) + ')' if released else ''}")
        httpx.post(f"{guard}/v1/autotrip/reset", timeout=10)
        apply_policy(guard)
        say("Guard policy restored from policy/policy.yaml")
        httpx.delete(f"{actions}/outbox", timeout=10)
        say("Outbox cleared, supplier records restored")
        archived = ks.rotate()
        say(f"Audit log rotated ({archived})" if archived else "Audit log already empty")
    wait_alerts_clear(say, timeout=max(10, 110 - (time.monotonic() - t0)))
    say(f"Reset done in {time.monotonic() - t0:.0f} s")


def status(say) -> None:
    bad = [line.split()[0] for ns in ("gateway", "ai", "observability")
           for line in kubectl("-n", ns, "get", "pods", "--no-headers").stdout.splitlines()
           if "Running" not in line or line.split()[1].split("/")[0] != line.split()[1].split("/")[1]]
    say("Pods: all running" if not bad else f"Pods not ready: {', '.join(bad)}")
    with Forward("gateway", "killswitch", 8000) as ks_url, Forward("ai", "guard", 8000) as guard, \
            Forward("observability", "kps-prometheus", 9090) as prom:
        scopes = KillSwitchApi(ks_url).status()["scopes"]
        say("Kill switch: " + ", ".join(f"{s} {'TRIPPED' if on else 'open'}" for s, on in scopes.items()))
        p = httpx.get(f"{guard}/v1/policy", timeout=5).json()
        modes = {c: v["mode"] for c, v in p["policy"]["checks"].items()}
        say(f"Guard policy {p['version']}: " + ", ".join(f"{c}={m}" for c, m in modes.items()))
        alerts = [a["labels"]["alertname"] for a in httpx.get(f"{prom}/api/v1/alerts", timeout=10).json()["data"]["alerts"]
                  if a["state"] == "firing"]
        say("Alerts firing: " + (", ".join(alerts) if alerts else "none"))
