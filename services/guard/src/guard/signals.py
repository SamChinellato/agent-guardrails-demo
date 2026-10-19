"""Second injection signal (GRD-01): a small regex heuristic list, plus redaction of
content excerpts for decision records (EVD-04)."""

import re
from dataclasses import dataclass

# (rule_id, pattern, score). Kept short and explainable; the classifier does the heavy lifting.
HEURISTICS = [
    ("heur.ignore_previous", r"\b(ignore|disregard|forget)\b.{0,40}\b(previous|prior|earlier|above)\b.{0,20}\b(instructions?|guidance|rules|messages?)\b", 0.95),
    ("heur.addressed_to_ai", r"\b(note|notice|message|instructions?)\s+(to|for)\s+(ai|llm)\s+(assistants?|systems?|models?|agents?)\b", 0.95),
    ("heur.if_you_are_ai", r"\bif you are an? (ai|llm|language model|assistant)\b", 0.9),
    ("heur.override", r"\b(overrides?|supersedes?)\s+(any|all)?\s*(earlier|previous|prior|other)\s+(guidance|instructions?)\b", 0.9),
    ("heur.conceal", r"\bdo not (mention|tell|reveal|disclose)\b.{0,30}\b(this|the user|anyone)\b", 0.85),
]
_COMPILED = [(rid, re.compile(p, re.I | re.S), s) for rid, p, s in HEURISTICS]


@dataclass(frozen=True)
class Signal:
    score: float
    rule_id: str | None


def heuristic(text: str) -> Signal:
    best = Signal(0.0, None)
    for rule_id, pattern, score in _COMPILED:
        if score > best.score and pattern.search(text):
            best = Signal(score, rule_id)
    return best


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
# Two or three capitalised words: catches personal names; over-redacts some company
# names, which is the safe direction for evidence.
_NAME = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}\b")


def redact(text: str, limit: int = 500) -> str:
    """Excerpt for decision records: emails, phone numbers and names removed, max 500 chars."""
    text = _EMAIL.sub("[email]", text)
    text = _PHONE.sub("[phone]", text)
    text = _NAME.sub("[name]", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
