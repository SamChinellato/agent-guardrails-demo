"""MCP server: simulated business actions (7.6). Nothing leaves the cluster:
  update_record         changes an in-memory supplier store seeded from records.json
  send_email            appends to an outbox file; no mail is ever sent
  export_customer_data  returns synthetic customer rows
The gateway's guard decides who may call what; this server enforces nothing itself, so the
demo shows that the controls live in the gateway. GET/DELETE /outbox are cluster-internal
(not routed by APISIX) and used by tests and `make reset`."""

import copy
import csv
import io
import json
import os
import threading
import time
from pathlib import Path

from fastapi import Response
from mcp.server.mcpserver import Context, MCPServer

from demo_common import get_logger, run
from demo_common.mcp import mcp_app, tool_span

RECORDS_FILE = Path(os.environ.get("RECORDS_FILE", "/fixtures/records.json"))
OUTBOX = Path(os.environ.get("OUTBOX_FILE", "/data/outbox.jsonl"))
log = get_logger("mcp_actions")
server = MCPServer("mcp-actions", instructions="Supplier record updates, email outbox and data export (simulated).")

_lock = threading.Lock()
_seed = json.loads(RECORDS_FILE.read_text()) if RECORDS_FILE.exists() else {"suppliers": {}, "customers": []}
_suppliers: dict[str, dict] = copy.deepcopy(_seed["suppliers"])


@server.tool(description="Update fields on a supplier record, e.g. {\"status\": \"watch\", \"notes\": \"...\"}.")
def update_record(id: str, fields: dict, ctx: Context) -> dict:
    with tool_span(ctx, "update_record", "actions") as span:
        span.set_attribute("record.id", id)
        with _lock:
            if id not in _suppliers:
                raise ValueError(f"no supplier record {id!r}")
            _suppliers[id].update({k: str(v) for k, v in fields.items()})
            record = {"id": id, **_suppliers[id]}
        log.info("tool call", extra={"tool": "update_record", "record_id": id, "fields": sorted(fields)})
        return record


@server.tool(description="Queue an email in the outbox (simulated; nothing is sent).")
def send_email(to: str, subject: str, body: str, ctx: Context) -> dict:
    with tool_span(ctx, "send_email", "actions") as span:
        span.set_attribute("email.to", to)
        entry = {"ts": time.time(), "to": to, "subject": subject, "body_chars": len(body)}
        with _lock:
            OUTBOX.parent.mkdir(parents=True, exist_ok=True)
            with OUTBOX.open("a") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
        log.info("tool call", extra={"tool": "send_email", "to": to})
        return {"queued": True, "to": to}


@server.tool(description="Export customer records (synthetic) as csv or json.")
def export_customer_data(format: str = "csv", ctx: Context = None) -> str:
    with tool_span(ctx, "export_customer_data", "actions"):
        rows = _seed["customers"]
        log.info("tool call", extra={"tool": "export_customer_data", "rows": len(rows), "format": format})
        if format == "json":
            return json.dumps(rows)
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(rows[0]) if rows else ["id"])
        writer.writeheader()
        writer.writerows(rows)
        return out.getvalue()


def _internal_routes(app) -> None:
    @app.get("/outbox", include_in_schema=False)
    async def outbox() -> Response:
        lines = OUTBOX.read_text().splitlines() if OUTBOX.exists() else []
        return Response(json.dumps([json.loads(line) for line in lines]), media_type="application/json")

    @app.delete("/outbox", include_in_schema=False)
    async def clear_outbox() -> dict:
        """Reset for `make reset` (REP-07): empty outbox, records back to the seed."""
        with _lock:
            OUTBOX.unlink(missing_ok=True)
            _suppliers.clear()
            _suppliers.update(copy.deepcopy(_seed["suppliers"]))
        return {"cleared": True}

    @app.get("/records", include_in_schema=False)
    async def records() -> dict:
        return _suppliers


app = mcp_app(server, "mcp-actions", setup=_internal_routes)


def main() -> None:
    run(app)
