"""Evidence report (7.9 steps 6-9, EVD-03..07), built only from the bundle's raw/ files.

The same raw/ always gives a byte-identical report.json (EVD-05): sorted keys, fixed float
precision, no generation time (that lives in manifest.json). report.html is rendered from
report.json with a Jinja template.
"""

import hashlib
import json
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES = Path(__file__).parent / "templates"
GENESIS = "0" * 64

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
# Phone numbers in written formats (+1-555-0101, 555-123-4567), not any run of digits:
# a looser pattern also caught dates inside incident ids.
_PHONE = re.compile(r"\+\d{1,3}(?:[\s.-]\d{2,4}){2,4}\b|\b\d{3}[\s.-]\d{3}[\s.-]\d{4}\b")
_NAME = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}\b")


def redact(text: str | None) -> str | None:
    """Emails, phone numbers and names out of free text (EVD-04)."""
    if text is None:
        return None
    text = _EMAIL.sub("[email]", str(text))
    text = _PHONE.sub("[phone]", text)
    return _NAME.sub("[name]", text)


def iso(ts: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def verify_chain(lines: list[str]) -> tuple[bool, str]:
    """The killswitch audit chain (KS-07): hash = sha256(canonical event without hash)."""
    prev = GENESIS
    for i, line in enumerate(lines, 1):
        try:
            e = json.loads(line)
        except ValueError:
            return False, f"audit line {i} is not valid JSON"
        body = json.dumps({k: v for k, v in e.items() if k != "hash"}, sort_keys=True, separators=(",", ":")).encode()
        if e.get("prev_hash") != prev:
            return False, f"audit line {i}: prev_hash does not match line {i - 1}"
        if hashlib.sha256(body).hexdigest() != e.get("hash"):
            return False, f"audit line {i}: hash does not match its content"
        prev = e["hash"]
    return True, f"{len(lines)} audit events, chain intact"


def _parse_iso(value: str) -> float:
    import datetime

    return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=datetime.UTC).timestamp()


TOOL_WORDS = {"docs__list_documents": "list documents", "docs__read_document": "read a document",
              "actions__update_record": "update a record", "actions__send_email": "send an email",
              "actions__export_customer_data": "export customer data"}


def condensed(tl: list[dict]) -> list[dict]:
    """For the HTML only: a run of routine tool calls by one agent becomes one row with counts.
    Anything else (blocks, denials, trips, alerts, hand-offs) keeps its own row."""
    out: list[dict] = []

    def routine(e):
        return e["action"].startswith("tool ") and e["action"].endswith(": ok") and not e.get("decision")

    for e in tl:
        last = out[-1] if out else None
        if routine(e) and last and last.get("_run") and last["actor"] == e["actor"]:
            tool = e["action"][5:-4]
            last["_counts"][tool] = last["_counts"].get(tool, 0) + 1
            last["until"] = e["ts"]
            continue
        row = {**e, "until": e["ts"]}
        if routine(e):
            row.update(_run=True, _counts={e["action"][5:-4]: 1})
        out.append(row)
    for row in out:
        if row.get("_run"):
            counts = row.pop("_counts")
            row.pop("_run")
            n = sum(counts.values())
            parts = ", ".join(f"{TOOL_WORDS.get(t, t)} {c}×" for t, c in sorted(counts.items(), key=lambda kv: -kv[1]))
            row["action"] = f"{n} tool call{'s' if n > 1 else ''}: {parts}" if n > 1 else f"tool {next(iter(counts))}: ok"
            row["count"] = n
        else:
            row["count"] = 1
    return out


def _attrs(span: dict) -> dict:
    out = {}
    for a in span.get("attributes", []):
        v = a["value"]
        out[a["key"]] = next(iter(v.values())) if v else None
    return out


def _b64_to_hex(v: str) -> str:
    import base64

    try:
        return base64.b64decode(v).hex()
    except Exception:  # noqa: BLE001
        return v


def _decisions(raw: Path) -> list[dict]:
    """Guard decision records, one per decision_id, oldest first."""
    out: dict[str, dict] = {}
    for line in (raw / "guard-decisions.jsonl").read_text().splitlines():
        d = json.loads(line)
        out.setdefault(d.get("decision_id") or line, d)
    return sorted(out.values(), key=lambda d: (float(d.get("ts") or 0), d.get("decision_id") or ""))


