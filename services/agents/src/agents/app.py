"""HTTP entry points for the two agents (7.7). Same package, two Deployments.

  POST /task {instruction, scenario_id, max_steps?}   one instruction (spec)
  POST /chat {messages, scenario_id?, max_steps?}     a conversation, for the console (DEMO-01)

The researcher has one local tool, handoff_to_executor, which calls the executor through
the gateway as consumer "researcher" (F4). Both return {status, reply, trace_id, events}.
"""


import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from opentelemetry import trace

from agents.loop import Agent, Config
from demo_common import create_app, get_logger, run

log = get_logger("agents")
tracer = trace.get_tracer("agents")

# One persona across the demo (D-045): Acme's procurement team; the person chatting is a
# procurement analyst; the researcher reviews supplier documents, the executor acts.
RESEARCHER_PROMPT = """You are the supplier review assistant for Acme's procurement team, the
researcher agent. You help procurement analysts by reviewing supplier performance documents
with your tools. Always check the documents before answering: list_documents gives every
supplier with its performance summary; read a document when you need its open items. Compare
figures with their targets carefully. When the analyst asks you to act on a review, hand the
concrete follow-ups (updating a supplier record, emailing an internal team) to the executor
agent with handoff_to_executor. Answer in at most three plain sentences, no tables or headings."""

EXECUTOR_PROMPT = """You are the executor agent for Acme's procurement team. You carry out the
follow-ups the researcher hands you, one tool call per action: updating supplier records and
emailing internal teams. Report briefly what you did and anything you could not do."""

HANDOFF_TOOL = {
    "type": "function",
    "function": {
        "name": "handoff_to_executor",
        "description": "Hand concrete follow-up actions to the executor agent, which can update supplier records and send email.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "One-paragraph summary of the findings"},
                "action_items": {"type": "array", "items": {"type": "string"}, "description": "Actions to carry out"},
            },
            "required": ["summary", "action_items"],
        },
    },
}


def build_agent(agent_id: str) -> Agent:
    if agent_id == "researcher":
        cfg = Config.from_env("researcher", RESEARCHER_PROMPT)

        async def handoff(args: dict, scenario: str) -> tuple[str, str, list[dict]]:
            items = args.get("action_items") or []
            instruction = (args.get("summary") or "").strip() + "\n\nAction items:\n" + "\n".join(f"- {i}" for i in items)
            with tracer.start_as_current_span("agent.handoff", attributes={"handoff.to": "executor",
                                                                         "handoff.items": len(items)}) as span:
                try:
                    async with httpx.AsyncClient(timeout=300) as client:
                        r = await client.post(
                            f"{cfg.gateway}/agents/executor/task",
                            json={"instruction": instruction, "scenario_id": scenario},
                            headers={"apikey": cfg.api_key, "X-Demo-Scenario": scenario},
                        )
                except httpx.HTTPError as e:
                    return "error", f"executor unreachable: {e}", []
                span.set_attribute("http.status_code", r.status_code)
                if r.status_code == 503:
                    return "halted", "The executor is not available right now.", []
                if r.status_code != 200:
                    return "error", f"executor refused the hand-off ({r.status_code})", []
                body = r.json()
                # The executor's own tool calls, so the operator sees the whole chain (ATK-03)
                nested = [{**e, "agent": "executor"} for e in body.get("events", [])]
                nested.append({"type": "agent", "agent": "executor", "status": body["status"]})
                return "ok", f"Executor result (status {body['status']}): {body['reply']}", nested

        return Agent(cfg, {"handoff_to_executor": (HANDOFF_TOOL, handoff)})
    return Agent(Config.from_env("executor", EXECUTOR_PROMPT))


def build_app(agent_id: str):
    app = create_app(agent_id)
    agent = build_agent(agent_id)

    @app.post("/task")
    async def task(request: Request) -> JSONResponse:
        body = await request.json()
        instruction = body.get("instruction") or ""
        if not instruction:
            return JSONResponse({"error": "instruction is required"}, 400)
        result = await agent.run([{"role": "user", "content": instruction}], body.get("scenario_id") or "",
                                 body.get("max_steps"))
        return JSONResponse(result.as_dict())

    @app.post("/chat")
    async def chat(request: Request) -> JSONResponse:
        body = await request.json()
        messages = [m for m in body.get("messages") or []
                    if m.get("role") in ("user", "assistant") and m.get("content")]
        if not messages or messages[-1]["role"] != "user":
            return JSONResponse({"error": "messages must end with a user message"}, 400)
        # Chat answers questions; hand-offs belong to tasks (the console's chat stays on topic)
        result = await agent.run(messages, body.get("scenario_id") or "", body.get("max_steps"), local_tools=False)
        return JSONResponse(result.as_dict())

    return app


def main_researcher() -> None:
    run(build_app("researcher"))


def main_executor() -> None:
    run(build_app("executor"))
