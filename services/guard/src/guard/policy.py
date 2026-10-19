"""Guard policy (GRD-02): parsed from policy.yaml, hot-reloaded when the file changes.

The file is a ConfigMap mounted as a directory (not subPath), so kubelet swaps it in place;
a background thread compares content hashes every second. A file that fails to parse is
rejected and the previous policy stays active.
"""

import hashlib
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from demo_common import get_logger

log = get_logger("guard.policy")

MODES = {"enforce", "monitor", "off"}
SEVERITIES = {"low", "medium", "high", "critical"}
CHECKS = {
    "llm.user_prompt_injection",
    "llm.tool_message_injection",
    "mcp.tool_result_injection",
    "mcp.tool_allowlist",
    "mcp.argument_rules",
    "agent.user_prompt_injection",  # DEMO-02: inbound filter on the agent route
}


@dataclass(frozen=True)
class Check:
    name: str
    mode: str
    severity: str
    threshold: float | None = None


@dataclass(frozen=True)
class AgentPolicy:
    tools: frozenset[str] = frozenset()
    argument_rules: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Policy:
    version: str  # content hash, recorded on every decision
    raw: dict
    checks: dict[str, Check]
    agents: dict[str, AgentPolicy]
    deny_tools_all: frozenset[str]
    auto_trip: dict

    def check(self, name: str) -> Check:
        return self.checks.get(name) or Check(name, "off", "low")

    def agent(self, agent_id: str) -> AgentPolicy:
        return self.agents.get(agent_id, AgentPolicy())


def parse(text: str) -> Policy:
    raw = yaml.safe_load(text) or {}
    checks = {}
    for name, c in (raw.get("checks") or {}).items():
        if name not in CHECKS:
            raise ValueError(f"unknown check {name!r}")
        mode, severity = c.get("mode", "enforce"), c.get("severity", "high")
        if mode not in MODES:
            raise ValueError(f"{name}: mode must be one of {sorted(MODES)}")
        if severity not in SEVERITIES:
            raise ValueError(f"{name}: severity must be one of {sorted(SEVERITIES)}")
        threshold = c.get("threshold")
        if threshold is not None and not 0 <= float(threshold) <= 1:
            raise ValueError(f"{name}: threshold must be within 0..1")
        checks[name] = Check(name, mode, severity, None if threshold is None else float(threshold))
    agents = {
        name: AgentPolicy(frozenset(a.get("tools") or []), dict(a.get("argument_rules") or {}))
        for name, a in (raw.get("agents") or {}).items()
    }
    return Policy(
        version=hashlib.sha256(text.encode()).hexdigest()[:12],
        raw=raw,
        checks=checks,
        agents=agents,
        deny_tools_all=frozenset(raw.get("deny_tools_all") or []),
        auto_trip=dict(raw.get("auto_trip") or {}),
    )


class PolicyStore:
    def __init__(self, path: str | Path, interval: float = 1.0):
        self.path = Path(path)
        self.interval = interval
        self._policy: Policy | None = None
        self._digest = ""
        self._stop = threading.Event()
        self.reload()
        threading.Thread(target=self._watch, name="policy-watch", daemon=True).start()

    @property
    def policy(self) -> Policy:
        assert self._policy is not None, "policy not loaded"
        return self._policy

    @property
    def ready(self) -> bool:
        return self._policy is not None

    def reload(self) -> bool:
        try:
            text = self.path.read_text()
        except OSError as e:
            log.error("cannot read policy", extra={"path": str(self.path), "error": str(e)})
            return False
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest == self._digest:
            return False
        try:
            policy = parse(text)
        except (ValueError, yaml.YAMLError, AttributeError, TypeError) as e:
            log.error("policy rejected; keeping previous", extra={"error": str(e), "policy_version": digest[:12]})
            self._digest = digest  # don't retry the same broken content every second
            return False
        previous = self._policy.version if self._policy else None
        self._policy, self._digest = policy, digest
        log.info(
            "policy loaded",
            extra={
                "policy_version": policy.version,
                "previous_version": previous,
                "modes": {n: c.mode for n, c in sorted(policy.checks.items())},
            },
        )
        return True

    def _watch(self) -> None:
        while not self._stop.wait(self.interval):
            self.reload()
