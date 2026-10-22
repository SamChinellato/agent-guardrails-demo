"""Benchmark candidate live-LLM servers (Q-16) with the requests the demo actually sends.

Each target is an OpenAI-compatible base URL (port-forwarded). Three measurements:
  cold    streamed request on a ~2,600-token prompt with a fresh nonce (no prompt-cache hit):
          time to first token, prompt tokens/s, generation tokens/s
  agent   the researcher agent's loop (its system prompt, docs__ tool names, temperature 0,
          seed 42, non-streamed as the agents call it), tools answered locally from the
          fixtures; per-step wall time against the gateway's 120 s timeout
  repeat  the agent run again: is the transcript identical?

An `ollama-native:` target uses Ollama's /api/chat instead, so num_ctx and num_thread can
be set per request (its OpenAI endpoint takes neither).

Usage: bench_llm.py NAME=URL=MODEL [...]   e.g. llamacpp=http://127.0.0.1:18000=granite-4.0-micro
       a NAME starting with ollama-native uses /api/chat with --ollama-options (JSON)
"""

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "scenarios" / "fixtures" / "documents"
sys.path.insert(0, str(ROOT / "services" / "agents" / "src"))
from agents.app import HANDOFF_TOOL, RESEARCHER_PROMPT  # noqa: E402

