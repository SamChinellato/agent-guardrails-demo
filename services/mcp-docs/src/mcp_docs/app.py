"""MCP server: supplier documents (7.6). Tools: list_documents, read_document.
Documents are synthetic fixtures; supplier-07 is poisoned on purpose (ATK-02)."""

import os
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer

from demo_common import get_logger, run
from demo_common.mcp import mcp_app, tool_span

DOCS_DIR = Path(os.environ.get("DOCS_DIR", "/fixtures/documents"))
log = get_logger("mcp_docs")
server = MCPServer("mcp-docs", instructions="Read-only store of supplier review documents.")


def _docs() -> dict[str, Path]:
    return {p.stem: p for p in sorted(DOCS_DIR.glob("*.md"))}


def _title(path: Path) -> str:
    first = path.read_text().splitlines()[0] if path.stat().st_size else path.stem
    return first.lstrip("# ").strip()


@server.tool(description="List the supplier review documents that can be read.")
def list_documents(ctx: Context) -> list[dict]:
    with tool_span(ctx, "list_documents", "docs"):
        docs = [{"id": doc_id, "title": _title(p)} for doc_id, p in _docs().items()]
        log.info("tool call", extra={"tool": "list_documents", "count": len(docs)})
        return docs


@server.tool(description="Read one supplier review document by its id, e.g. supplier-01.")
def read_document(id: str, ctx: Context) -> str:
    with tool_span(ctx, "read_document", "docs") as span:
        span.set_attribute("document.id", id)
        path = _docs().get(id)
        log.info("tool call", extra={"tool": "read_document", "document_id": id, "found": path is not None})
        if path is None:
            raise ValueError(f"no document with id {id!r}")
        return path.read_text()


app = mcp_app(server, "mcp-docs")


def main() -> None:
    run(app)
