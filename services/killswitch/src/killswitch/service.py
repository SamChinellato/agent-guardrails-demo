"""Trip / reset logic (KS-01..07, KS-12). The only runtime caller of the Admin API (GW-07)."""

import os
import threading
import time

import httpx
from opentelemetry import trace
from prometheus_client import Counter, Gauge

from demo_common import get_logger
from killswitch.apisix import AGENTS, Apisix
from killswitch.audit import AuditLog

log = get_logger("killswitch")
tracer = trace.get_tracer("killswitch")

ACTIVE = Gauge("killswitch_active", "1 while a kill-switch scope is engaged", ["scope"])
EVENTS = Counter("killswitch_events_total", "Kill-switch events", ["type", "scope", "source", "verified"])

GATEWAY = os.environ.get("GATEWAY_URL", "http://apisix-gateway.gateway.svc.cluster.local").rstrip("/")
PROBE_KEY = os.environ.get("PROBE_KEY", "")
SCOPES = ["global", "tools", *(f"agent:{a}" for a in AGENTS)]


class Rejected(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class KillSwitch:
    def __init__(self, audit: AuditLog):
        self.apisix = Apisix()
        self.audit = audit
        self._lock = threading.Lock()
        self.last_reset: dict[str, float] = {}  # scope -> ts, for the Alertmanager startsAt rule
        for e in audit.read():
            if e["type"] == "reset":
                self.last_reset[e["scope"]] = e["ts"]

    # ------------------------------------------------------------------ verification
    def _probe(self, scope: str) -> int | None:
        """A synthetic request through the gateway as consumer probe (KS-04)."""
        if scope == "global":
            r = httpx.post(f"{GATEWAY}/llm/v1/chat/completions", timeout=5,
                           json={"model": "probe", "messages": [{"role": "user", "content": "probe"}]},
                           headers={"apikey": PROBE_KEY, "X-Demo-Scenario": "smoke"})
        elif scope == "tools":
            r = httpx.post(f"{GATEWAY}/mcp/docs", timeout=5,
                           json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                           headers={"apikey": PROBE_KEY, "accept": "application/json, text/event-stream"})
        else:
            return None  # agent scope: read-back only (KS-04)
        return r.status_code

    def _verify(self, scope: str, engaged: bool, deadline_s: float = 5) -> tuple[bool, int | None]:
        """Read back from APISIX, then probe, until both agree or the deadline passes."""
        end = time.monotonic() + deadline_s
        probe = None
        while True:
            try:
                ok = self.apisix.is_tripped(scope) == engaged
                if ok:
                    probe = self._probe(scope)
                    ok = probe is None or ((probe == 503) == engaged)
            except (httpx.HTTPError, Exception) as e:  # noqa: BLE001 - verification must not crash the request
                log.warning("verification attempt failed", extra={"scope": scope, "error": str(e)})
                ok = False
            if ok or time.monotonic() > end:
                return ok, probe
            time.sleep(0.1)

    # ------------------------------------------------------------------ actions
    def trip(self, scope: str, reason: str, actor: str, source: str,
             trace_ids: list[str] | None = None, decision_id: str | None = None) -> dict:
        self._check_scope(scope)
        start = time.monotonic()
        with self._lock, tracer.start_as_current_span("killswitch.trip", attributes={
            "killswitch.scope": scope, "killswitch.source": source, "killswitch.actor": actor,
        }) as span:
            now = time.time()
            incident_id, new = self.audit.incident_for(scope, now)
            already = self.apisix.is_tripped(scope)
            changes = [] if already else self.apisix.set_scope(scope, True, incident_id)
            verified, probe = self._verify(scope, True)
            event = self._record("trip", scope, reason, actor, source, incident_id, trace_ids, decision_id,
                                 changes, verified, probe, start, already=already, new_incident=new)
            span.set_attribute("killswitch.incident_id", incident_id)
            span.set_attribute("killswitch.verified", verified)
        self.refresh_gauges()
        if not verified:
            raise Rejected(500, f"trip of {scope} could not be verified")
        return event

    def reset(self, scope: str, reason: str, actor: str, source: str = "manual") -> dict:
        self._check_scope(scope)
        start = time.monotonic()
        with self._lock, tracer.start_as_current_span("killswitch.reset", attributes={
            "killswitch.scope": scope, "killswitch.actor": actor,
        }):
            already = not self.apisix.is_tripped(scope)
            changes = [] if already else self.apisix.set_scope(scope, False)
            verified, probe = self._verify(scope, False)
            event = self._record("reset", scope, reason, actor, source, None, None, None,
                                 changes, verified, probe, start, already=already)
            self.last_reset[scope] = event["ts"]
        self.refresh_gauges()
        if not verified:
            raise Rejected(500, f"reset of {scope} could not be verified")
        return event

    def _record(self, kind, scope, reason, actor, source, incident_id, trace_ids, decision_id,
                changes, verified, probe, start, already=False, new_incident=None) -> dict:
        event = self.audit.append({
            "ts": time.time(),
            "type": kind,
            "scope": scope,
            "target": scope,
            "reason": reason,
            "actor": actor,
            "source": source,
            "incident_id": incident_id,
            "trace_ids": trace_ids or [],
            "decision_id": decision_id,
            "apisix_changes": changes,
            "no_change": already,  # KS-03: already in that state; recorded, nothing changed
            "verified": verified,
            "probe_status": probe,
            "latency_ms": round((time.monotonic() - start) * 1000, 1),
            "control_refs": [],  # EVD-07 placeholder
        })
        EVENTS.labels(kind, scope, source, str(verified).lower()).inc()
        log.warning("kill switch " + kind, extra={
            "event_type": kind, "scope": scope, "actor": actor, "source": source, "reason": reason,
            "incident_id": incident_id, "decision_id": decision_id, "verified": verified,
            "latency_ms": event["latency_ms"], "no_change": already, "event_id": event["event_id"],
        })
        return event

    @staticmethod
    def _check_scope(scope: str) -> None:
        if scope not in SCOPES:
            raise Rejected(400, f"unknown scope {scope!r}; one of {SCOPES}")

    def status(self) -> dict:
        s = self.apisix.status()
        return {**s, "checked_at": time.time()}

    def refresh_gauges(self) -> None:
        try:
            for scope, on in self.apisix.status()["scopes"].items():
                ACTIVE.labels(scope).set(1 if on else 0)
        except Exception as e:  # noqa: BLE001
            log.warning("cannot read kill-switch state", extra={"error": str(e)})

    # ------------------------------------------------------------------ Alertmanager (F6)
    def from_alertmanager(self, payload: dict) -> list[dict]:
        """Trip the scope in each firing alert's killswitch_scope label. An alert that began
        before that scope's last reset is not acted on again (D-037)."""
        results = []
        for alert in payload.get("alerts", []):
            labels = alert.get("labels", {})
            scope = labels.get("killswitch_scope")
            if alert.get("status") != "firing" or not scope:
                continue
            starts = _parse_ts(alert.get("startsAt"))
            if starts and starts <= self.last_reset.get(scope, 0):
                results.append({"scope": scope, "skipped": "alert began before the last reset"})
                continue
            name = labels.get("alertname", "alert")
            summary = (alert.get("annotations") or {}).get("summary", "")
            try:
                e = self.trip(scope, f"{name}: {summary}".strip(": "), actor="alertmanager", source="alertmanager")
                results.append({"scope": scope, "incident_id": e["incident_id"], "verified": e["verified"]})
            except Rejected as r:
                results.append({"scope": scope, "error": str(r)})
        return results


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
