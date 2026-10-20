"""make evidence / evidence-verify (EVD-01, EVD-06): collect, build the report, hash everything."""

import hashlib
import json
import time
from pathlib import Path

from demo_cli import collect, ops, report

EVIDENCE = ops.ROOT / "evidence"


def files(bundle: Path) -> list[Path]:
    return sorted(p for p in bundle.rglob("*") if p.is_file() and p.name != "manifest.json")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bundle_hash(entries: dict[str, str]) -> str:
    return hashlib.sha256("".join(f"{h}  {p}\n" for p, h in sorted(entries.items())).encode()).hexdigest()


def write_manifest(bundle: Path) -> dict:
    entries = {str(p.relative_to(bundle)): sha256(p) for p in files(bundle)}
    manifest = {"files": entries, "bundle_sha256": bundle_hash(entries),
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "generator": "demo-cli evidence 1"}
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def scan_secrets(bundle: Path) -> list[str]:
    """No generated secret may appear anywhere in the bundle (EVD-04)."""
    secrets = [p.read_text().strip() for p in (ops.ROOT / ".secrets").iterdir() if p.is_file()]
    hits = []
    for f in files(bundle):
        text = f.read_text(errors="replace")
        hits += [f"{f.relative_to(bundle)}" for s in secrets if s and len(s) >= 12 and s in text]
    return sorted(set(hits))


def make(incident: str | None, trace: str | None, say) -> Path:
    target, kind = (incident, "incident") if incident else (trace, "trace")
    bundle = EVIDENCE / target
    if bundle.exists():
        import shutil

        shutil.rmtree(bundle)
    say(f"Collecting evidence for {kind} {target}")
    collect.collect(target, kind, bundle, say)
    r = report.build(bundle)
    if hits := scan_secrets(bundle):
        raise SystemExit(f"refusing to finish: secrets found in {hits}")
    m = write_manifest(bundle)
    c = r["summary"]["containment"]
    say(f"{len(m['files'])} files, bundle sha256 {m['bundle_sha256'][:16]}…")
    if c:
        say(f"Containment: {c['scope']} tripped by {c['tripped_by']}, verified in {c['trip_to_verified_ms']:.0f} ms")
    say(f"Report: {bundle / 'report.html'}")
    return bundle


def rebuild(bundle: Path) -> None:
    """Regenerate report.json/html from raw/ (used to show EVD-05 determinism)."""
    report.build(bundle)


def verify(bundle: Path, say) -> bool:
    """EVD-06: every file matches the manifest, nothing extra or missing, the bundle hash
    matches, and the killswitch audit chain is intact."""
    try:
        m = json.loads((bundle / "manifest.json").read_text())
    except (OSError, ValueError) as e:
        say(f"manifest.json unreadable: {e}")
        say("Evidence bundle FAILED verification")
        return False
    ok = True
    actual = {str(p.relative_to(bundle)): sha256(p) for p in files(bundle)}
    for path in sorted(set(m["files"]) | set(actual)):
        if path not in actual:
            say(f"missing: {path}"); ok = False
        elif path not in m["files"]:
            say(f"not in manifest: {path}"); ok = False
        elif actual[path] != m["files"][path]:
            say(f"changed: {path}"); ok = False
    if bundle_hash(m["files"]) != m["bundle_sha256"]:
        say("bundle hash does not match the manifest"); ok = False
    chain_ok, detail = report.verify_chain(
        [line for line in (bundle / "raw/killswitch/audit.jsonl").read_text().splitlines() if line.strip()])
    say(detail)
    ok = ok and chain_ok
    say("Evidence bundle verified" if ok else "Evidence bundle FAILED verification")
    return ok
