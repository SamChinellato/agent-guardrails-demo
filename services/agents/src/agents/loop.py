"""The agent loop (7.7): list tools, call the LLM with them, run the tool calls, repeat until
the model answers or max_steps. Every call goes through the gateway.

Gateway answers become outcomes, never crashes:
  LLM 400/403        prompt refused by the gateway's guards -> status blocked_by_policy
  tool -32001        tool call denied by policy -> told to the model, loop continues
  tool quarantined   result withheld by the guard -> told to the model, loop continues
  503 kill switch    stop at once -> status halted_by_kill_switch (KS-06)
"""

import json
import os
import uuid
from dataclasses import dataclass, field

import httpx
import openai
from opentelemetry import propagate, trace
from prometheus_client import Counter

from agents import obo
from agents.tools import GatewayMcp, KillSwitchEngaged
from demo_common import get_logger

log = get_logger("agents.loop")
tracer = trace.get_tracer("agents")

TOKENS = Counter("agent_llm_tokens_total", "LLM tokens used, as reported by the model", ["agent", "direction"])
TOOL_CALLS = Counter("agent_tool_calls_total", "Tool calls by outcome", ["agent", "tool", "outcome"])
TASKS = Counter("agent_tasks_total", "Tasks by final status", ["agent", "status"])

MAX_STEPS_CAP = 150  # ATK-04: a scenario may raise the step limit


@dataclass
class Config:
    agent_id: str
    gateway: str
    api_key: str
    servers: list[str]
    model: str
    max_steps: int
    system_prompt: str

    @classmethod
    def from_env(cls, agent_id: str, system_prompt: str) -> "Config":
        return cls(
            agent_id=agent_id,
            gateway=os.environ.get("GATEWAY_URL", "http://apisix-gateway.gateway.svc.cluster.local").rstrip("/"),
            api_key=os.environ["AGENT_API_KEY"],
            servers=[s for s in os.environ.get("MCP_SERVERS", "").split(",") if s],
            model=os.environ.get("LLM_MODEL", "demo"),
            max_steps=int(os.environ.get("MAX_STEPS", "6")),
            system_prompt=system_prompt,
        )


@dataclass
class Result:
    status: str  # completed | blocked_by_policy | halted_by_kill_switch | max_steps | error
    reply: str
    trace_id: str
    task_id: str
    events: list[dict] = field(default_factory=list)
    identity: list[dict] = field(default_factory=list)   # OBO token summaries, this agent's first (D-072)

    def as_dict(self) -> dict:
        return {"status": self.status, "reply": self.reply, "trace_id": self.trace_id,
                "task_id": self.task_id, "events": self.events, "identity": self.identity}


def kill_error(e: openai.APIStatusError) -> bool:
    try:
        return e.status_code == 503 and e.response.json()["error"]["type"] == "kill_switch_engaged"
    except Exception:
        return False


