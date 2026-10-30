"""Presenter console (DEMO-01). One page; its backend holds the demo-user key, so the
browser never sees a secret. Every message goes through the gateway to the researcher
(F1), so the gateway's inbound filters apply before the agent sees anything (DEMO-02).

  GET  /             the page
  POST /api/chat     {messages, rogue?} -> {kind: reply|blocked|halted|error, ...}
  GET  /api/me       who is signed in, and their tokens' claims (AUTH_MODE=verify, D-072)

With AUTH_MODE=verify (OpenShift) every page and API needs an IBM Verify sign-in first
(identity.py, docs/IDENTITY.md); the default, none, is the anonymous console as before.
"""

import os
from pathlib import Path

import httpx
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from console_app import flow as flows
from console_app import identity, scenarios
from demo_common import create_app, get_logger, run

log = get_logger("console")
GATEWAY = os.environ.get("GATEWAY_URL", "http://apisix-gateway.gateway.svc.cluster.local").rstrip("/")
KEY = os.environ.get("DEMO_USER_KEY", "")
GRAFANA_PORT = os.environ.get("GRAFANA_PORT", "3000")
# OpenShift (OS-04): Grafana has its own Route host, <subdomain>.<the console's domain>
GRAFANA_SUBDOMAIN = os.environ.get("GRAFANA_SUBDOMAIN", "")
# The "How it works" diagram for this target: architecture.png (k3d) or
# architecture-openshift.png (D-062); both are rendered by make diagram
ARCHITECTURE = Path(os.environ.get("ARCHITECTURE_IMAGE", "architecture.png")).name
ROGUE_SCENARIO = os.environ.get("ROGUE_SCENARIO", "s6-rogue:scripted")
ROGUE_MAX_STEPS = int(os.environ.get("ROGUE_MAX_STEPS", "120"))
PAGE = (Path(__file__).parent / "static" / "index.html").read_text()

app = create_app("console")
OPEN = ("/healthz", "/readyz", "/metrics", "/auth/", "/signin")   # reachable without signing in
# OpenShift: IBM Verify is the console's only gate (no OAuth proxy in front, D-072), so without
# it the console serves nothing rather than an anonymous, public console
REQUIRE_SIGN_IN = os.environ.get("REQUIRE_SIGN_IN", "") == "true"
SIGNIN = (Path(__file__).parent / "static" / "signin.html").read_text()


@app.middleware("http")
async def require_sign_in(request: Request, call_next):
    """AUTH_MODE=verify: pages send you to the sign-in page, the API answers 401 (D-072)."""
    path = request.url.path
    if REQUIRE_SIGN_IN and not identity.enabled() and not path.startswith(("/healthz", "/readyz", "/metrics")):
        return JSONResponse({"error": "sign-in with IBM Verify isn't configured (scripts/openshift/verify-secret.sh)"}, 503)
    if not identity.enabled() or path.startswith(OPEN):
        return await call_next(request)
    if (s := identity.session(request)) is not None:
        identity.current.set(s)   # the agents act for this person (D-072)
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"error": "sign in first"}, 401)
    from fastapi.responses import RedirectResponse

    return RedirectResponse(identity.PREFIX + "signin", 303)


@app.get("/signin", include_in_schema=False)
async def signin(request: Request):
    """The sign-in page: one button to IBM Verify, and the reason if the last try failed."""
    if identity.enabled() and identity.session(request):
        from fastapi.responses import RedirectResponse

        return RedirectResponse(identity.PREFIX, 303)
    return HTMLResponse(SIGNIN)


app.add_api_route("/auth/login", identity.login, methods=["GET"], include_in_schema=False)
app.add_api_route("/auth/callback", identity.callback, methods=["GET"], include_in_schema=False)
app.add_api_route("/auth/logout", identity.logout, methods=["GET"], include_in_schema=False)

app.add_api_route("/api/me", identity.me, methods=["GET"], include_in_schema=False)


@app.get("/architecture.png", include_in_schema=False)
async def architecture():
    """The architecture diagram (make diagram), shown from the console header."""
    from fastapi.responses import FileResponse

    return FileResponse(Path(__file__).parent / "static" / ARCHITECTURE, media_type="image/png")


@app.get("/samples/{name}", include_in_schema=False)
async def sample(name: str):
    """Example documents to upload (D-047): one clean, one poisoned."""
    from fastapi.responses import FileResponse, PlainTextResponse

    path = Path(__file__).parent / "static" / "samples" / Path(name).name
    if not path.is_file():
        return PlainTextResponse("not found", 404)
    return FileResponse(path, media_type="text/markdown", filename=path.name)


