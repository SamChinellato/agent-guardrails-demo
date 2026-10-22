"""Stage the live-mode model into the models volume (OS-03), verifying every file's SHA-256
against the pinned manifest. Re-runs keep files that already verify, so a restart resumes.

--verify-only (vLLM's init container): no network; wait until every file is present and
verifies, re-checking every 15 s, so vLLM starts only on a complete, untampered model."""

import hashlib
import os
import sys
import time
import urllib.request

dest = os.environ["MODEL_DIR"]
verify_only = "--verify-only" in sys.argv


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


with open(os.environ["MANIFEST"]) as f:
    wanted = [line.split() for line in f if line.strip() and not line.startswith("#")]

if verify_only:
    verified: set[str] = set()   # files don't change once staged; check each one once
    while True:
        for want, name in wanted:
            path = os.path.join(dest, name)
            if name not in verified and os.path.exists(path) and sha256(path) == want:
                verified.add(name)
        missing = [name for _, name in wanted if name not in verified]
        if not missing:
            print("model verified", flush=True)
            sys.exit(0)
        print(f"waiting for model-fetch: {len(missing)} file(s) missing or unverified", flush=True)
        time.sleep(15)

repo, rev = os.environ["MODEL_REPO"], os.environ["MODEL_REVISION"]
os.makedirs(dest, exist_ok=True)
for want, name in wanted:
    path = os.path.join(dest, name)
    if os.path.exists(path) and sha256(path) == want:
        print(f"{name}: present, sha256 ok", flush=True)
        continue
    print(f"{name}: downloading", flush=True)
    urllib.request.urlretrieve(f"https://huggingface.co/{repo}/resolve/{rev}/{name}", path + ".part")
    got = sha256(path + ".part")
    if got != want:
        os.remove(path + ".part")
        sys.exit(f"checksum mismatch for {name}: {got} != {want}")
    os.replace(path + ".part", path)
    print(f"{name}: sha256 ok", flush=True)
print("model staged", flush=True)
