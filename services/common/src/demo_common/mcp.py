"""Helpers for the MCP servers (7.6): streamable HTTP, stateless, JSON responses only,
mounted under the shared FastAPI app so /healthz, /readyz and /metrics still work."""

import contextlib
import os
from collections.abc import Iterator

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from opentelemetry import propagate, trace

from demo_common.app import create_app

tracer = trace.get_tracer("demo_common.mcp")


def mcp_app(server: MCPServer, service: str, setup=None):
    """FastAPI app serving MCP at /mcp. DNS-rebinding protection stays on, limited to the
    in-cluster names in MCP_ALLOWED_HOSTS (comma-separated host[:port]).
    setup(app) may add extra routes; it runs before the MCP mount, which catches every path."""
    hosts = [h for h in os.environ.get("MCP_ALLOWED_HOSTS", "localhost:8000,127.0.0.1:8000").split(",") if h]
    starlette = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with server.session_manager.run():
            yield

    app = create_app(service, lifespan=lifespan)
    if setup is not None:
        setup(app)
    app.mount("/", starlette)
    return app


@contextlib.contextmanager
def tool_span(ctx: Context, tool: str, server: str) -> Iterator[trace.Span]:
    """Span for one tool call, parented on the HTTP request's traceparent. Stateless MCP
    runs tools in the session manager's task group, outside the request's OTel context,
    so the context is taken from the request headers explicitly (OBS-01)."""
    request = getattr(ctx.request_context, "request", None)
    headers = dict(request.headers) if request is not None else {}
    parent = propagate.extract(headers)
    with tracer.start_as_current_span(
        f"mcp.server/{tool}",
        context=parent,
        kind=trace.SpanKind.INTERNAL,
        attributes={"mcp.tool": tool, "mcp.server": server, "agent.id": headers.get("x-agent-id", "")},
    ) as span:
        yield span
