"""Append-only audit log (KS-07): one JSON event per line on a PVC, hash-chained.

  hash = sha256(canonical JSON of the event without "hash"), and each event carries the
  previous event's hash in prev_hash (the first event: 64 zeros). Changing, removing or
  reordering any line breaks the chain, which `make evidence-verify` checks (EVD-06).

Also holds the incident index (KS-12): a trip for the same target within 10 minutes of
that incident's last trip joins it. The index is rebuilt from the log on start.
"""

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

GENESIS = "0" * 64
INCIDENT_JOIN_SECONDS = 600


def canonical(event: dict) -> bytes:
    return json.dumps({k: v for k, v in event.items() if k != "hash"}, sort_keys=True, separators=(",", ":")).encode()


def event_hash(event: dict) -> str:
    return hashlib.sha256(canonical(event)).hexdigest()


def verify_chain(lines: list[str]) -> tuple[bool, str]:
    prev = GENESIS
    for i, line in enumerate(lines, 1):
        e = json.loads(line)
        if e.get("prev_hash") != prev:
            return False, f"line {i}: prev_hash does not match line {i - 1}"
        if event_hash(e) != e.get("hash"):
            return False, f"line {i}: hash does not match its content"
        prev = e["hash"]
    return True, f"{len(lines)} events, chain intact"


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_hash = GENESIS
        self._incidents: dict[str, tuple[str, float]] = {}  # target -> (incident_id, last trip ts)
        for e in self.read():
            self._last_hash = e["hash"]
            if e["type"] == "trip" and e.get("incident_id"):
                self._incidents[e["target"]] = (e["incident_id"], e["ts"])

    def read(self, limit: int | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text().splitlines()
        if limit:
            lines = lines[-limit:]
        return [json.loads(line) for line in lines if line.strip()]

    def incident_for(self, target: str, now: float) -> tuple[str, bool]:
        """(incident_id, is_new) for a trip of target at time now."""
        current = self._incidents.get(target)
        if current and now - current[1] < INCIDENT_JOIN_SECONDS:
            return current[0], False
        return "INC-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(now)) + "-" + uuid.uuid4().hex[:4], True

    def append(self, event: dict) -> dict:
        with self._lock:
            event = {**event, "event_id": "evt-" + uuid.uuid4().hex[:16], "prev_hash": self._last_hash}
            event["hash"] = event_hash(event)
            with self.path.open("a") as f:
                f.write(json.dumps(event, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._last_hash = event["hash"]
            if event["type"] == "trip" and event.get("incident_id"):
                self._incidents[event["target"]] = (event["incident_id"], event["ts"])
            return event

    def rotate(self) -> str | None:
        """Archive the current log (make reset, REP-07); a new chain starts from genesis."""
        with self._lock:
            if not self.path.exists() or not self.path.stat().st_size:
                return None
            archived = self.path.with_name(f"{self.path.stem}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.jsonl")
            self.path.rename(archived)
            self._last_hash = GENESIS
            self._incidents.clear()
            return archived.name

    def verify(self) -> tuple[bool, str]:
        lines = self.path.read_text().splitlines() if self.path.exists() else []
        return verify_chain([line for line in lines if line.strip()])
