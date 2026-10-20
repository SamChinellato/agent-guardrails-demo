"""Killswitch HTTP API (KS-02) and control panel (KS-08).

  POST /v1/trip          {scope, reason, actor, source, trace_ids?, decision_id?}
  POST /v1/reset         {scope, reason, actor, confirm?}   global needs confirm "RESET GLOBAL"
  GET  /v1/status        live from APISIX
  GET  /v1/events        recent audit events;  GET /v1/audit/verify  hash chain check
  POST /v1/alertmanager  Alertmanager webhook (F6)
  POST /v1/rotate        archive the audit log (make reset)
  GET  /                 control panel (operator token, set once as a cookie)

Tokens (KS-09): operator -> everything; guard -> trip agent:* only; alertmanager -> webhook only.
"""

import hmac
import html
import os
import threading
import time

from fastapi import Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from demo_common import create_app, get_logger, run
from killswitch.audit import AuditLog
from killswitch.service import SCOPES, KillSwitch, Rejected

log = get_logger("killswitch.api")
TOKENS = {
    "operator": os.environ.get("OPERATOR_TOKEN", ""),
    "guard": os.environ.get("GUARD_TOKEN", ""),
    "alertmanager": os.environ.get("ALERTMANAGER_TOKEN", ""),
}
COOKIE = "ks_operator"

ks = KillSwitch(AuditLog(os.environ.get("AUDIT_LOG", "/data/audit.jsonl")))
app = create_app("killswitch", ready_check=lambda: bool(ks.status()))


def _gauge_loop() -> None:
    while True:
        ks.refresh_gauges()
        time.sleep(5)


threading.Thread(target=_gauge_loop, daemon=True, name="gauges").start()


def caller(request: Request) -> str | None:
    """Which token the request carries: bearer header, or the panel cookie (operator)."""
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else request.cookies.get(COOKIE, "")
    for role, value in TOKENS.items():
        if value and token and hmac.compare_digest(token, value):
            return role
    return None


def deny(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status)


def _authorise(role: str | None, action: str, scope: str = "") -> JSONResponse | None:
    if role is None:
        return deny(401, "a valid bearer token is required")
    if role == "operator":
        return None
    if role == "guard" and action == "trip" and scope.startswith("agent:"):
        return None
    if role == "alertmanager" and action == "alertmanager":
        return None
    return deny(403, f"the {role} token may not {action} {scope}".strip())


# ---------------------------------------------------------------------------- API
@app.post("/v1/trip")
async def trip(request: Request) -> JSONResponse:
    body = await request.json()
    scope, role = body.get("scope", ""), caller(request)
    if err := _authorise(role, "trip", scope):
        return err
    if not body.get("reason"):
        return deny(400, "reason is required")
    try:
        e = ks.trip(scope, body["reason"], body.get("actor") or role, body.get("source") or "manual",
                    body.get("trace_ids"), body.get("decision_id"))
    except Rejected as r:
        return deny(r.status, str(r))
    return JSONResponse({"incident_id": e["incident_id"], "verified": e["verified"], "latency_ms": e["latency_ms"],
                         "no_change": e["no_change"], "event_id": e["event_id"]})


@app.post("/v1/reset")
async def reset(request: Request) -> JSONResponse:
    body = await request.json()
    scope, role = body.get("scope", ""), caller(request)
    if err := _authorise(role, "reset", scope):
        return err
    if not body.get("reason"):
        return deny(400, "reason is required")
    if scope == "global" and body.get("confirm") != "RESET GLOBAL":
        return deny(400, 'resetting global needs confirm: "RESET GLOBAL"')
    try:
        e = ks.reset(scope, body["reason"], body.get("actor") or role)
    except Rejected as r:
        return deny(r.status, str(r))
    return JSONResponse({"verified": e["verified"], "latency_ms": e["latency_ms"], "no_change": e["no_change"],
                         "event_id": e["event_id"]})


@app.get("/v1/status")
async def status(request: Request) -> JSONResponse:
    if caller(request) != "operator":
        return deny(401, "operator token required")
    return JSONResponse(ks.status())


@app.get("/v1/events")
async def events(request: Request, limit: int = 100) -> JSONResponse:
    if caller(request) != "operator":
        return deny(401, "operator token required")
    return JSONResponse({"events": ks.audit.read(limit)})


@app.get("/v1/audit/verify")
async def audit_verify(request: Request) -> JSONResponse:
    if caller(request) != "operator":
        return deny(401, "operator token required")
    ok, detail = ks.audit.verify()
    return JSONResponse({"valid": ok, "detail": detail})


