"""Verify (or wait for) the pinned Ollama model in OLLAMA_MODELS (OS-03): the manifest's
SHA-256 must equal OLLAMA_MODEL_DIGEST (what `ollama list` and /api/tags report), and every
blob it lists must hash to its own name. No network. With --wait, re-check every 15 s."""

import hashlib
import json
import os
import sys
import time

models, name, want = os.environ["OLLAMA_MODELS"], os.environ["OLLAMA_MODEL"], os.environ["OLLAMA_MODEL_DIGEST"]
repo, tag = name.split(":")
manifest = os.path.join(models, "manifests", "registry.ollama.ai", "library", repo, tag)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def problem() -> str | None:
    if not os.path.exists(manifest):
        return "manifest missing"
    if (got := sha256(manifest)) != want:
        return f"manifest digest {got} != pinned {want}"
    with open(manifest) as f:
        m = json.load(f)
    for layer in [m["config"], *m["layers"]]:
        blob = os.path.join(models, "blobs", layer["digest"].replace(":", "-"))
        if not os.path.exists(blob) or sha256(blob) != layer["digest"]:
            return f"blob {layer['digest']} missing or corrupt"
    return None


while (p := problem()) is not None:
    if "--wait" not in sys.argv:
        sys.exit(f"{name}: {p}")
    print(f"waiting for {name}: {p}", flush=True)
    time.sleep(15)
print(f"{name} verified ({want})", flush=True)
