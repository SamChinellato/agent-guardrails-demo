"""MCP inspecting proxy (GRD-07, F3). Streamable-HTTP JSON-RPC, JSON responses only.

  initialize, notifications, ping, ...  passed through unchanged
  tools/list   result filtered to the agent's allowlist (GRD-03)
  tools/call   allowlist and argument rules before forwarding (GRD-03/04);
               on block -> JSON-RPC error -32001 with decision_id
               tool result scanned for injection (mcp.tool_result_injection);
               on block -> result replaced by isError + "Content quarantined ..."
  SSE responses from upstream are rejected (servers are JSON-only by configuration).
"""

import json
import os

import httpx
from fastapi import Request, Response
from fastapi.concurrency import run_in_threadpool

from demo_common import get_logger
from guard.engine import Engine, Subject

log = get_logger("guard.mcp")

UPSTREAMS = {
    "docs": os.environ.get("MCP_DOCS_URL", "http://mcp-docs.ai.svc.cluster.local:8000/mcp"),
    "actions": os.environ.get("MCP_ACTIONS_URL", "http://mcp-actions.ai.svc.cluster.local:8000/mcp"),
}
FORWARD_HEADERS = ("content-type", "accept", "mcp-protocol-version", "mcp-session-id", "x-agent-id", "x-demo-scenario")
BLOCKED = -32001


def rpc_error(msg_id, code: int, message: str, data: dict | None = None, status: int = 200) -> Response:
    body = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
    if data:
        body["error"]["data"] = data
    return Response(json.dumps(body), status_code=status, media_type="application/json")


class McpProxy:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5))

    async def handle(self, server: str, request: Request) -> Response:
        if server not in UPSTREAMS:
            return rpc_error(None, -32601, f"unknown MCP server {server!r}", status=404)
        raw = await request.body()
        try:
            msg = json.loads(raw)
        except ValueError:
            return rpc_error(None, -32700, "parse error", status=400)
        if not isinstance(msg, dict):
            return rpc_error(None, -32600, "batch requests are not supported", status=400)

        agent = request.headers.get("x-agent-id", "")
        method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        route = f"mcp-{server}"

        if method == "tools/call":
            tool = f"{server}.{params.get('name', '')}"
            args = params.get("arguments") or {}
            for check in ("mcp.tool_allowlist", "mcp.argument_rules"):
                d = await run_in_threadpool(
                    self.engine.evaluate, Subject(check, agent, route, tool=tool, arguments=args)
                )
                if d and d.verdict == "block":
                    return rpc_error(msg_id, BLOCKED, f"Blocked by policy: {d.reason}",
                                     {"decision_id": d.decision_id, "check": d.check})

        upstream = await self._forward(server, raw, request)
        if isinstance(upstream, Response):
            return upstream
        status, headers, body = upstream
        if body is None or method not in ("tools/list", "tools/call") or "result" not in body:
            return Response(json.dumps(body) if body is not None else b"", status_code=status, headers=headers)

        if method == "tools/list":
            body["result"]["tools"] = self._visible_tools(server, agent, body["result"].get("tools", []))
        elif method == "tools/call":
            body["result"] = await self._scan_result(server, agent, route, params.get("name", ""), body["result"])
        return Response(json.dumps(body), status_code=status, headers=headers)

    async def _forward(self, server: str, raw: bytes, request: Request):
        headers = {h: request.headers[h] for h in FORWARD_HEADERS if h in request.headers}
        try:
            r = await self.client.post(UPSTREAMS[server], content=raw, headers=headers)
        except httpx.HTTPError as e:
            log.error("MCP upstream unreachable", extra={"server": server, "error": str(e)})
            return rpc_error(None, -32603, f"MCP server {server} unreachable", status=502)
        ctype = r.headers.get("content-type", "")
        if ctype.startswith("text/event-stream"):
            return rpc_error(None, -32603, "SSE responses are not supported by the gateway", status=502)
        out_headers = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "mcp-session-id")}
        if not r.content:
            return Response(b"", status_code=r.status_code, headers=out_headers)
        try:
            return r.status_code, out_headers, r.json()
        except ValueError:
            return rpc_error(None, -32603, "invalid JSON from MCP server", status=502)

    def _visible_tools(self, server: str, agent: str, tools: list[dict]) -> list[dict]:
        policy = self.engine.store.policy
        if policy.check("mcp.tool_allowlist").mode != "enforce":
            return tools
        allowed = policy.agent(agent).tools - policy.deny_tools_all
        return [t for t in tools if f"{server}.{t.get('name')}" in allowed]

    async def _scan_result(self, server: str, agent: str, route: str, name: str, result: dict) -> dict:
        text = "\n\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if not text:
            return result
        d = await run_in_threadpool(
            self.engine.evaluate,
            Subject("mcp.tool_result_injection", agent, route, content=text, tool=f"{server}.{name}"),
        )
        if d and d.verdict == "block":
            return {
                "content": [{"type": "text", "text": f"Content quarantined by policy (decision {d.decision_id})"}],
                "isError": True,
            }
        return result
