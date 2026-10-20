"""Kill-switch state lives in APISIX, the source of truth (7.4). Nothing is cached.

  global        fault-injection enabled in pc-llm, pc-mcp and pc-agents     (KS-01)
  tools         fault-injection enabled in pc-mcp only
  agent:<name>  agent-kill-switch plugin on that consumer (D-013)

Two scopes can both need pc-mcp (global and tools), so each plugin config records which
scopes hold it with labels killswitch.global / killswitch.tools; fault-injection stays on
while any label remains. Plugin configs are PATCHed; consumers can't be (D-014), so they
are read, changed and PUT back. Every change is read back before it counts (KS-04).
"""

import os

import httpx

ADMIN = os.environ.get("APISIX_ADMIN_URL", "http://apisix-admin.gateway.svc.cluster.local:9180").rstrip("/")
ADMIN_KEY = os.environ.get("APISIX_ADMIN_KEY", "")
PLUGIN_CONFIGS = {"global": ["pc-llm", "pc-mcp", "pc-agents"], "tools": ["pc-mcp"]}
AGENTS = ("researcher", "executor")


class AdminError(Exception):
    pass


class Apisix:
    def __init__(self):
        self.http = httpx.Client(base_url=f"{ADMIN}/apisix/admin", headers={"X-API-KEY": ADMIN_KEY}, timeout=5)

    def _get(self, path: str) -> dict:
        r = self.http.get(path)
        if r.status_code != 200:
            raise AdminError(f"GET {path}: {r.status_code}")
        return r.json()["value"]

    def _send(self, method: str, path: str, body: dict) -> None:
        r = self.http.request(method, path, json=body)
        if r.status_code not in (200, 201):
            raise AdminError(f"{method} {path}: {r.status_code} {r.text[:200]}")

    # ------------------------------------------------------------------ state
    def plugin_config(self, pc: str) -> dict:
        return self._get(f"/plugin_configs/{pc}")

    @staticmethod
    def fi_enabled(pc: dict) -> bool:
        return pc["plugins"]["fault-injection"].get("_meta", {}).get("disable") is False

    def consumer(self, name: str) -> dict:
        return self._get(f"/consumers/{name}")

    def status(self) -> dict:
        """Live state of every scope plus the raw switches it is derived from."""
        pcs = {pc: self.plugin_config(pc) for pc in PLUGIN_CONFIGS["global"]}
        labels = {pc: (v.get("labels") or {}) for pc, v in pcs.items()}
        scopes = {
            "global": all(labels[pc].get("killswitch.global") == "on" and self.fi_enabled(pcs[pc])
                          for pc in PLUGIN_CONFIGS["global"]),
            "tools": labels["pc-mcp"].get("killswitch.tools") == "on" and self.fi_enabled(pcs["pc-mcp"]),
        }
        for agent in AGENTS:
            c = self.consumer(agent)
            ks = (c.get("plugins") or {}).get("agent-kill-switch")
            scopes[f"agent:{agent}"] = ks is not None
        return {
            "scopes": scopes,
            "plugin_configs": {pc: {"fault_injection": self.fi_enabled(v), "labels": labels[pc]} for pc, v in pcs.items()},
        }

    def is_tripped(self, scope: str) -> bool:
        return self.status()["scopes"].get(scope, False)

    # ------------------------------------------------------------------ changes
    def set_scope(self, scope: str, on: bool, incident_id: str | None = None) -> list[dict]:
        """Engage or release one scope. Returns the APISIX changes made (for the audit log)."""
        if scope in PLUGIN_CONFIGS:
            return self._set_plugin_configs(scope, on)
        if scope.startswith("agent:") and scope[6:] in AGENTS:
            return self._set_consumer(scope[6:], on, incident_id)
        raise ValueError(f"unknown scope {scope!r}")

    def _set_plugin_configs(self, scope: str, on: bool) -> list[dict]:
        changes = []
        label = f"killswitch.{scope}"
        for pc in PLUGIN_CONFIGS[scope]:
            current = self.plugin_config(pc)
            labels = dict(current.get("labels") or {})
            if on:
                labels[label] = "on"
            else:
                labels.pop(label, None)
            engaged = any(v == "on" for k, v in labels.items() if k.startswith("killswitch."))
            # PATCH merges maps, so a removed label must be sent as null to drop it
            patch_labels = {k: v for k, v in labels.items()}
            if not on:
                patch_labels[label] = None
            self._send("PATCH", f"/plugin_configs/{pc}", {
                "labels": patch_labels or None,
                "plugins": {"fault-injection": {"_meta": {"disable": not engaged}}},
            })
            changes.append({"object": f"plugin_configs/{pc}", "fault_injection": engaged, "labels": labels})
        return changes

    def _set_consumer(self, agent: str, on: bool, incident_id: str | None) -> list[dict]:
        c = self.consumer(agent)
        body = {k: c[k] for k in ("username", "desc", "plugins", "labels", "group_id") if k in c}
        plugins = dict(body.get("plugins") or {})
        if on:
            plugins["agent-kill-switch"] = {"incident_id": incident_id or "", "message": "Agent suspended by incident response"}
        else:
            plugins.pop("agent-kill-switch", None)
        body["plugins"] = plugins
        self._send("PUT", "/consumers", body)
        return [{"object": f"consumers/{agent}", "agent_kill_switch": on}]