class Agent:
    """local_tools: extra tools the agent runs itself, {name: (definition, async handler)}.
    A handler returns (outcome, text, nested_events)."""

    def __init__(self, cfg: Config, local_tools: dict | None = None):
        self.cfg = cfg
        self.local_tools = local_tools or {}

    def _system(self) -> str:
        # qwen3 reasons at length unless told not to; that costs ~25 s per call on CPU (D-018)
        prefix = "/no_think\n" if "qwen3" in self.cfg.model.lower() else ""
        return prefix + self.cfg.system_prompt

    async def run(self, messages: list[dict], scenario: str, max_steps: int | None = None,
                  local_tools: bool = True, obo_token: str | None = None) -> Result:
        """obo_token: this agent's token for the person it acts for (D-072). It goes on tool
        calls only, where the guard checks it, and to the hand-off; never to the LLM."""
        task_id = "task-" + uuid.uuid4().hex[:12]
        steps = min(max_steps or self.cfg.max_steps, MAX_STEPS_CAP)
        headers = {"X-Demo-Scenario": scenario} if scenario else {}
        obo.current.set(obo_token)
        with tracer.start_as_current_span(
            "agent.task",
            attributes={"agent.id": self.cfg.agent_id, "scenario.id": scenario or "", "task.id": task_id},
        ) as span:
            trace_id = format(span.get_span_context().trace_id, "032x")
            if obo_token:
                me = obo.summary(self.cfg.agent_id, obo_token)
                span.set_attribute("agent.acting_for", me["user"] or "")
            result = await self._run(messages, headers, steps, task_id, trace_id, scenario, local_tools, obo_token)
            # The executor's identity comes back from the hand-off as events; move it across
            nested = [e for e in result.events if e.get("type") == "identity"]
            result.events = [e for e in result.events if e.get("type") != "identity"]
            result.identity = ([me] if obo_token else []) + [{k: v for k, v in e.items() if k != "type"} for e in nested]
            span.set_attribute("agent.status", result.status)
            TASKS.labels(self.cfg.agent_id, result.status).inc()
            log.info("task finished", extra={"task_id": task_id, "status": result.status, "scenario": scenario,
                                             "events": len(result.events)})
            return result

    async def _run(self, messages, headers, steps, task_id, trace_id, scenario, local_tools=True, obo_token=None) -> Result:
        events: list[dict] = []
        convo = [{"role": "system", "content": self._system()}, *messages]
        llm = openai.AsyncOpenAI(
            base_url=f"{self.cfg.gateway}/llm/v1", api_key="via-gateway",
            default_headers={"apikey": self.cfg.api_key, **headers}, timeout=150, max_retries=0,
        )

        def done(status: str, reply: str) -> Result:
            return Result(status, reply, trace_id, task_id, events)

        try:
            mcp_headers = {**headers, "X-Obo-Token": obo_token} if obo_token else headers
            async with GatewayMcp(self.cfg.gateway, self.cfg.api_key, self.cfg.servers, mcp_headers) as mcp:
                tools = await mcp.openai_tools() + ([d for d, _ in self.local_tools.values()] if local_tools else [])
                for _ in range(steps):
                    msg = await self._chat(llm, convo, tools, events)
                    if isinstance(msg, Result):  # refused by the gateway
                        msg.trace_id, msg.task_id = trace_id, task_id
                        return msg
                    convo.append(msg.model_dump(exclude_none=True))
                    if not msg.tool_calls:
                        return done("completed", msg.content or "")
                    for call in msg.tool_calls:
                        text = await self._tool(mcp, call, scenario, events)
                        convo.append({"role": "tool", "tool_call_id": call.id, "content": text})
                return done("max_steps", "Stopped: step limit reached.")
        except KillSwitchEngaged:
            events.append({"type": "halted", "detail": "kill switch engaged"})
            return done("halted_by_kill_switch", "Halted: the gateway's kill switch is engaged for this agent.")
        except openai.APIStatusError as e:
            if kill_error(e):
                events.append({"type": "halted", "detail": "kill switch engaged"})
                return done("halted_by_kill_switch", "Halted: the gateway's kill switch is engaged for this agent.")
            log.error("LLM call failed", extra={"status": e.status_code})
            return done("error", f"LLM call failed ({e.status_code})")
        except (openai.APIError, httpx.HTTPError) as e:
            log.error("gateway unreachable", extra={"error": str(e)})
            return done("error", "The gateway could not be reached.")

    async def _chat(self, llm, convo, tools, events):
        with tracer.start_as_current_span("gen_ai.chat", attributes={
            "gen_ai.system": "openai", "gen_ai.operation.name": "chat", "gen_ai.request.model": self.cfg.model,
        }) as span:
            # The OpenAI SDK's HTTP client isn't covered by the httpx auto-instrumentation,
            # so the trace context is injected explicitly (OBS-01)
            carrier: dict[str, str] = {}
            propagate.inject(carrier)
            try:
                r = await llm.chat.completions.create(
                    model=self.cfg.model, messages=convo, tools=tools or openai.NOT_GIVEN,
                    temperature=0, seed=42, stream=False, extra_headers=carrier,
                )
            except openai.APIStatusError as e:
                if e.status_code in (400, 403) and not kill_error(e):
                    detail = _error_body(e)
                    events.append({"type": "blocked", "where": "llm", **detail})
                    span.set_attribute("gen_ai.blocked", True)
                    return Result("blocked_by_policy", "The gateway blocked this request.", "", "", events)
                raise
            usage = r.usage
            if usage:
                span.set_attribute("gen_ai.usage.input_tokens", usage.prompt_tokens)
                span.set_attribute("gen_ai.usage.output_tokens", usage.completion_tokens)
                TOKENS.labels(self.cfg.agent_id, "input").inc(usage.prompt_tokens)
                TOKENS.labels(self.cfg.agent_id, "output").inc(usage.completion_tokens)
            span.set_attribute("gen_ai.response.model", r.model or "")
            span.set_attribute("gen_ai.response.finish_reasons", [c.finish_reason or "" for c in r.choices])
            return r.choices[0].message

    async def _tool(self, mcp, call, scenario, events) -> str:
        name = call.function.name
        try:
            args = json.loads(call.function.arguments or "{}")
        except ValueError:
            args = {}
        server = name.partition("__")[0] if "__" in name else "local"
        with tracer.start_as_current_span("mcp.tool_call", attributes={"mcp.tool": name, "mcp.server": server}) as span:
            nested: list[dict] = []
            if name in self.local_tools:
                outcome, text, nested = await self.local_tools[name][1](args, scenario)
                decision = None
            else:
                try:
                    r = await mcp.call(name, args)
                except KillSwitchEngaged:
                    span.set_attribute("mcp.decision", "halted")
                    raise
                outcome, text, decision = r.outcome, r.text, r.decision_id
            span.set_attribute("mcp.decision", outcome)
            if decision:
                span.set_attribute("guard.decision_id", decision)
        TOOL_CALLS.labels(self.cfg.agent_id, name, outcome).inc()
        events.append({"type": "tool", "agent": self.cfg.agent_id, "tool": name, "outcome": outcome,
                       **({"decision_id": decision} if decision else {})})
        events.extend(nested)
        if outcome == "denied":
            return "Not done: " + plain_denial(text)
        return text


def plain_denial(message: str) -> str:
    """The gateway's denial in a plain sentence for the model."""
    m = message.lower()
    if "recipient domain" in m:
        return "emails can only go to demo.local addresses."
    if "fields" in m and "may not be changed" in m:
        return "only the status and notes fields can be changed."
    return "that action is not available to this agent."


def _error_body(e: openai.APIStatusError) -> dict:
    try:
        err = e.response.json().get("error") or {}
        if isinstance(err, dict):
            return {"status": e.status_code, "reason": err.get("type") or err.get("message"),
                    "decision_id": err.get("decision_id")}
    except Exception:
        pass
    try:
        return {"status": e.status_code, "reason": e.response.json().get("message")}
    except Exception:
        return {"status": e.status_code, "reason": e.response.text[:100]}