@app.get("/v1/audit/raw")
async def audit_raw(request: Request) -> HTMLResponse:
    """The audit log exactly as stored, for the evidence bundle (EVD-02)."""
    if caller(request) != "operator":
        return deny(401, "operator token required")
    text = ks.audit.path.read_text() if ks.audit.path.exists() else ""
    return HTMLResponse(text, media_type="application/x-ndjson")


@app.post("/v1/rotate")
async def rotate(request: Request) -> JSONResponse:
    if caller(request) != "operator":
        return deny(401, "operator token required")
    return JSONResponse({"archived": ks.audit.rotate()})


@app.post("/v1/alertmanager")
async def alertmanager(request: Request) -> JSONResponse:
    if err := _authorise(caller(request), "alertmanager"):
        return err
    return JSONResponse({"results": ks.from_alertmanager(await request.json())})


# ---------------------------------------------------------------------------- panel
LABELS = {"global": "Everything", "tools": "All tools", "agent:researcher": "Researcher agent",
          "agent:executor": "Executor agent"}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kill switch</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible+Next:wght@400;600;700&display=swap">
<style>
:root{--fog:#eef1f4;--paper:#fff;--ink:#1c2433;--muted:#6b7484;--rule:#d8dde4;--open:#2e8a57;--stop:#c8322f;--stop-soft:#f7e1e0;--lane:#2f5aa8;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--fog:#141a24;--paper:#1d2533;--ink:#e8ecf2;--muted:#9aa3b2;--rule:#2c3647;--open:#5cc48b;--stop:#f0706c;--stop-soft:#3a2224;--lane:#6f97e0;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--fog);color:var(--ink);font:400 18px/1.5 "Atkinson Hyperlegible Next",system-ui,sans-serif}
main{max-width:46rem;margin:0 auto;padding:28px 16px 48px}h1{font-size:1.5rem;margin:0 0 20px}
ul{list-style:none;margin:0;padding:0}.scope{display:flex;justify-content:space-between;align-items:center;gap:12px;
background:var(--paper);border:1px solid var(--rule);border-radius:12px;padding:14px 18px;margin-bottom:8px}
.state{font-weight:700}.state.open{color:var(--open)}.state.on{color:var(--stop)}.scope.on{background:var(--stop-soft);border-color:var(--stop)}
.why{color:var(--muted);font-size:.9rem}form.act{display:flex;flex-wrap:wrap;gap:8px;margin:20px 0 28px}
select,input,button{font:inherit;border-radius:10px;border:1px solid var(--rule);padding:10px 12px;background:var(--paper);color:var(--ink)}
input[name=reason],input[name=confirm]{flex:1;min-width:10rem}button{cursor:pointer;font-weight:600;border:0;color:#fff}
.trip{background:var(--stop)}.reset{background:var(--lane)}h2{font-size:1rem;color:var(--muted);font-weight:600;margin:0 0 8px}
.ev{padding:6px 0;border-bottom:1px solid var(--rule);font-size:.95rem}
form.act .row{display:flex;gap:8px;width:100%}form.act [hidden]{display:none}.ev time{color:var(--muted);margin-right:10px}
.msg{padding:10px 14px;border-radius:10px;background:var(--paper);border:1px solid var(--rule);margin-bottom:16px}
:focus-visible{outline:3px solid var(--lane);outline-offset:2px}
</style></head><body><main>
<h1>Kill switch</h1>
{message}
<div id="live">{live}</div>
<form class="act" method="post" action="act">
  <div class="row"><select name="scope" id="scope" aria-label="Scope">{options}</select>
  <input name="reason" placeholder="Reason" required aria-label="Reason"></div>
  <div class="row"><input name="confirm" id="confirm" placeholder="To reset everything, type RESET GLOBAL" aria-label="Confirm global reset">
  <button class="trip" name="action" value="trip">Trip</button>
  <button class="reset" name="action" value="reset">Reset</button></div>
</form>
<h2>Recent events</h2><div id="events">{events}</div>
</main>
<script>
const scope = document.getElementById("scope"), confirmBox = document.getElementById("confirm");
const showConfirm = () => { confirmBox.hidden = scope.value !== "global"; };
scope.addEventListener("change", showConfirm); showConfirm();
setInterval(async () => {
  try {
    const r = await fetch("fragment", {credentials: "same-origin"});
    if (!r.ok) return;
    const d = await r.json();
    document.getElementById("live").innerHTML = d.live;
    document.getElementById("events").innerHTML = d.events;
  } catch (e) {}
}, 3000);
</script></body></html>"""

LOGIN = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kill switch</title><style>body{margin:0;background:#eef1f4;color:#1c2433;font:400 18px/1.5 system-ui,sans-serif}
main{max-width:26rem;margin:15vh auto;padding:16px}input,button{font:inherit;width:100%;padding:12px;border-radius:10px;border:1px solid #d8dde4;margin-top:10px}
button{background:#2f5aa8;color:#fff;border:0;font-weight:600}</style></head><body><main>
<h1>Kill switch</h1><form method="post" action="login"><input type="password" name="token" placeholder="Operator token" aria-label="Operator token" required autofocus>
<button>Open the control panel</button></form>{error}</main></body></html>"""


def _fmt_time(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _live_html() -> str:
    try:
        scopes = ks.status()["scopes"]
    except Exception:  # noqa: BLE001
        return '<p class="msg">The gateway could not be read.</p>'
    # the trip that actually engaged each scope (repeats that changed nothing don't count)
    trips = {e["scope"]: e for e in ks.audit.read(200) if e["type"] == "trip" and not e.get("no_change")}
    rows = []
    for scope in SCOPES:
        on = scopes.get(scope, False)
        why = ""
        if on and scope in trips:
            e = trips[scope]
            why = f'<div class="why">{html.escape(e["reason"])}. Incident {html.escape(e["incident_id"] or "")}</div>'
        rows.append(f'<li class="scope{" on" if on else ""}"><div>{LABELS[scope]}{why}</div>'
                    f'<span class="state {"on" if on else "open"}">{"Tripped" if on else "Open"}</span></li>')
    return "<ul>" + "".join(rows) + "</ul>"


def _events_html() -> str:
    out = []
    changed = [e for e in ks.audit.read(100) if not e.get("no_change")][-8:]  # repeats stay in the audit log
    for e in reversed(changed):
        verb = "tripped" if e["type"] == "trip" else "reset"
        who = {"guard": "by the guard", "alertmanager": "by an alert", "manual": f'by {html.escape(e["actor"])}'}.get(e["source"], "")
        mark = "" if e.get("verified") else " (not verified)"
        out.append(f'<div class="ev"><time>{_fmt_time(e["ts"])}</time>{LABELS.get(e["scope"], e["scope"])} {verb} {who}: '
                   f'{html.escape(e["reason"])} <span class="why">{e["latency_ms"]:.0f} ms{mark}</span></div>')
    return "".join(out) or '<p class="why">No events yet.</p>'


@app.get("/", response_class=HTMLResponse)
async def panel(request: Request, msg: str = "") -> HTMLResponse:
    if caller(request) != "operator":
        return HTMLResponse(LOGIN.replace("{error}", ""))
    options = "".join(f'<option value="{s}">{LABELS[s]}</option>' for s in SCOPES)
    message = f'<p class="msg">{html.escape(msg)}</p>' if msg else ""
    return HTMLResponse(PAGE.replace("{live}", _live_html()).replace("{events}", _events_html())
                        .replace("{options}", options).replace("{message}", message))


@app.get("/fragment")
async def fragment(request: Request) -> JSONResponse:
    if caller(request) != "operator":
        return deny(401, "operator token required")
    return JSONResponse({"live": _live_html(), "events": _events_html()})


@app.post("/login")
async def login(token: str = Form(...)) -> HTMLResponse:
    if not (TOKENS["operator"] and hmac.compare_digest(token, TOKENS["operator"])):
        return HTMLResponse(LOGIN.replace("{error}", "<p>That token is not the operator token.</p>"), 401)
    r = RedirectResponse("./", 303)
    r.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=12 * 3600)
    return r


@app.post("/act")
async def act(request: Request, scope: str = Form(...), reason: str = Form(...), action: str = Form(...),
              confirm: str = Form("")) -> RedirectResponse:
    if caller(request) != "operator":
        return RedirectResponse("./", 303)
    try:
        if action == "trip":
            e = ks.trip(scope, reason, "operator", "manual")
            msg = f"{LABELS[scope]} tripped and verified in {e['latency_ms']:.0f} ms."
        elif scope == "global" and confirm != "RESET GLOBAL":
            msg = "To reset everything, type RESET GLOBAL in the confirmation box."
        else:
            e = ks.reset(scope, reason, "operator", "manual")
            msg = f"{LABELS[scope]} reset and verified in {e['latency_ms']:.0f} ms."
    except Rejected as r:
        msg = str(r)
    from urllib.parse import quote

    return RedirectResponse(f"./?msg={quote(msg)}", 303)


def main() -> None:
    run(app)
