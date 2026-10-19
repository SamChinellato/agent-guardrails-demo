"""Decision engine: deciders judge, the engine applies policy modes and records.

    Decider   -> "is this a violation, how sure, which rule, why"   (pluggable)
    Engine    -> mode (enforce/monitor/off) -> verdict (allow/flag/block),
                 decision record (GRD-05), auto-trip on critical (GRD-06)

LocalDecider uses the classifier, the regex heuristics and the policy's tool rules.
An external decisioning model can implement the same Decider protocol and be chained
in front of, or instead of, the local one (DECISIONS D-027) without touching records,
modes or enforcement.
"""

import hashlib
import time
import uuid
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Protocol

from opentelemetry import trace
from prometheus_client import Counter

from demo_common import get_logger
from guard.classifier import Classifier
from guard.policy import Policy, PolicyStore
from guard.signals import heuristic, redact

log = get_logger("guard.decision")

DECISIONS = Counter("guard_decisions_total", "Guard decisions", ["agent", "check", "mode", "verdict"])
INJECTION_CHECKS = {
    "llm.user_prompt_injection",
    "llm.tool_message_injection",
    "mcp.tool_result_injection",
    "agent.user_prompt_injection",
}


@dataclass
class Subject:
    """What a check looks at."""

    check: str
    agent: str
    route: str
    content: str | None = None  # text for injection checks
    tool: str | None = None  # "server.tool" for tool checks
    arguments: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Judgement:
    violation: bool
    score: float
    rule_id: str | None
    reason: str


class Decider(Protocol):
    def judge(self, policy: Policy, subject: Subject) -> Judgement: ...


@dataclass
class Decision:
    decision_id: str
    ts: float
    trace_id: str | None
    span_id: str | None
    agent: str
    check: str
    mode: str
    severity: str
    verdict: str  # allow | flag | block
    score: float
    rule_id: str | None
    reason: str
    route: str
    tool: str | None
    policy_version: str
    content_sha256: str | None
    excerpt: str | None


class LocalDecider:
    def __init__(self, classifier: Classifier):
        self.classifier = classifier
        self._score = lru_cache(maxsize=4096)(classifier.score)

    def judge(self, policy: Policy, s: Subject) -> Judgement:
        if s.check in INJECTION_CHECKS:
            return self._injection(policy, s)
        if s.check == "mcp.tool_allowlist":
            return self._allowlist(policy, s)
        if s.check == "mcp.argument_rules":
            return self._arguments(policy, s)
        raise ValueError(f"no decider for {s.check}")

    def _injection(self, policy: Policy, s: Subject) -> Judgement:
        text = s.content or ""
        model = self._score(text)
        heur = heuristic(text)
        threshold = policy.check(s.check).threshold or 0.9
        score, rule_id = (heur.score, heur.rule_id) if heur.score > model else (model, "classifier")
        if score >= threshold:
            return Judgement(True, score, rule_id, f"prompt injection suspected (score {score:.2f} >= {threshold})")
        return Judgement(False, score, rule_id if heur.rule_id else None, "no injection detected")

    def _allowlist(self, policy: Policy, s: Subject) -> Judgement:
        if s.tool in policy.deny_tools_all:
            return Judgement(True, 1.0, "deny_tools_all", f"{s.tool} is denied to all agents")
        if s.tool not in policy.agent(s.agent).tools:
            return Judgement(True, 1.0, "agent_allowlist", f"{s.tool} is not in {s.agent}'s tool allowlist")
        return Judgement(False, 0.0, None, "tool allowed")

    def _arguments(self, policy: Policy, s: Subject) -> Judgement:
        rules = policy.agent(s.agent).argument_rules.get(s.tool) or {}
        args = s.arguments or {}
        if domains := rules.get("to_domain_allow"):
            recipients = args.get("to", [])
            recipients = [recipients] if isinstance(recipients, str) else list(recipients)
            for r in (x.strip() for raw in recipients for x in str(raw).split(",")):
                domain = r.rsplit("@", 1)[-1].lower() if "@" in r else ""
                if domain not in {d.lower() for d in domains}:
                    return Judgement(True, 1.0, "to_domain_allow", f"recipient domain {domain or r!r} is not allowed")
        if allowed := rules.get("fields_allow"):
            extra = sorted(set((args.get("fields") or {}).keys()) - set(allowed))
            if extra:
                return Judgement(True, 1.0, "fields_allow", f"fields {extra} may not be changed")
        return Judgement(False, 0.0, None, "arguments allowed")


class Engine:
    def __init__(self, store: PolicyStore, decider: Decider, on_critical=None):
        self.store = store
        self.decider = decider
        self.on_critical = on_critical  # callable(Decision), e.g. killswitch auto-trip

    def evaluate(self, s: Subject) -> Decision | None:
        """Judge one subject under the current policy. None when the check is off."""
        policy = self.store.policy
        check = policy.check(s.check)
        if check.mode == "off":
            return None
        j = self.decider.judge(policy, s)
        verdict = "allow"
        if j.violation:
            verdict = "block" if check.mode == "enforce" else "flag"
        ctx = trace.get_current_span().get_span_context()
        d = Decision(
            decision_id="dec-" + uuid.uuid4().hex[:16],
            ts=time.time(),
            trace_id=format(ctx.trace_id, "032x") if ctx.is_valid else None,
            span_id=format(ctx.span_id, "016x") if ctx.is_valid else None,
            agent=s.agent,
            check=s.check,
            mode=check.mode,
            severity=check.severity,
            verdict=verdict,
            score=round(j.score, 4),
            rule_id=j.rule_id,
            reason=j.reason,
            route=s.route,
            tool=s.tool,
            policy_version=policy.version,
            content_sha256=hashlib.sha256(s.content.encode()).hexdigest() if s.content else None,
            excerpt=redact(s.content) if s.content and verdict != "allow" else None,
        )
        self._record(d)
        if verdict == "block" and check.severity == "critical" and self.on_critical:
            self.on_critical(d)
        return d

    def _record(self, d: Decision) -> None:
        DECISIONS.labels(d.agent or "unknown", d.check, d.mode, d.verdict).inc()
        attrs = {k: v for k, v in asdict(d).items() if v is not None and k not in ("excerpt", "ts")}
        trace.get_current_span().add_event("guard.decision", attributes={f"guard.{k}": v for k, v in attrs.items()})
        level = log.warning if d.verdict != "allow" else log.info
        level("guard decision", extra={**asdict(d), "event": "guard_decision"})
