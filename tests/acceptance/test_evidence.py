"""Evidence bundle (EVD-01..07): the P6 definition of done. A bundle from an S3 incident
verifies; regenerating it from the same inputs gives a byte-identical report.json; changing
any byte of any file fails verification."""

import hashlib
import json
import re
import shutil
import subprocess

import pytest

from conftest import ROOT, SECRETS


def make(*args, timeout=400):
    r = subprocess.run(["make", "-s", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout + r.stderr


@pytest.fixture(scope="module")
def s3_bundle():
    make("reset", timeout=200)
    code, out = make("demo", "SCENARIO=s3")
    assert code == 0, out
    incident = re.search(r"INCIDENT=(INC-[0-9a-f-]+)", out).group(1)
    code, out = make("evidence", f"INCIDENT={incident}")
    assert code == 0, out
    return ROOT / "evidence" / incident


def test_bundle_contents(s3_bundle):
    b = s3_bundle
    for f in ("report.html", "report.json", "manifest.json", "raw/environment.json", "raw/metrics.json",
              "raw/guard-decisions.jsonl", "raw/killswitch/audit.jsonl", "raw/config/apisix.json",
              "raw/config/guard-policy.json"):
        assert (b / f).is_file(), f
    assert list((b / "raw/traces").glob("*.json")) and list((b / "raw/logs").glob("*.jsonl"))
    env = json.loads((b / "raw/environment.json").read_text())
    assert env["git_sha"] and env["versions"]["APISIX_DIGEST"].startswith("sha256:")
    assert any(i["image_id"] for i in env["images"].values())
    policy = json.loads((b / "raw/config/guard-policy.json").read_text())
    assert len(policy["version"]) == 12


def test_report_tells_the_s3_story(s3_bundle):
    r = json.loads((s3_bundle / "report.json").read_text())
    s = r["summary"]
    assert any("agent:executor was tripped by the guard" in w for w in s["what_happened"])
    assert any(b["check"] == "mcp.tool_allowlist" and b["agent"] == "executor" for b in s["blocked"])
    c = s["containment"]
    assert c["verified"] and c["tripped_by"] == "guard" and c["violation_to_trip_ms"] is not None
    actions = [e["action"] for e in r["timeline"]]
    assert "hand-off to executor" in actions and "trip agent:executor" in actions
    assert any(a.startswith("tool actions__export_customer_data: denied") for a in actions)
    for e in r["timeline"]:  # EVD-03 fields and the EVD-07 placeholder
        assert {"ts", "component", "actor", "action", "decision", "trace_id", "span_id"} <= e.keys()
        assert e["control_refs"] == []
    assert r["timeline"] == sorted(r["timeline"], key=lambda e: e["ts"])
    assert r["integrity"]["audit_chain_valid"] is True


def test_no_secrets_and_excerpts_redacted(s3_bundle):
    secrets = [p.read_text().strip() for p in SECRETS.iterdir() if p.is_file()]
    for f in s3_bundle.rglob("*"):
        if f.is_file():
            text = f.read_text(errors="replace")
            assert not any(s and s in text for s in secrets), f"secret in {f}"
    apisix = json.loads((s3_bundle / "raw/config/apisix.json").read_text())
    for c in apisix["consumers"]:
        assert c["plugins"]["key-auth"]["key"] == "[removed]"
    report_text = json.dumps(json.loads((s3_bundle / "report.json").read_text())["timeline"])
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", report_text), "an email address reached the timeline"
    for line in (s3_bundle / "raw/guard-decisions.jsonl").read_text().splitlines():
        excerpt = json.loads(line).get("excerpt") or ""
        assert "@external-review.example" not in excerpt and len(excerpt) <= 500


def test_verify_passes(s3_bundle):
    code, out = make("evidence-verify", f"DIR={s3_bundle}")
    assert code == 0 and "Evidence bundle verified" in out, out


def test_report_json_is_deterministic(s3_bundle):
    before = (s3_bundle / "report.json").read_bytes()
    subprocess.run(["uv", "run", "-q", "--frozen", "--package", "cli", "demo", "evidence-rebuild", str(s3_bundle)],
                   cwd=ROOT / "services", check=True, capture_output=True)
    assert (s3_bundle / "report.json").read_bytes() == before  # EVD-05
    assert "generated_at" not in before.decode() and "generated_at" in (s3_bundle / "manifest.json").read_text()


@pytest.mark.parametrize("victim", ["report.json", "report.html", "raw/killswitch/audit.jsonl", "raw/metrics.json",
                                    "raw/traces/*.json", "raw/config/apisix.json"])
def test_any_changed_byte_fails_verification(s3_bundle, tmp_path, victim):
    copy = tmp_path / "bundle"
    shutil.copytree(s3_bundle, copy)
    target = next(copy.glob(victim))
    data = bytearray(target.read_bytes())
    data[len(data) // 2] ^= 0x01
    target.write_bytes(bytes(data))
    code, out = make("evidence-verify", f"DIR={copy}")
    assert code != 0 and "FAILED" in out, out


def test_rehashed_manifest_cannot_hide_an_audit_edit(s3_bundle, tmp_path):
    """Even with the manifest recomputed, editing an audit event breaks the hash chain."""
    copy = tmp_path / "bundle"
    shutil.copytree(s3_bundle, copy)
    audit = copy / "raw/killswitch/audit.jsonl"
    lines = audit.read_text().splitlines()
    e = json.loads(lines[-1])
    e["reason"] = "routine maintenance"
    lines[-1] = json.dumps(e, sort_keys=True)
    audit.write_text("\n".join(lines) + "\n")
    m = json.loads((copy / "manifest.json").read_text())
    m["files"]["raw/killswitch/audit.jsonl"] = hashlib.sha256(audit.read_bytes()).hexdigest()
    m["bundle_sha256"] = hashlib.sha256("".join(f"{h}  {p}\n" for p, h in sorted(m["files"].items())).encode()).hexdigest()
    (copy / "manifest.json").write_text(json.dumps(m, indent=2, sort_keys=True))
    code, out = make("evidence-verify", f"DIR={copy}")
    assert code != 0 and "hash does not match its content" in out, out


def test_evidence_by_trace(s3_bundle):
    trace = json.loads((s3_bundle / "report.json").read_text())["subject"]["trace_ids"][0]
    code, out = make("evidence", f"TRACE={trace}")
    assert code == 0, out
    r = json.loads((ROOT / "evidence" / trace / "report.json").read_text())
    assert r["subject"]["kind"] == "trace" and r["subject"]["trace_ids"] == [trace]
    assert make("evidence-verify", f"DIR={ROOT / 'evidence' / trace}")[0] == 0
