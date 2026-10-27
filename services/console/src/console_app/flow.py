"""Where a request went (D-046), from its real trace: the major components it touched, how
many calls crossed each link, and where the gateway blocked or stopped something.

Links (all through the gateway):
  you-researcher, researcher-llm, researcher-docs, researcher-executor,
  executor-llm, executor-actions, killswitch (an agent suspended inside this request)
"""

import asyncio
import os
import ssl
import time
from pathlib import Path

import httpx

TEMPO = os.environ.get("TEMPO_URL", "http://tempo.observability.svc.cluster.local:3200").rstrip("/")
# OpenShift (OS-15): the Tempo gateway takes the pod's ServiceAccount token (re-read per call,
# the kubelet rotates it) and serves a certificate from the service CA
TEMPO_TOKEN_FILE = os.environ.get("TEMPO_TOKEN_FILE", "")
TEMPO_CA_FILE = os.environ.get("TEMPO_CA_FILE", "")
ROUTE_LINK = {"agent-researcher": "you-researcher", "agent-executor": "researcher-executor"}


# The gateway's spans, as the collector names them: /mcp routes and the rest (D-071)
GATEWAYS = {"api-gateway", "mcp-gateway"}


def _attrs(span: dict) -> dict:
    return {a["key"]: next(iter(a["value"].values()), None) for a in span.get("attributes", [])}


def _link(route: str, consumer: str) -> str | None:
    if route in ROUTE_LINK:
        return ROUTE_LINK[route]
    target = "llm" if route.startswith("llm") else "docs" if route == "mcp-docs" else "actions" if route == "mcp-actions" else None
    if target and consumer in ("researcher", "executor"):
        return f"{consumer}-{target}"
    return None


async def _fetch(client: httpx.AsyncClient, trace_id: str) -> dict | None:
    try:  # a large trace still being written can time out; the caller simply tries again
        headers = {"Authorization": f"Bearer {Path(TEMPO_TOKEN_FILE).read_text().strip()}"} if TEMPO_TOKEN_FILE else {}
        r = await client.get(f"{TEMPO}/api/traces/{trace_id}", headers=headers)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError, OSError):
        return None


async def flow(trace_id: str, settle_s: float = 10) -> dict:
    """Wait (bounded) until the trace stops growing, then summarise it."""
    deadline, last, data = time.monotonic() + settle_s, -1, None
    async with httpx.AsyncClient(timeout=20, verify=ssl.create_default_context(cafile=TEMPO_CA_FILE) if TEMPO_CA_FILE else True) as client:
        while time.monotonic() < deadline:
            data = await _fetch(client, trace_id)
            n = sum(len(ss["spans"]) for b in (data or {}).get("batches", []) for ss in b["scopeSpans"])
            if n and n == last:
                break
            last = n
            await asyncio.sleep(1.5)
    links: dict[str, dict] = {}
    seen: set[str] = set()
    kills: list[str] = []

    def bump(link: str, field: str = "calls", detail: str | None = None) -> None:
        entry = links.setdefault(link, {"calls": 0, "blocked": 0, "withheld": 0, "stopped": 0, "details": []})
        entry[field] += 1
        if detail and detail not in entry["details"]:
            entry["details"].append(detail)

    for batch in (data or {}).get("batches", []):
        service = next((a["value"].get("stringValue") for a in batch["resource"]["attributes"]
                        if a["key"] == "service.name"), "")
        for ss in batch["scopeSpans"]:
            for span in ss["spans"]:
                if span["spanId"] in seen:  # Tempo can return a span twice (D-041)
                    continue
                seen.add(span["spanId"])
                a = _attrs(span)
                if service in GATEWAYS and (link := _link(a.get("route_id", ""), a.get("consumer_name", ""))):
                    status = int(a.get("http.status_code") or 0)
                    bump(link)
                    if status in (400, 403):  # MCP denials answer 200; those come from the guard below
                        bump(link, "blocked", "prompt blocked" if link.endswith("llm") else "message blocked")
                    elif status == 503:
                        bump(link, "stopped", "refused: circuit breaker open")
                if service == "guard":
                    for ev in span.get("events", []):
                        if ev["name"] != "guard.decision":
                            continue
                        e = {x["key"]: next(iter(x["value"].values()), None) for x in ev.get("attributes", [])}
                        if e.get("guard.verdict") != "block":
                            continue
                        agent, tool, check = e.get("guard.agent", ""), e.get("guard.tool") or "", e.get("guard.check", "")
                        if tool.startswith("actions."):
                            bump(f"{agent}-actions", "blocked", tool.split(".", 1)[1].replace("_", " "))
                        elif tool.startswith("docs.") and check == "mcp.tool_result_injection":
                            bump(f"{agent}-docs", "withheld", "a document withheld")
                if service == "killswitch" and span["name"] == "killswitch.trip":
                    kills.append(a.get("killswitch.scope", ""))
    for entry in links.values():  # a blocked/stopped call is still one call
        entry["calls"] = max(entry["calls"], entry["blocked"] + entry["stopped"])
    return {"trace_id": trace_id, "links": links, "kills": sorted(set(kills)), "complete": last > 0}