@app.post("/api/upload")
async def upload(request: Request) -> JSONResponse:
    """Add a document to the supplier store, through the gateway (D-047)."""
    body = await request.json()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{GATEWAY}/demo/documents", headers={"apikey": KEY},
                              json={"name": body.get("name"), "content": body.get("content")})
    try:
        data = r.json()
    except ValueError:
        data = {"error": f"the gateway answered {r.status_code}"}
    return JSONResponse(data, r.status_code if r.status_code < 500 else 502)


@app.get("/", response_class=HTMLResponse)
async def page() -> HTMLResponse:
    return HTMLResponse(PAGE.replace("{{GRAFANA_PORT}}", GRAFANA_PORT).replace("{{GRAFANA_SUBDOMAIN}}", GRAFANA_SUBDOMAIN))


@app.post("/api/chat")
async def chat(request: Request) -> JSONResponse:
    body = await request.json()
    messages = body.get("messages") or []
    last = messages[-1]["content"] if messages and messages[-1].get("role") == "user" else ""
    # A story prompt plays its scenario (D-044); it still goes through the gateway as demo-user,
    # so a malicious one is filtered exactly like any other message
    trace_id = _trace_id()
    if (fn := scenarios.trigger_for(last)) is not None:
        try:
            return JSONResponse({"kind": "steps", "steps": await fn(last), "trace_id": trace_id})
        except Exception as e:  # noqa: BLE001
            log.exception("story prompt failed")
            return JSONResponse({"kind": "error", "message": f"That request could not run: {e}"})
    rogue = bool(body.get("rogue"))
    payload = {"messages": body.get("messages") or [], "scenario_id": ROGUE_SCENARIO if rogue else "chat"}
    if rogue:
        payload["max_steps"] = ROGUE_MAX_STEPS
    elif identity.current.get() is not None:
        payload["handoff"] = True   # a signed-in person's researcher may hand work to the executor (D-072)
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            r = await client.post(f"{GATEWAY}/agents/researcher/chat", json=payload,
                                  headers={"apikey": KEY, "X-Demo-Scenario": payload["scenario_id"],
                                           **identity.agent_headers()})
    except httpx.HTTPError as e:
        log.error("gateway unreachable", extra={"error": str(e)})
        return JSONResponse({"kind": "error", "message": "The gateway could not be reached."})
    try:
        data = r.json()
    except ValueError:
        data = {"message": r.text[:200]}
    err = data.get("error") if isinstance(data.get("error"), dict) else {}

    if r.status_code == 200:
        identity.remember(data)
        if data.get("status") == "halted_by_kill_switch":  # the agent met an earlier trip (D-069)
            data.update(await scenarios.explain_halt())
        return JSONResponse({"kind": "reply", "trace_id": trace_id, **data})
    if r.status_code == 400:  # ai-prompt-guard: a known attack pattern
        return JSONResponse({"kind": "blocked", "reason": "pattern", "trace_id": trace_id})
    if r.status_code == 403 and err.get("type") == "prompt_injection_blocked":
        return JSONResponse({"kind": "blocked", "reason": "injection", "decision_id": err.get("decision_id"),
                             "score": err.get("score"), "threshold": _threshold(err.get("reason")),
                             "trace_id": trace_id})
    if r.status_code == 503 and err.get("type") == "kill_switch_engaged":
        return JSONResponse({"kind": "halted", "incident_id": err.get("incident_id"), "trace_id": trace_id,
                             **await scenarios.explain_halt(err.get("incident_id"))})
    log.warning("unexpected gateway answer", extra={"status": r.status_code})
    return JSONResponse({"kind": "error", "message": f"The gateway answered {r.status_code}."})


@app.get("/api/flow/{trace_id}")
async def flow(trace_id: str) -> JSONResponse:
    """Monitoring tab: where this request went, from its trace (D-046)."""
    return JSONResponse(await flows.flow(trace_id))


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


def _trace_id() -> str | None:
    """The trace this exchange belongs to: the console's own request span."""
    from opentelemetry import trace as otel_trace

    ctx = otel_trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def _threshold(reason: str | None) -> float | None:
    import re

    m = re.search(r"the ([0-9.]+) threshold", reason or "")
    return float(m.group(1)) if m else None


def main() -> None:
    run(app)
