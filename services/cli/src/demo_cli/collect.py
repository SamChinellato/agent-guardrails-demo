"""Evidence collection (7.9 steps 1-5): pull the raw inputs for one incident or trace into
evidence/<id>/raw/. Everything the report says is later derived from these files alone."""

import json
import re
import subprocess
import time
from pathlib import Path

import httpx
import yaml

from demo_cli import ops

WINDOW_S = 600  # +-10 min around the incident (7.9 step 1)
MAX_TRACES = 25
SECRET_FIELDS = re.compile(r"(key|password|secret|token|credential|authorization)", re.I)


def _write(path: Path, data, raw: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if raw else json.dumps(data, indent=2, sort_keys=True) + "\n")


def strip_secrets(obj):
    """Remove credential values from exported config (EVD-02, EVD-04)."""
    if isinstance(obj, dict):
        return {k: ("[removed]" if SECRET_FIELDS.search(k) and isinstance(v, (str, int)) else strip_secrets(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_secrets(v) for v in obj]
    return obj


def loki_query(loki: str, query: str, start: float, end: float, limit: int = 5000) -> list[dict]:
    r = httpx.get(f"{loki}/loki/api/v1/query_range", timeout=30, params={
        "query": query, "start": str(int(start * 1e9)), "end": str(int(end * 1e9)), "limit": limit,
        "direction": "forward"})
    r.raise_for_status()
    out = []
    for stream in r.json()["data"]["result"]:
        for ts, line in stream["values"]:
            out.append({"ts_ns": ts, "labels": stream["stream"], "line": line})
    return sorted(out, key=lambda e: (e["ts_ns"], e["line"]))


def settle(tempo: str, loki: str, trace_ids: list[str], decision_ids: list[str | None], start: float, say,
           timeout: float = 20) -> None:
    """Telemetry reaches Tempo and Loki a few seconds after the fact (OBS-06). Before
    collecting, wait until every named trace and the trip's decision record are stored and
    the traces have stopped growing, so a bundle made right after an incident is complete."""
    deadline = time.monotonic() + timeout
    sizes: dict[str, int] = {}
    while time.monotonic() < deadline:
        pending = []
        for tid in trace_ids:
            r = httpx.get(f"{tempo}/api/traces/{tid}", timeout=10)
            n = sum(len(ss["spans"]) for b in r.json().get("batches", []) for ss in b["scopeSpans"]) if r.status_code == 200 else 0
            if n == 0 or n != sizes.get(tid):
                pending.append(tid)
            sizes[tid] = n
        for did in filter(None, decision_ids):
            if not loki_query(loki, f'{{service_name="guard"}} | decision_id="{did}"', start - 60, time.time() + 1, 5):
                pending.append(did)
        if not pending:
            return
        time.sleep(2)
    say("Some telemetry was still arriving; the bundle has what was stored after 20 s")


def collect(target: str, kind: str, out: Path, say) -> dict:
    """kind: incident | trace. Returns what was collected (ids, window)."""
    raw = out / "raw"
    with ops.Forward("gateway", "killswitch", 8000) as ks_url, \
            ops.Forward("observability", "tempo", 3200) as tempo, \
            ops.Forward("observability", "loki", 3100) as loki, \
            ops.Forward("observability", "kps-prometheus", 9090) as prom, \
            ops.Forward("gateway", "apisix-admin", 9180) as admin, \
            ops.Forward("ai", "guard", 8000) as guard:
        ks = ops.KillSwitchApi(ks_url)

        # 1. Incident -> killswitch events -> trace ids, agents and the window
        audit = ks.http.get("/v1/audit/raw").text
        events = [json.loads(line) for line in audit.splitlines() if line.strip()]
        trace_ids: list[str] = []
        agents: set[str] = set()
        if kind == "incident":
            mine = [e for e in events if e.get("incident_id") == target]
            if not mine:
                raise SystemExit(f"no killswitch events for incident {target}")
            start, end = min(e["ts"] for e in mine), max(e["ts"] for e in mine)
            # the incident's reset(s) follow its trips but carry no incident_id
            scopes = {e["scope"] for e in mine}
            resets = [e for e in events if e["type"] == "reset" and e["scope"] in scopes and start <= e["ts"] <= end + WINDOW_S]
            if resets:
                end = max(end, resets[0]["ts"])
            for e in mine:
                trace_ids += e.get("trace_ids") or []
                if e["scope"].startswith("agent:"):
                    agents.add(e["scope"][6:])
        else:
            t = httpx.get(f"{tempo}/api/traces/{target}", timeout=30)
            if t.status_code != 200:
                raise SystemExit(f"trace {target} not found in Tempo")
            stamps = [int(s["startTimeUnixNano"]) / 1e9 for b in t.json()["batches"] for ss in b["scopeSpans"] for s in ss["spans"]]
            start, end = min(stamps), max(stamps)
            trace_ids = [target]
        w0, w1 = start - WINDOW_S, end + WINDOW_S
        settle(tempo, loki, trace_ids, [e.get("decision_id") for e in events
                                         if kind == "incident" and e.get("incident_id") == target], start, say)

        # Guard decisions. A trip that names its trace (guard auto-trip) scopes the bundle to that
        # trace; an alert-driven trip names none, so take that agent's activity in the 2 minutes
        # before the trip. The +-10 min window is where these are searched for (7.9 step 1).
        # Filter in LogQL, not afterwards: the window can hold thousands of routine "allow"
        # records, and a client-side filter would stop at Loki's line limit (D-041).
        named = set(trace_ids)
        relevant: list[dict] = []
        if named:
            for tid in sorted(named):
                relevant += [d["labels"] for d in loki_query(
                    loki, f'{{service_name="guard"}} | trace_id="{tid}" | event="guard_decision"', w0, w1)]
        else:
            who = "|".join(sorted(agents))
            relevant = [d["labels"] for d in loki_query(
                loki, f'{{service_name="guard"}} | event="guard_decision" | agent=~"{who}" | verdict=~"block|flag"',
                start - 120, start + 1)]
            trace_ids += [d["trace_id"] for d in relevant if d.get("trace_id")]
            if len(set(trace_ids)) < 3:
                access = loki_query(loki, f'{{service_name=~"api-gateway|mcp-gateway"}} | json | consumer=~"{who}"', start - 120, start + 1)
                trace_ids += [a["labels"]["trace_id"] for a in access if a["labels"].get("trace_id")]
        relevant = [d for d in relevant if d.get("verdict") in ("block", "flag")]
        incident_window = [start - 120, end]
        trace_ids = sorted(dict.fromkeys(t for t in trace_ids if t))[:MAX_TRACES]
        say(f"{len(trace_ids)} trace(s), window {time.strftime('%H:%M:%S', time.localtime(w0))}"
            f"–{time.strftime('%H:%M:%S', time.localtime(w1))}")

        # 2-3. Traces and their logs
        for tid in trace_ids:
            t = httpx.get(f"{tempo}/api/traces/{tid}", timeout=30)
            if t.status_code == 200:
                _write(raw / "traces" / f"{tid}.json", t.json())
            logs = loki_query(loki, f'{{service_name=~".+"}} | json | trace_id="{tid}"', w0, w1)
            _write(raw / "logs" / f"{tid}.jsonl", "".join(json.dumps(x, sort_keys=True) + "\n" for x in logs), raw=True)
        _write(raw / "guard-decisions.jsonl", "".join(json.dumps(d, sort_keys=True) + "\n" for d in relevant), raw=True)

        # Killswitch: the whole audit chain (needed to verify it) and this incident's events
        _write(raw / "killswitch" / "audit.jsonl", audit, raw=True)

        # 4. Prometheus snapshot over the window
        queries = {
            "killswitch_active": "max by (scope) (killswitch_active)",
            "guard_blocks_per_min": 'sum by (agent, check) (rate(guard_decisions_total{verdict="block"}[1m])) * 60',
            "cut_off_503_per_min": 'sum by (consumer) (rate(gateway_http_status{code="503"}[1m])) * 60',
            "tokens_per_min": "sum by (consumer) (rate(gateway_llm_prompt_tokens[1m]) + rate(gateway_llm_completion_tokens[1m])) * 60",
            "tool_calls_per_min": 'sum by (consumer) (rate(gateway_http_status{route=~"mcp-.*"}[1m])) * 60',
            "alerts_firing": 'max by (alertname, killswitch_scope) (ALERTS{alertstate="firing"})',
        }
        snapshot = {}
        for name, q in queries.items():
            r = httpx.get(f"{prom}/api/v1/query_range", timeout=30,
                          params={"query": q, "start": w0, "end": min(w1, time.time()), "step": "15s"})
            snapshot[name] = {"query": q, "result": r.json()["data"]["result"]}
        _write(raw / "metrics.json", snapshot)

        # 5. Config snapshots: APISIX objects without credentials, the guard policy
        key = ops.secret("apisix-admin-key")
        config = {}
        for kind_ in ("routes", "consumers", "plugin_configs", "upstreams", "plugin_metadata/opentelemetry"):
            r = httpx.get(f"{admin}/apisix/admin/{kind_}", headers={"X-API-KEY": key}, timeout=30).json()
            items = r.get("list") if "list" in r else [r]
            config[kind_] = sorted((strip_secrets(i["value"]) for i in items if "value" in i),
                                   key=lambda v: str(v.get("id") or v.get("username") or ""))
        _write(raw / "config" / "apisix.json", config)
        policy = httpx.get(f"{guard}/v1/policy", timeout=10).json()
        _write(raw / "config" / "guard-policy.json", policy)

    # environment.json (EVD-02): versions.env, git SHA, the image digests actually running
    versions = {}
    for line in (ops.ROOT / "versions.env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            versions[k] = v
    sha = subprocess.run(["git", "-C", str(ops.ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "-C", str(ops.ROOT), "status", "--porcelain"], capture_output=True, text=True).stdout.strip())
    images = {}
    for ns in ("gateway", "ai", "observability"):
        pods = json.loads(ops.kubectl("-n", ns, "get", "pods", "-o", "json").stdout)["items"]
        for p in pods:
            for c in p["status"].get("containerStatuses", []):
                images[f"{ns}/{c['name']}"] = {"image": c["image"], "image_id": c["imageID"]}
    _write(raw / "environment.json", {"versions": versions, "git_sha": sha, "git_dirty": dirty,
                                      "images": dict(sorted(images.items()))})
    meta = {"kind": kind, "target": target, "window": [w0, w1], "focus": incident_window,
            "trace_ids": trace_ids, "agents": sorted(agents)}
    _write(raw / "request.json", meta)
    return meta


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text())