def timeline(raw: Path, req: dict) -> list[dict]:
    w0, w1 = req["window"]
    f0, f1 = req.get("focus", req["window"])
    incident = req["target"] if req["kind"] == "incident" else None
    items: list[dict] = []

    def add(ts, component, actor, action, decision=None, trace_id=None, span_id=None, detail=None):
        items.append({
            "ts": iso(ts), "_ts": ts, "component": component, "actor": actor, "action": action,
            "decision": decision, "trace_id": trace_id, "span_id": span_id,
            "detail": redact(detail), "control_refs": [],  # EVD-07
        })

    # This incident's kill-switch events, and the reset of its scope that ended it
    lines = [json.loads(line) for line in (raw / "killswitch/audit.jsonl").read_text().splitlines()]
    scopes = {e["scope"] for e in lines if incident and e.get("incident_id") == incident}
    last_trip = max((e["ts"] for e in lines if incident and e.get("incident_id") == incident), default=None)
    first_reset = min((e["ts"] for e in lines if e["type"] == "reset" and e["scope"] in scopes
                       and last_trip and e["ts"] > last_trip and not e.get("no_change")), default=None)
    for e in lines:
        mine = (e.get("incident_id") == incident) if incident else (w0 <= e["ts"] <= w1)
        if not (mine or e["ts"] == first_reset):
            continue
        what = f"{e['type']} {e['scope']}" + (" (no change)" if e.get("no_change") else "")
        add(e["ts"], "killswitch", f"{e['source']}:{e['actor']}", what, e.get("decision_id"),
            (e.get("trace_ids") or [None])[0], None,
            f"{e['reason']}; verified={e['verified']} in {e['latency_ms']:.0f} ms; incident {e.get('incident_id') or '-'}")

    # Guard decisions (block / flag)
    for d in _decisions(raw):
        ts = float(d.get("ts") or 0) or int(d.get("observed_timestamp", 0)) / 1e9
        add(ts, "guard", d.get("agent"), f"{d.get('check')} {d.get('verdict')}", d.get("decision_id"),
            d.get("trace_id"), d.get("span_id"), f"{d.get('reason')}" + (f" (tool {d['tool']})" if d.get("tool") else ""))

    # Spans that tell the story: tasks, tool calls, hand-offs, auto-trips. Tempo can return a
    # span twice (live store and a flushed block), so spans are de-duplicated by id.
    seen: set[str] = set()
    for f in sorted((raw / "traces").glob("*.json")):
        t = json.loads(f.read_text())
        for b in t.get("batches", []):
            svc = next((a["value"].get("stringValue") for a in b["resource"]["attributes"] if a["key"] == "service.name"), "?")
            for ss in b.get("scopeSpans", []):
                for s in ss.get("spans", []):
                    name, a = s["name"], _attrs(s)
                    if name not in ("agent.task", "mcp.tool_call", "agent.handoff", "guard.auto_trip"):
                        continue
                    if name == "mcp.tool_call" and a.get("mcp.server") == "local":
                        continue  # the hand-off is shown by its agent.handoff span
                    ts = int(s["startTimeUnixNano"]) / 1e9
                    tid, sid = _b64_to_hex(s.get("traceId", "")), _b64_to_hex(s.get("spanId", ""))
                    if sid in seen:
                        continue
                    seen.add(sid)
                    if name == "agent.task":
                        action = f"task started ({a.get('scenario.id') or 'no scenario'})"
                        detail = f"status {a.get('agent.status')}"
                    elif name == "mcp.tool_call":
                        action = f"tool {a.get('mcp.tool')}: {a.get('mcp.decision')}"
                        detail = None
                    elif name == "agent.handoff":
                        action = "hand-off to executor"
                        detail = f"{a.get('handoff.items')} action item(s)"
                    else:
                        action, detail = f"auto-trip {a.get('killswitch.scope')}", None
                    add(ts, svc, a.get("agent.id") or svc, action, a.get("guard.decision_id"), tid, sid, detail)

    # Alerts: the first time each one fired in the window
    metrics = json.loads((raw / "metrics.json").read_text())
    for series in metrics["alerts_firing"]["result"]:
        m = series["metric"]
        if incident and m.get("killswitch_scope") not in scopes:
            continue
        first = next((float(ts) for ts, v in series["values"] if float(v) >= 1 and f0 - 60 <= float(ts) <= f1), None)
        if first is not None:
            add(first, "prometheus", "alert rule", f"alert {m.get('alertname')} firing",
                detail=f"scope {m.get('killswitch_scope') or '-'}")

    items.sort(key=lambda i: (i["_ts"], i["component"], i["span_id"] or "", i["action"]))
    for i in items:
        del i["_ts"]
    return items


