"""Auto-trip (GRD-06, F5): a critical violation in enforce mode asks the killswitch to trip
scope agent:<id>. De-duplicated per agent for auto_trip.dedupe_seconds (default 60).
Runs on a background thread so the blocked request is answered immediately.

Until the killswitch exists (P5), KILLSWITCH_URL is unset and trips are only logged.
"""

import os
import threading
import time

import httpx
from opentelemetry import context as otel_context
from opentelemetry import trace

from demo_common import get_logger
from guard.engine import Decision
from guard.policy import PolicyStore

log = get_logger("guard.autotrip")
tracer = trace.get_tracer("guard.autotrip")


class AutoTrip:
    def __init__(self, store: PolicyStore):
        self.store = store
        self.url = os.environ.get("KILLSWITCH_URL", "").rstrip("/")
        self.token = os.environ.get("KILLSWITCH_TOKEN", "")
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def __call__(self, d: Decision) -> None:
        conf = self.store.policy.auto_trip
        if conf.get("on_severity", "critical") != d.severity:
            return
        scope = f"agent:{d.agent}" if conf.get("scope", "agent") == "agent" else conf["scope"]
        dedupe = float(conf.get("dedupe_seconds", 60))
        now = time.monotonic()
        with self._lock:
            if now - self._last.get(scope, -1e9) < dedupe:
                log.info("auto-trip suppressed (dedupe)", extra={"scope": scope, "decision_id": d.decision_id})
                return
            self._last[scope] = now
        ctx = otel_context.get_current()
        threading.Thread(target=self._trip, args=(scope, d, ctx), daemon=True).start()

    def _trip(self, scope: str, d: Decision, ctx) -> None:
        token = otel_context.attach(ctx)
        try:
            body = {
                "scope": scope,
                "reason": f"guard: {d.check} ({d.reason})",
                "actor": "guard",
                "source": "guard",
                "trace_ids": [d.trace_id] if d.trace_id else [],
                "decision_id": d.decision_id,
            }
            if not self.url:
                log.warning("auto-trip requested but KILLSWITCH_URL is unset", extra={"scope": scope, **body})
                return
            with tracer.start_as_current_span("guard.auto_trip", attributes={"killswitch.scope": scope}):
                r = httpx.post(
                    f"{self.url}/v1/trip", json=body, headers={"Authorization": f"Bearer {self.token}"}, timeout=5
                )
            log.warning(
                "auto-trip sent",
                extra={"scope": scope, "status": r.status_code, "decision_id": d.decision_id,
                       "incident_id": r.json().get("incident_id") if r.is_success else None},
            )
        except Exception as e:  # never let a trip failure take the guard down
            log.error("auto-trip failed", extra={"scope": scope, "error": str(e), "decision_id": d.decision_id})
            with self._lock:
                self._last.pop(scope, None)  # allow a retry on the next violation
        finally:
            otel_context.detach(token)