GATEWAY_TIMEOUT = 120
TASK = "Which suppliers are below their on-time delivery target, and by how much?"
TOOLS = [
    {"type": "function", "function": {
        "name": "docs__list_documents",
        "description": "List the supplier review documents, each with its id, supplier name and a one-line performance summary.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "docs__read_document",
        "description": "Read one supplier review document by its id, e.g. supplier-01.",
        "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}},
    HANDOFF_TOOL,
]


def summary(text: str) -> str:
    """As mcp-docs' _summary: category line and performance bullets only."""
    out, perf = [], False
    for line in text.splitlines():
        if line.startswith("**Category:**"):
            out.append(line.replace("**", "").strip())
        elif line.startswith("## "):
            perf = line.startswith("## Performance")
        elif perf and line.startswith("- "):
            out.append(line[2:].strip())
    return "; ".join(out)


def run_tool(name: str, args: dict) -> str:
    docs = {p.stem: p for p in sorted(DOCS.glob("*.md"))}
    if name == "docs__list_documents":
        return json.dumps([{"id": k, "supplier": p.read_text().splitlines()[0].lstrip("# ")
                            .replace("Supplier review: ", ""), "summary": summary(p.read_text())}
                           for k, p in docs.items()])
    if name == "docs__read_document":
        p = docs.get(args.get("id", ""))
        return p.read_text() if p else f"Error: no document with id {args.get('id')!r}"
    if name == "handoff_to_executor":
        return "Handed off to the executor."
    return f"Error: unknown tool {name}"


class Target:
    def __init__(self, spec: str, ollama_options: dict):
        self.name, self.url, self.model = spec.split("=", 2)
        self.native = self.name.startswith("ollama-native")
        self.options = ollama_options
        # A fresh connection per request: port-forward drops pooled idle connections
        self.http = httpx.Client(base_url=self.url.rstrip("/"), timeout=600,
                                 limits=httpx.Limits(max_keepalive_connections=0))

    def chat(self, messages: list[dict], tools: list | None, max_tokens: int | None = None) -> dict:
        """Non-streamed chat; returns {message, prompt_tokens, completion_tokens, seconds}."""
        t0 = time.monotonic()
        if self.native:
            body = {"model": self.model, "messages": [self._native_msg(m) for m in messages], "stream": False,
                    "options": {**self.options, "temperature": 0, "seed": 42,
                                **({"num_predict": max_tokens} if max_tokens else {})}}
            if tools:
                body["tools"] = tools
            r = self.http.post("/api/chat", json=body).raise_for_status().json()
            msg = r["message"]
            calls = [{"id": f"call_{i}", "type": "function",
                      "function": {"name": c["function"]["name"], "arguments": json.dumps(c["function"]["arguments"])}}
                     for i, c in enumerate(msg.get("tool_calls") or [])]
            return {"message": {"role": "assistant", "content": msg.get("content") or "",
                                **({"tool_calls": calls} if calls else {})},
                    "prompt_tokens": r.get("prompt_eval_count", 0), "completion_tokens": r.get("eval_count", 0),
                    "seconds": time.monotonic() - t0}
        body = {"model": self.model, "messages": messages, "temperature": 0, "seed": 42, "stream": False}
        if tools:
            body["tools"] = tools
        if max_tokens:
            body["max_tokens"] = max_tokens
        r = self.http.post("/v1/chat/completions", json=body).raise_for_status().json()
        msg = r["choices"][0]["message"]
        return {"message": {k: v for k, v in msg.items() if k in ("role", "content", "tool_calls") and v is not None},
                "prompt_tokens": r["usage"]["prompt_tokens"], "completion_tokens": r["usage"]["completion_tokens"],
                "seconds": time.monotonic() - t0}

    @staticmethod
    def _native_msg(m: dict) -> dict:
        if m.get("tool_calls"):
            return {"role": "assistant", "content": m.get("content") or "", "tool_calls": [
                {"function": {"name": c["function"]["name"], "arguments": json.loads(c["function"]["arguments"])}}
                for c in m["tool_calls"]]}
        return {"role": m["role"], "content": m.get("content") or ""}

    def cold(self) -> dict:
        """Streamed, cache-busting long prompt: TTFT, prompt and generation rates."""
        docs = "\n\n".join(p.read_text() for p in sorted(DOCS.glob("*.md")))
        messages = [{"role": "system", "content": f"Session {uuid.uuid4()}. {RESEARCHER_PROMPT}"},
                    {"role": "user", "content": ("Here are this quarter's reviews and last quarter's archived copies.\n\n"
                                                 + "\n\n".join([docs] * 3) + "\n\nSummarise the overall supplier "
                                                 "picture in about 150 words.")}]
        t0 = time.monotonic()
        first = None
        n_prompt = n_gen = 0
        if self.native:
            body = {"model": self.model, "messages": messages, "stream": True,
                    "options": {**self.options, "temperature": 0, "seed": 42, "num_predict": 256}}
            with self.http.stream("POST", "/api/chat", json=body) as r:
                for line in r.raise_for_status().iter_lines():
                    if not line:
                        continue
                    ev = json.loads(line)
                    if first is None and ev.get("message", {}).get("content"):
                        first = time.monotonic()
                    if ev.get("done"):
                        n_prompt, n_gen = ev.get("prompt_eval_count", 0), ev.get("eval_count", 0)
        else:
            body = {"model": self.model, "messages": messages, "temperature": 0, "seed": 42, "max_tokens": 256,
                    "stream": True, "stream_options": {"include_usage": True}}
            with self.http.stream("POST", "/v1/chat/completions", json=body) as r:
                for line in r.raise_for_status().iter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    ev = json.loads(line[6:])
                    if first is None and any((c.get("delta") or {}).get("content") for c in ev.get("choices", [])):
                        first = time.monotonic()
                    if ev.get("usage"):
                        n_prompt, n_gen = ev["usage"]["prompt_tokens"], ev["usage"]["completion_tokens"]
        end = time.monotonic()
        ttft = (first or end) - t0
        return {"prompt_tokens": n_prompt, "ttft_s": round(ttft, 1),
                "prompt_tok_s": round(n_prompt / ttft, 1) if ttft else None,
                "gen_tokens": n_gen, "gen_tok_s": round((n_gen - 1) / (end - first), 1) if first and end > first else None}

    def agent(self, max_steps: int = 6) -> dict:
        convo = [{"role": "system", "content": RESEARCHER_PROMPT}, {"role": "user", "content": TASK}]
        steps = []
        for _ in range(max_steps):
            r = self.chat(convo, TOOLS)
            msg = r["message"]
            convo.append(msg)
            calls = msg.get("tool_calls") or []
            steps.append({"seconds": round(r["seconds"], 1), "prompt_tokens": r["prompt_tokens"],
                          "completion_tokens": r["completion_tokens"],
                          "calls": [f'{c["function"]["name"]}({c["function"]["arguments"]})' for c in calls]})
            if not calls:
                return {"status": "completed", "steps": steps, "reply": msg.get("content", ""), "transcript": convo}
            for c in calls:
                try:
                    args = json.loads(c["function"]["arguments"] or "{}")
                except ValueError:
                    args = {}
                convo.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": run_tool(c["function"]["name"], args)})
        return {"status": "max_steps", "steps": steps, "reply": "", "transcript": convo}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="+")
    ap.add_argument("--ollama-options", default='{"num_ctx": 16384, "num_thread": 8}')
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    results = {}
    for spec in a.targets:
        t = Target(spec, json.loads(a.ollama_options))
        print(f"== {t.name}: warm-up", flush=True)
        t.chat([{"role": "user", "content": "Say OK."}], None, max_tokens=4)
        cold = [t.cold() for _ in range(2)]
        print(f"   cold: {cold}", flush=True)
        runs = [t.agent() for _ in range(2)]
        for i, run in enumerate(runs):
            total = sum(s["seconds"] for s in run["steps"])
            worst = max(s["seconds"] for s in run["steps"])
            print(f"   agent run {i + 1}: {run['status']}, {len(run['steps'])} steps, {total:.1f} s total, "
                  f"slowest step {worst:.1f} s{' OVER GATEWAY TIMEOUT' if worst > GATEWAY_TIMEOUT else ''}", flush=True)
            for s in run["steps"]:
                print(f"     {s}", flush=True)
            print(f"     reply: {run['reply']!r}", flush=True)
        # Servers mint random tool-call ids; compare what the model said and called
        same = [(s["calls"], r["reply"]) for r in runs[:1] for s in r["steps"]] == \
               [(s["calls"], r["reply"]) for r in runs[1:] for s in r["steps"]]
        print(f"   repeat identical: {same}", flush=True)
        results[t.name] = {"cold": cold, "agent": [{k: v for k, v in r.items() if k != "transcript"} for r in runs],
                           "repeat_identical": same}
    if a.out:
        a.out.write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
