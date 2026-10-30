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


def init_series(agents=("researcher", "executor", "demo-user", "probe")) -> None:
    """Create every series at 0. A series that first appears already at, say, 4 shows no
    increase() to Prometheus, so the first GuardBlockSpike after a start would be missed."""
    from guard.policy import CHECKS, MODES

    for agent in agents:
        for check in CHECKS:
            for mode in MODES - {"off"}:
                for verdict in ("allow", "flag", "block"):
                    DECISIONS.labels(agent, check, mode, verdict)
INJECTION_CHECKS = {
    "llm.user_prompt_injection",
    "llm.tool_message_injection",
    "mcp.tool_result_injection",
    "agent.user_prompt_injection",
}


@dataclass
class Subject:
    """What a check looks at. origin is "external" for untrusted content (what a person
    typed, MCP tool results) and "agent" for text the agent produced itself (D-034)."""

    check: str
    agent: str
    route: str
    content: str | None = None  # text for injection checks
    tool: str | None = None  # "server.tool" for tool checks
    arguments: dict = field(default_factory=dict)
    origin: str = "external"
    user: str | None = None  # the person the agent acts for, from its verified OBO token (D-072)
    groups: list[str] = field(default_factory=list)
    user_error: str | None = None  # why the OBO token was rejected, if it was


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
    user: str | None
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
        if s.check == "mcp.user_entitlement":
            return self._entitlement(policy, s)
        raise ValueError(f"no decider for {s.check}")

    def _injection(self, policy: Policy, s: Subject) -> Judgement:
        text = s.content or ""
        heur = heuristic(text)
        # The classifier is for untrusted content. It scores many short, system-sounding
        # agent messages ("Not done: ...", hand-off results) as injections, so agent-produced
        # text gets the heuristics only (D-034).
        model = self._score(text) if s.origin == "external" else 0.0
        threshold = policy.check(s.check).threshold or 0.9
        score, rule_id = (heur.score, heur.rule_id) if heur.score > model else (model, "classifier")
        signals = (f"classifier {model:.3f}" if s.origin == "external" else "classifier not applied (agent-produced text)")
        signals += f", heuristic {heur.rule_id} {heur.score:.2f}" if heur.rule_id else ", no heuristic matched"
        if score >= threshold:
            return Judgement(True, score, rule_id,
                             f"looks like a prompt injection: score {score:.3f} is at or above the {threshold} threshold ({signals})")
        return Judgement(False, score, rule_id if heur.rule_id else None,
                         f"no injection found: score {score:.3f} is below the {threshold} threshold ({signals})")

    def _allowlist(self, policy: Policy, s: Subject) -> Judgement:
        if s.tool in policy.deny_tools_all:
            return Judgement(True, 1.0, "deny_tools_all",
                             f"{s.tool} is denied to all agents (policy rule deny_tools_all)")
        if s.tool not in policy.agent(s.agent).tools:
            allowed = ", ".join(sorted(policy.agent(s.agent).tools)) or "none"
            return Judgement(True, 1.0, "agent_allowlist",
                             f"{s.tool} is not in the {s.agent}'s tool allowlist (allowed: {allowed})")
        return Judgement(False, 0.0, None, f"{s.tool} is in the {s.agent}'s tool allowlist")

    def _arguments(self, policy: Policy, s: Subject) -> Judgement:
        rules = policy.agent(s.agent).argument_rules.get(s.tool) or {}
        args = s.arguments or {}
        if domains := rules.get("to_domain_allow"):
            recipients = args.get("to", [])
            recipients = [recipients] if isinstance(recipients, str) else list(recipients)
            allowed_domains = {d.lower() for d in domains}
            for r in (x.strip() for raw in recipients for x in str(raw).split(",")):
                domain = r.rsplit("@", 1)[-1].lower() if "@" in r else ""
                # demo.local and its subdomains (e.g. blueridge.demo.local) are internal
                if not any(domain == d or domain.endswith("." + d) for d in allowed_domains):
                    return Judgement(True, 1.0, "to_domain_allow",
                                     f"recipient domain {domain or r!r} is outside the allowed domains "
                                     f"({', '.join(sorted(allowed_domains))}) for {s.tool}")
        if allowed := rules.get("fields_allow"):
            extra = sorted(set((args.get("fields") or {}).keys()) - set(allowed))
            if extra:
                return Judgement(True, 1.0, "fields_allow",
                                 f"fields {extra} may not be changed (allowed: {', '.join(allowed)})")
        return Judgement(False, 0.0, None, "arguments within the policy" if rules else "no argument rules for this tool")


    def _entitlement(self, policy: Policy, s: Subject) -> Judgement:
        if s.user_error:
            return Judgement(True, 1.0, "user_token", f"the {s.agent}'s token for the user was rejected: {s.user_error}")
        if not s.user:
            return Judgement(False, 0.0, None, "no user token: the caller isn't acting for a signed-in person")
        groups = ", ".join(s.groups) or "none"
        if s.tool not in policy.user_tools(s.groups):
            return Judgement(True, 1.0, "user_entitlement",
                             f"{s.user} may not {s.tool}: their groups ({groups}) don't allow it, "
                             f"so the {s.agent} may not do it for them")
        return Judgement(False, 0.0, None, f"{s.user} may {s.tool} (groups: {groups})")


class SeenTexts:
    """Hashes of tool results the MCP proxy inspected, and of the guard's own replacement
    text, so the LLM-stage check can tell external content from agent-produced text."""

    def __init__(self, ttl: float = 3600, limit: int = 20000):
        self.ttl, self.limit = ttl, limit
        self._seen: dict[str, float] = {}

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha256(text.strip().encode()).hexdigest()

    def add(self, *texts: str) -> None:
        now = time.monotonic()
        if len(self._seen) > self.limit:
            self._seen = {k: t for k, t in self._seen.items() if now - t < self.ttl}
        for text in texts:
            if text:
                self._seen[self.key(text)] = now

    def __contains__(self, text: str) -> bool:
        t = self._seen.get(self.key(text))
        return t is not None and time.monotonic() - t < self.ttl


class Engine:
    def __init__(self, store: PolicyStore, decider: Decider, on_critical=None):
        self.store = store
        self.decider = decider
        self.on_critical = on_critical  # callable(Decision), e.g. killswitch auto-trip
        self.external = SeenTexts()  # MCP results seen by the proxy
        self.own = SeenTexts()  # text the guard itself wrote (quarantine notices)

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
            user=s.user,
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
