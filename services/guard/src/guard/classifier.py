"""Prompt-injection classifier (GRD-01): DeBERTa-v3 ONNX model on CPU, no network at runtime.

Text is scored per segment: each paragraph and each HTML comment on its own, with
overlapping 512-token windows for long segments. The highest injection probability wins.
Scoring a whole document at once dilutes a hidden instruction: the poisoned supplier-07
scores 0.004 as one block but its comment alone scores ~0.99 (DECISIONS D-026).
"""

import json
import re
import threading
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

MAX_TOKENS = 512
STRIDE = 256
_COMMENT = re.compile(r"<!--(.*?)-->", re.S)
_PARAGRAPH = re.compile(r"\n\s*\n")


def segments(text: str) -> list[str]:
    """Paragraphs of the visible text plus the content of every HTML comment."""
    hidden = [m.strip() for m in _COMMENT.findall(text)]
    visible = _COMMENT.sub("\n\n", text)
    parts = [p.strip() for p in _PARAGRAPH.split(visible)] + hidden
    return [p for p in parts if p]


class Classifier:
    def __init__(self, model_dir: str | Path):
        model_dir = Path(model_dir)
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        self.session = ort.InferenceSession(str(model_dir / "model.onnx"), opts, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}
        labels = json.loads((model_dir / "config.json").read_text())["id2label"]
        self.injection_index = next(int(k) for k, v in labels.items() if v.upper() == "INJECTION")
        self.cls = self.tokenizer.token_to_id("[CLS]")
        self.sep = self.tokenizer.token_to_id("[SEP]")
        self._lock = threading.Lock()

    def _windows(self, ids: list[int]) -> list[list[int]]:
        body = MAX_TOKENS - 2
        if len(ids) <= body:
            return [ids]
        return [ids[i : i + body] for i in range(0, len(ids) - STRIDE, STRIDE)] or [ids[:body]]

    def score(self, text: str) -> float:
        """Probability (0..1) that text contains a prompt injection; max over segments."""
        return max((self._score_segment(seg) for seg in segments(text or "")), default=0.0)

    def _score_segment(self, text: str) -> float:
        ids = self.tokenizer.encode(text, add_special_tokens=False).ids
        best = 0.0
        for window in self._windows(ids):
            seq = [self.cls, *window, self.sep]
            feed = {
                "input_ids": np.array([seq], dtype=np.int64),
                "attention_mask": np.ones((1, len(seq)), dtype=np.int64),
            }
            if "token_type_ids" in self.inputs:
                feed["token_type_ids"] = np.zeros((1, len(seq)), dtype=np.int64)
            with self._lock:
                logits = self.session.run(None, {k: v for k, v in feed.items() if k in self.inputs})[0][0]
            p = np.exp(logits - logits.max())
            best = max(best, float(p[self.injection_index] / p.sum()))
        return best
