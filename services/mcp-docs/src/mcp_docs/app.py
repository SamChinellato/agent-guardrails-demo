"""MCP server: supplier documents (7.6). Tools: list_documents, read_document.
Documents are synthetic fixtures; supplier-07 is poisoned on purpose (ATK-02).

Documents a person uploads in the console (D-047) join the store as upload-NN. They are not
scanned here: like every document, they are scanned by the guard when an agent reads one.
  POST   /uploads  {name, content}  -> {id, supplier}   (reached only through the guard)
  DELETE /uploads                   -> remove every upload (New chat)
"""

import os
import re
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from mcp.server.mcpserver import Context, MCPServer

from demo_common import get_logger, run
from demo_common.mcp import mcp_app, tool_span

DOCS_DIR = Path(os.environ.get("DOCS_DIR", "/fixtures/documents"))
UPLOADS_DIR = Path(os.environ.get("UPLOADS_DIR", "/uploads"))
MAX_UPLOAD_BYTES = 32 * 1024
MAX_UPLOADS = 10
log = get_logger("mcp_docs")
server = MCPServer("mcp-docs", instructions="Read-only store of supplier review documents.")


def _docs() -> dict[str, Path]:
    uploads = sorted(UPLOADS_DIR.glob("upload-*.md")) if UPLOADS_DIR.is_dir() else []
    return {p.stem: p for p in [*sorted(DOCS_DIR.glob("*.md")), *uploads]}


def _title(path: Path) -> str:
    first = path.read_text().splitlines()[0] if path.stat().st_size else path.stem
    return first.lstrip("# ").replace("Supplier review: ", "").strip()


def _summary(path: Path) -> str:
    """The visible status line and performance bullets only: never comments or other sections,
    so a document's hidden text can't reach the agent through the listing."""
    lines, in_perf, out = path.read_text().splitlines(), False, []
    for line in lines:
        if line.startswith("**Category:**"):
            out.append(line.replace("**", "").strip())
        elif line.startswith("## "):
            in_perf = line.startswith("## Performance")
        elif in_perf and line.startswith("- "):
            out.append(line[2:].strip())
    return "; ".join(out)


@server.tool(description="List the supplier review documents, each with its id, supplier name and a one-line performance summary.")
def list_documents(ctx: Context) -> list[dict]:
    with tool_span(ctx, "list_documents", "docs"):
        docs = [{"id": doc_id, "supplier": _title(p), "summary": _summary(p)} for doc_id, p in _docs().items()]
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


def _routes(app) -> None:
    @app.post("/uploads")
    async def upload(request: Request) -> JSONResponse:
        body = await request.json()
        content, name = body.get("content"), str(body.get("name") or "document")
        if not isinstance(content, str) or not content.strip():
            return JSONResponse({"error": "the document is empty"}, 400)
        if len(content.encode()) > MAX_UPLOAD_BYTES:
            return JSONResponse({"error": f"documents can be at most {MAX_UPLOAD_BYTES // 1024} KB"}, 413)
        existing = sorted(UPLOADS_DIR.glob("upload-*.md"))
        if len(existing) >= MAX_UPLOADS:
            return JSONResponse({"error": "too many uploads; start a new chat"}, 409)
        doc_id = f"upload-{len(existing) + 1:02d}"
        if not content.lstrip().startswith("#"):  # give it a title line, so the listing can name it
            title = re.sub(r"[^\w .-]", "", Path(name).stem)[:60] or "Uploaded document"
            content = f"# {title}\n\n{content}"
        path = UPLOADS_DIR / f"{doc_id}.md"
        path.write_text(content)
        log.info("document uploaded", extra={"document_id": doc_id, "bytes": len(content.encode())})
        return JSONResponse({"id": doc_id, "supplier": _title(path)})

    @app.delete("/uploads")
    async def clear_uploads() -> JSONResponse:
        removed = 0
        for p in UPLOADS_DIR.glob("upload-*.md"):
            p.unlink()
            removed += 1
        return JSONResponse({"removed": removed})


app = mcp_app(server, "mcp-docs", setup=_routes)


def main() -> None:
    run(app)
