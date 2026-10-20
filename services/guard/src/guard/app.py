"""Guard service (7.3): the only component that holds decision logic.

  POST /v1/inspect/llm   called by the ai-injection-guard Lua plugin (GW-09)
  POST /mcp/{server}     MCP inspecting proxy (GRD-07)
  GET  /v1/policy        current policy and its version hash

X-Agent-Id is trusted because NetworkPolicy only admits APISIX pods (7.3).
"""

import os
import threading

from fastapi import Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from opentelemetry import trace
from prometheus_client import Histogram

from demo_common import create_app, get_logger, run
from guard.autotrip import AutoTrip
from guard.classifier import Classifier
from guard.engine import Engine, LocalDecider, Subject, init_series
from guard.mcp_proxy import McpProxy
from guard.policy import PolicyStore

log = get_logger("guard")
tracer = trace.get_tracer("guard")
INSPECT_SECONDS = Histogram("guard_inspect_seconds", "Time to inspect one LLM request", ["stage"])

MODEL_DIR = os.environ.get("MODEL_DIR", "/models/onnx")
POLICY_FILE = os.environ.get("POLICY_FILE", "/policy/policy.yaml")
ORDER = {"allow": 0, "flag": 1, "block": 2}

state: dict = {}


def _load() -> None:
    """Load the model off the request path; /readyz stays 503 until done (GRD-08)."""
    store = PolicyStore(POLICY_FILE)
    classifier = Classifier(MODEL_DIR)
    classifier.score("warm-up")
    autotrip = AutoTrip(store)
    engine = Engine(store, LocalDecider(classifier), on_critical=autotrip)
    state.update(store=store, engine=engine, proxy=McpProxy(engine), autotrip=autotrip)
    log.info("guard ready", extra={"policy_version": store.policy.version})


def ready() -> bool:
    return "engine" in state and state["store"].ready


app = create_app("guard", ready_check=ready)
init_series()
threading.Thread(target=_load, name="guard-load", daemon=True).start()


def _text(content) -> str:
    """OpenAI message content: a string or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _subjects(messages: list[dict], stage: str, agent: str) -> list[Subject]:
    route = "agent-ingress" if stage == "agent_ingress" else "llm"
    out = []
    for m in messages:
        role, text = m.get("role"), _text(m.get("content"))
        if not text:
            continue
        if stage == "agent_ingress" and role == "user":
            out.append(Subject("agent.user_prompt_injection", agent, route, content=text))
        elif stage == "llm" and role == "user":
            out.append(Subject("llm.user_prompt_injection", agent, route, content=text))
        elif stage == "llm" and role == "tool":
            if text in state["engine"].own:
                continue  # the guard's own quarantine notice
            origin = "external" if text in state["engine"].external else "agent"
            out.append(Subject("llm.tool_message_injection", agent, route, content=text, origin=origin))
    return out


@app.post("/v1/inspect/llm")
async def inspect_llm(request: Request) -> JSONResponse:
    if not ready():
        return JSONResponse({"error": "guard not ready"}, 503)
    body = await request.json()
    stage = body.get("stage", "llm")
    agent = request.headers.get("x-agent-id", "")
    with tracer.start_as_current_span("guard.inspect", attributes={"guard.stage": stage, "agent.id": agent}) as span, \
            INSPECT_SECONDS.labels(stage).time():
        decisions = []
        for subject in _subjects(body.get("messages") or [], stage, agent):
            d = await run_in_threadpool(state["engine"].evaluate, subject)
            if d:
                decisions.append(d)
        worst = max(decisions, key=lambda d: (ORDER[d.verdict], d.score), default=None)
        verdict = worst.verdict if worst else "allow"
        span.set_attribute("guard.verdict", verdict)
        result = {"verdict": verdict, "decision_ids": [d.decision_id for d in decisions]}
        if worst and verdict != "allow":
            result.update(decision_id=worst.decision_id, check=worst.check, reason=worst.reason, score=worst.score)
        return JSONResponse(result)


@app.post("/mcp/{server}")
async def mcp(server: str, request: Request) -> Response:
    if not ready():
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "guard not ready"}}, 503)
    with tracer.start_as_current_span(
        "guard.mcp", attributes={"mcp.server": server, "agent.id": request.headers.get("x-agent-id", "")}
    ):
        return await state["proxy"].handle(server, request)


@app.api_route("/v1/documents", methods=["POST", "DELETE"])
async def documents(request: Request) -> Response:
    """Console uploads (D-047), passed to the documents server, which only the guard may reach.
    Not scanned here: the guard scans every document when an agent reads it."""
    import httpx

    from guard.mcp_proxy import UPSTREAMS

    url = UPSTREAMS["docs"].rsplit("/mcp", 1)[0] + "/uploads"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.request(request.method, url, content=await request.body(),
                                 headers={"content-type": "application/json"})
    return Response(r.content, status_code=r.status_code, media_type="application/json")


@app.post("/v1/autotrip/reset")
async def autotrip_reset() -> JSONResponse:
    """Clear auto-trip de-duplication (make reset). Not routed by APISIX; reached only by
    the operator's port-forward."""
    if not ready():
        return JSONResponse({"error": "guard not ready"}, 503)
    state["autotrip"].clear()
    return JSONResponse({"cleared": True})


@app.get("/v1/policy")
async def policy() -> JSONResponse:
    if not ready():
        return JSONResponse({"error": "guard not ready"}, 503)
    p = state["store"].policy
    return JSONResponse({"version": p.version, "policy": p.raw})


def main() -> None:
    run(app)