def summary(raw: Path, req: dict, tl: list[dict]) -> dict:
    events = [json.loads(line) for line in (raw / "killswitch/audit.jsonl").read_text().splitlines()]
    w0, w1 = req["window"]
    incident = req["target"] if req["kind"] == "incident" else None
    trips = [e for e in events if e["type"] == "trip" and not e.get("no_change")
             and (e.get("incident_id") == incident if incident else w0 <= e["ts"] <= w1)]
    decisions = _decisions(raw)
    blocks = [d for d in decisions if d.get("verdict") == "block"]
    flags = [d for d in decisions if d.get("verdict") == "flag"]

    containment = {}
    if trips:
        trip = trips[0]
        ts_of = {d.get("decision_id"): float(d.get("ts") or 0) for d in decisions}
        violation_ts = ts_of.get(trip.get("decision_id")) or (min(float(d.get("ts") or 0) for d in blocks) if blocks else None)
        resets = [e for e in events if e["type"] == "reset" and e["scope"] == trip["scope"] and e["ts"] > trip["ts"] and not e.get("no_change")]
        first_seen = min((_parse_iso(i["ts"]) for i in tl if i["component"] not in ("killswitch", "prometheus")), default=None)
        containment = {
            "activity_to_trip_s": round(trip["ts"] - first_seen, 1) if first_seen else None,
            "scope": trip["scope"],
            "tripped_by": trip["source"],
            "violation_to_trip_ms": round((trip["ts"] - violation_ts) * 1000, 1) if violation_ts else None,
            "trip_to_verified_ms": trip["latency_ms"],
            "verified": trip["verified"],
            "trip_to_reset_s": round(resets[0]["ts"] - trip["ts"], 1) if resets else None,
        }

    def blocked(d):
        return {"agent": d.get("agent"), "check": d.get("check"), "tool": d.get("tool"),
                "decision_id": d.get("decision_id"), "reason": redact(d.get("reason"))}

    what = []
    for t in trips:
        cause = {"guard": "the guard", "alertmanager": "an alert", "manual": f"operator {t['actor']}"}.get(t["source"], t["source"])
        what.append(f"{t['scope']} was tripped by {cause}: {redact(t['reason'])}")
    if flags:
        what.append(f"{len(flags)} suspicious item(s) were flagged in monitor mode and allowed through")
    return {
        "what_happened": what,
        "blocked": sorted((blocked(d) for d in blocks), key=lambda b: (b["decision_id"] or "")),
        "who_acted": sorted({f"{t['source']}:{t['actor']}" for t in trips}),
        "containment": containment,
        "counts": {"traces": len(req["trace_ids"]), "timeline_entries": len(tl), "guard_blocks": len(blocks),
                   "guard_flags": len(flags), "kill_switch_trips": len(trips)},
    }


def build(bundle: Path) -> dict:
    raw = bundle / "raw"
    req = json.loads((raw / "request.json").read_text())
    ok, chain = verify_chain([line for line in (raw / "killswitch/audit.jsonl").read_text().splitlines() if line.strip()])
    policy = json.loads((raw / "config/guard-policy.json").read_text())
    env = json.loads((raw / "environment.json").read_text())
    tl = timeline(raw, req)
    report = {
        "subject": {"kind": req["kind"], "id": req["target"], "window": [iso(req["window"][0]), iso(req["window"][1])],
                    "trace_ids": req["trace_ids"], "agents": req["agents"]},
        "summary": summary(raw, req, tl),
        "timeline": tl,
        "integrity": {"audit_chain_valid": ok, "audit_chain": chain},
        "configuration": {"guard_policy_version": policy["version"], "git_sha": env["git_sha"],
                          "git_dirty": env["git_dirty"]},
    }
    text = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    (bundle / "report.json").write_text(text)
    jenv = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]))
    (bundle / "report.html").write_text(jenv.get_template("report.html.j2").render(r=report, rows=condensed(tl)))
    return report
