"""Presenter console (DEMO-01). One page; its backend holds the demo-user key, so the
browser never sees a secret. Every message goes through the gateway to the researcher
(F1), so the gateway's inbound filters apply before the agent sees anything (DEMO-02).

  GET  /             the page
  POST /api/chat     {messages, rogue?} -> {kind: reply|blocked|halted|error, ...}
"""

import os
from pathlib import Path

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from console_app import scenarios
from demo_common import create_app, get_logger, run

log = get_logger("console")
GATEWAY = os.environ.get("GATEWAY_URL", "http://apisix-gateway.gateway.svc.cluster.local").rstrip("/")
KEY = os.environ.get("DEMO_USER_KEY", "")
GRAFANA_PORT = os.environ.get("GRAFANA_PORT", "3000")
ROGUE_SCENARIO = os.environ.get("ROGUE_SCENARIO", "s6-rogue:scripted")
ROGUE_MAX_STEPS = int(os.environ.get("ROGUE_MAX_STEPS", "120"))
PAGE = (Path(__file__).parent / "static" / "index.html").read_text()

app = create_app("console")


@app.get("/", response_class=HTMLResponse)
async def page() -> HTMLResponse:
    return HTMLResponse(PAGE.replace("{{GRAFANA_PORT}}", GRAFANA_PORT))


@app.post("/api/chat")
async def chat(request: Request) -> JSONResponse:
    body = await request.json()
    messages = body.get("messages") or []
    last = messages[-1]["content"] if messages and messages[-1].get("role") == "user" else ""
    # A story prompt plays its scenario (D-044); it still goes through the gateway as demo-user,
    # so a malicious one is filtered exactly like any other message
    if (fn := scenarios.trigger_for(last)) is not None:
        try:
            return JSONResponse({"kind": "steps", "steps": await fn(last)})
        except Exception as e:  # noqa: BLE001
            log.exception("story prompt failed")
            return JSONResponse({"kind": "error", "message": f"That request could not run: {e}"})
    rogue = bool(body.get("rogue"))
    payload = {"messages": body.get("messages") or [], "scenario_id": ROGUE_SCENARIO if rogue else "chat"}
    if rogue:
        payload["max_steps"] = ROGUE_MAX_STEPS
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            r = await client.post(f"{GATEWAY}/agents/researcher/chat", json=payload,
                                  headers={"apikey": KEY, "X-Demo-Scenario": payload["scenario_id"]})
    except httpx.HTTPError as e:
        log.error("gateway unreachable", extra={"error": str(e)})
        return JSONResponse({"kind": "error", "message": "The gateway could not be reached."})
    try:
        data = r.json()
    except ValueError:
        data = {"message": r.text[:200]}
    err = data.get("error") if isinstance(data.get("error"), dict) else {}

    if r.status_code == 200:
        return JSONResponse({"kind": "reply", **data})
    if r.status_code == 400:  # ai-prompt-guard: a known attack pattern
        return JSONResponse({"kind": "blocked", "reason": "pattern"})
    if r.status_code == 403 and err.get("type") == "prompt_injection_blocked":
        return JSONResponse({"kind": "blocked", "reason": "injection", "decision_id": err.get("decision_id")})
    if r.status_code == 503 and err.get("type") == "kill_switch_engaged":
        return JSONResponse({"kind": "halted", "incident_id": err.get("incident_id")})
    log.warning("unexpected gateway answer", extra={"status": r.status_code})
    return JSONResponse({"kind": "error", "message": f"The gateway answered {r.status_code}."})


@app.get("/api/scenarios")
async def scenario_list() -> JSONResponse:
    return JSONResponse([{"id": k, "title": t} for k, (t, _) in scenarios.SCENARIOS.items() if k != "reset"])


@app.post("/api/scenario")
async def scenario(request: Request) -> JSONResponse:
    """Run one scenario end to end and return the steps to draw (D-043)."""
    sid = (await request.json()).get("id", "")
    if sid not in scenarios.SCENARIOS:
        return JSONResponse({"error": f"unknown scenario {sid!r}"}, 400)
    title, fn = scenarios.SCENARIOS[sid]
    try:
        steps = await fn()
    except Exception as e:  # noqa: BLE001 - show the presenter what went wrong, don't crash the page
        log.exception("scenario failed", extra={"scenario": sid})
        steps = [{"kind": "lane", "tone": "stop", "text": f"The scenario could not run: {e}"}]
    return JSONResponse({"id": sid, "title": title, "steps": steps})


def main() -> None:
    run(app)
