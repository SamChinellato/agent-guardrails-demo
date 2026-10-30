"""MCP tools through the gateway (F3), with the official MCP SDK.

The SDK's HTTP client is httpx2, which OTel's httpx instrumentation does not see, so the
current trace context and the demo headers are injected with a request hook (OBS-01).
Gateway outcomes are mapped to plain results the agent loop understands:
  ok | denied (-32001, policy) | quarantined (isError from the guard) | error | halted (kill switch)
"""

import contextlib
import json
from dataclasses import dataclass

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from opentelemetry import propagate

KILL_CODE = -32003


class KillSwitchEngaged(Exception):
    """The gateway refused the call because a kill switch is tripped (KS-06)."""


@dataclass
class ToolResult:
    outcome: str  # ok | denied | quarantined | error
    text: str
    decision_id: str | None = None
    check: str | None = None  # which guard check denied it, e.g. mcp.user_entitlement (D-072)


def _is_kill(e: BaseException) -> bool:
    if isinstance(e, MCPError):
        return e.error.code == KILL_CODE or "kill_switch_engaged" in (e.error.message or "")
    if isinstance(e, BaseExceptionGroup):
        return any(_is_kill(x) for x in e.exceptions)
    return False


def _mcp_errors(e: BaseException) -> list[MCPError]:
    if isinstance(e, MCPError):
        return [e]
    if isinstance(e, BaseExceptionGroup):
        return [m for x in e.exceptions for m in _mcp_errors(x)]
    return []


class GatewayMcp:
    """One stateless MCP session per server for the duration of a task."""

    def __init__(self, gateway: str, api_key: str, servers: list[str], headers: dict[str, str]):
        self.gateway, self.api_key, self.servers, self.headers = gateway, api_key, servers, headers
        self.sessions: dict[str, ClientSession] = {}
        self._stack = contextlib.AsyncExitStack()

    async def _inject(self, request) -> None:
        carrier: dict[str, str] = {}
        propagate.inject(carrier)
        for k, v in carrier.items():
            request.headers[k] = v

    async def __aenter__(self) -> "GatewayMcp":
        try:
            for server in self.servers:
                client = httpx2.AsyncClient(
                    headers={"apikey": self.api_key, **self.headers},
                    event_hooks={"request": [self._inject]},
                    timeout=60,
                )
                streams = await self._stack.enter_async_context(
                    streamable_http_client(f"{self.gateway}/mcp/{server}", http_client=client)
                )
                session = await self._stack.enter_async_context(ClientSession(streams[0], streams[1]))
                await session.initialize()
                self.sessions[server] = session
        except BaseException as e:
            await self._stack.aclose()
            if _is_kill(e):
                raise KillSwitchEngaged(str(e)) from None
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        with contextlib.suppress(BaseException):
            await self._stack.aclose()

    async def openai_tools(self) -> list[dict]:
        """The agent's visible tools as OpenAI function definitions, named server__tool.
        The gateway has already filtered tools/list to this agent's allowlist (GRD-03)."""
        out = []
        for server, session in self.sessions.items():
            try:
                listed = await session.list_tools()
            except BaseException as e:
                if _is_kill(e):
                    raise KillSwitchEngaged(str(e)) from None
                raise
            for t in listed.tools:
                out.append({
                    "type": "function",
                    "function": {
                        "name": f"{server}__{t.name}",
                        "description": t.description or t.name,
                        "parameters": t.input_schema if hasattr(t, "input_schema") else t.inputSchema,
                    },
                })
        return out

    async def call(self, qualified: str, arguments: dict) -> ToolResult:
        server, _, tool = qualified.partition("__")
        session = self.sessions.get(server)
        if session is None:
            return ToolResult("error", f"unknown tool {qualified}")
        try:
            r = await session.call_tool(tool, arguments)
        except BaseException as e:
            if _is_kill(e):
                raise KillSwitchEngaged(str(e)) from None
            for m in _mcp_errors(e):
                data = m.error.data if isinstance(m.error.data, dict) else {}
                if m.error.code == -32001:
                    return ToolResult("denied", m.error.message, data.get("decision_id"), data.get("check"))
                return ToolResult("error", m.error.message)
            return ToolResult("error", f"{type(e).__name__}: {e}")
        text = "\n".join(getattr(c, "text", "") for c in r.content) or json.dumps(r.structured_content or {})
        if r.is_error:
            if text.startswith("Content quarantined by policy"):
                decision = text.rsplit("decision ", 1)[-1].rstrip(")")
                return ToolResult("quarantined", text, decision)
            return ToolResult("error", text)
        return ToolResult("ok", text)
