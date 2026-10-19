"""Scripted OpenAI-compatible LLM (7.5).

Scripts live in SCRIPTS_DIR/<scenario>.yaml, selected by the X-Demo-Scenario header the
agents propagate. Each script lists, per agent (X-Agent-Id, set by the gateway), one entry
per step: tool calls or a final message. The step is the number of assistant turns already
in the conversation, so the service is stateless and every reply is deterministic.

Script format:

    agents:
      researcher:
        - tool_calls: [{name: docs__list_documents, arguments: {}}]
        - final: "Summary ..."
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import Request
from fastapi.responses import JSONResponse
from prometheus_client import Counter

from demo_common import create_app, get_logger, run

SCRIPTS_DIR = Path(os.environ.get("SCRIPTS_DIR", "/scripts"))
log = get_logger("mock_llm")
app = create_app("mock-llm")

REQUESTS = Counter("mock_llm_requests_total", "Chat completion requests", ["scenario", "agent", "matched"])

_cache: dict[Path, tuple[float, dict]] = {}


def load_script(scenario: str) -> dict | None:
    """Read a scenario script, re-reading when the file changes (ConfigMap updates)."""
    if not scenario or "/" in scenario or scenario.startswith("."):
        return None
    path = SCRIPTS_DIR / f"{scenario}.yaml"
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    script = yaml.safe_load(path.read_text()) or {}
    _cache[path] = (mtime, script)
    return script


def count_tokens(value: Any) -> int:
    """Rough whitespace token count; stable across runs, which is all the demo needs."""
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return max(1, len(text.split()))


def stable_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()
    return f"{prefix}-{digest[:24]}"


def build_message(entry: dict, step: int, request_id: str) -> tuple[dict, str]:
    if "tool_calls" in entry:
        calls = [
            {
                "id": stable_id("call", request_id, step, i),
                "type": "function",
                "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments", {}), sort_keys=True)},
            }
            for i, c in enumerate(entry["tool_calls"])
        ]
        return {"role": "assistant", "content": entry.get("content"), "tool_calls": calls}, "tool_calls"
    return {"role": "assistant", "content": entry.get("final", "")}, "stop"


def generic_message(messages: list[dict]) -> dict:
    last_user = next((m.get("content") for m in reversed(messages) if m.get("role") == "user"), "")
    preview = (last_user or "")[:80] if isinstance(last_user, str) else ""
    return {"role": "assistant", "content": f"Summary: reviewed the request ({preview!s}). No further action needed."}


@app.get("/v1/models")
async def models() -> dict:
    return {"object": "list", "data": [{"id": "mock-llm", "object": "model", "owned_by": "demo"}]}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": {"type": "invalid_request_error", "message": "body must be JSON"}}, 400)
    if body.get("stream"):
        return JSONResponse({"error": {"type": "invalid_request_error", "message": "streaming is not supported"}}, 400)

    messages = body.get("messages") or []
    scenario = request.headers.get("x-demo-scenario", "")
    agent = request.headers.get("x-agent-id", "")
    step = sum(1 for m in messages if m.get("role") == "assistant")
    request_id = stable_id("chatcmpl", scenario, agent, messages)

    script = load_script(scenario)
    steps = ((script or {}).get("agents") or {}).get(agent) or []
    if step < len(steps):
        message, finish = build_message(steps[step], step, request_id)
        matched = "true"
    else:
        # No script entry: generic summary, and say so loudly (7.5)
        message, finish, matched = generic_message(messages), "stop", "false"
        log.warning(
            "no mock-llm script entry; returning generic summary",
            extra={"scenario": scenario, "agent": agent, "step": step, "script_found": script is not None},
        )
    REQUESTS.labels(scenario or "none", agent or "none", matched).inc()

    prompt_tokens = count_tokens(messages)
    completion_tokens = count_tokens(message.get("content") or message.get("tool_calls") or "")
    log.info(
        "chat completion",
        extra={"scenario": scenario, "agent": agent, "step": step, "finish_reason": finish, "matched": matched},
    )
    return JSONResponse(
        {
            "id": request_id,
            "object": "chat.completion",
            "created": 0,  # fixed: responses are byte-identical for identical requests
            "model": body.get("model") or "mock-llm",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
    )


def main() -> None:
    run(app)
