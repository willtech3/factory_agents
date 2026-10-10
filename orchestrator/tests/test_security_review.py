"""The security pass: deterministic pre-checks, pass tagging through the merge, and the standing rule."""
from pathlib import Path

from factory import checks, security
from factory.stages.implement import merge_findings, standing_findings, tag_pass


def test_secret_scan_hits_vendor_shapes_and_reports_suspects_only():
    added = {"app/config.py": ["AWS_KEY = 'AKIAABCDEFGHIJKLMNOP'", "token = 'ghp_" + "a" * 40 + "'", "password = 'hunter2hunter2hunter2'", "x = 1"],
             "tests/test_x.py": ["-----BEGIN RSA PRIVATE KEY-----"]}
    r = security.secret_scan(added)
    assert {h["kind"] for h in r["hits"]} == {"aws_access_key", "github_token", "private_key_block"}
    assert all(len(h["excerpt"]) <= 25 for h in r["hits"])                       # never the whole credential
    assert [s["added_line_index"] for s in r["suspects"]] == [3]
    assert not checks.secret_scan(added).passed
    assert checks.secret_scan({"app/x.py": ["password = os.environ['PASSWORD']"]}).passed


def test_prechecks_skip_cleanly_without_scanners(tmp_path, monkeypatch):
    monkeypatch.setattr(security.shutil, "which", lambda name: None)
    out = security.prechecks(tmp_path, ["app/x.py"], {"app/x.py": ["x = 1"]})
    assert out["secret_scan"] == {"hits": [], "suspects": []}
    assert out["dependency_audit"]["status"] == "skipped" and out["sast"]["status"] == "skipped"


def _f(fid, sev, cat, loc, status="open", repro=None):
    ev = {"locations": [loc]}
    if repro:
        ev["reproduction"] = repro
    return {"finding_id": fid, "severity": sev, "category": cat, "claim": fid, "evidence": ev, "violates": "x",
            "refutation_condition": "y", "status": status}


def test_security_pass_merges_with_its_tag_and_reproduced_races_stand():
    p1 = tag_pass({"checks_performed": [{"what": "a"}], "findings": [_f("F-1", "minor", "concurrency", "app/s.py:10", repro="probe.py -> 70 torn")]}, "1")
    p2 = tag_pass({"checks_performed": [], "findings": [_f("F-1", "minor", "concurrency", "app/s.py:10")]}, "2")
    sec = tag_pass({"checks_performed": [{"what": "secret scan"}], "findings": [
        dict(_f("F-1", "major", "security", "app/main.py:42", repro="curl ... -> 200 with another tenant's flag"), cwe="CWE-639")]}, "security")
    merged = merge_findings([p1, p2, sec], "spec_x", 1, "b", "h", "br")
    by_loc = {f["evidence"]["locations"][0]: f for f in merged["findings"]}
    assert by_loc["app/main.py:42"]["pass"] == "security" and by_loc["app/main.py:42"]["cwe"] == "CWE-639"
    assert by_loc["app/s.py:10"]["pass"] == "1"                                   # first pass wins the duplicate
    assert len(merged["checks_performed"]) == 2
    standing = standing_findings(merged["findings"])
    assert {f["evidence"]["locations"][0] for f in standing} == {"app/main.py:42", "app/s.py:10"}  # the reproduced minor race stands
    assert standing_findings([_f("F-9", "minor", "concurrency", "a:1")]) == []                       # unreproduced minor does not
    assert standing_findings([_f("F-9", "major", "security", "a:1", status="withdrawn")]) == []


def test_security_reviewer_is_a_known_agent_and_policy_knob():
    from factory.config import AGENT_FILES
    from factory.state import Policy
    assert AGENT_FILES["security_reviewer"] == "./agents/security_reviewer.md"
    assert Policy().security_review is True and Policy().stage_budget_usd["security_review"] > 0


def test_prechecks_include_dependency_delta_and_dangerous_api_grep(tmp_path, monkeypatch):
    monkeypatch.setattr(security.shutil, "which", lambda name: None)
    added = {"pyproject.toml": ['requests = ">=2"'], "app/x.py": ["data = pickle.loads(blob)", "r = subprocess.run(cmd, shell=True)", "ok = 1"],
             ".github/workflows/ci.yml": ["- run: echo hi"]}
    out = security.prechecks(tmp_path, list(added), added, deleted_files=["requirements.txt"])
    assert out["dependency_delta"]["files"] == [".github/workflows/ci.yml", "pyproject.toml"] and out["dependency_delta"]["deleted_manifest_files"] == ["requirements.txt"]
    assert [h["added_line_index"] for h in out["dangerous_api_grep"]] == [1, 2] and out["dangerous_api_grep"][0]["pattern"].startswith("pickle.loads")


def test_security_findings_are_normalised_and_advisory_ones_never_stand():
    from factory.stages.implement import normalise_security_findings
    f = _f("S-1", "blocking", "security", "app/a.py:1")                       # no confidence -> pattern -> capped at minor
    g = dict(_f("S-2", "major", "security", "app/b.py:1", repro="curl ..."), confidence="proven", scope="introduced")
    g["evidence"]["executed"] = True
    h = dict(_f("S-3", "blocking", "security", "app/c.py:1"), confidence="reachable", scope="pre_existing")
    out = normalise_security_findings({"findings": [f, g, h]})["findings"]
    assert out[0]["severity"] == "minor" and out[0]["severity_as_filed"] == "blocking" and out[0]["confidence"] == "pattern"
    assert out[1]["severity"] == "major"
    assert out[2]["severity"] == "minor"
    tagged = tag_pass({"findings": out}, "security")["findings"]
    assert [x["finding_id"] for x in standing_findings(tagged)] == ["S-2"]           # proven and executed stands; the capped ones do not
    for x in tagged: x["advisory"] = True
    assert standing_findings(tagged) == []                                           # advisory mode: nothing from the pass stands


def test_merge_records_corroboration_across_passes():
    p1 = tag_pass({"checks_performed": [], "findings": [_f("F-1", "major", "security", "app/main.py:42")]}, "1")
    sec = tag_pass({"checks_performed": [], "findings": [_f("F-1", "major", "security", "app/main.py:42", repro="x")]}, "security")
    merged = merge_findings([p1, sec], "spec_x", 1, "b", "h", "br")
    assert len(merged["findings"]) == 1 and merged["findings"][0]["corroborated_by"] in (["1"], ["security"])


def test_security_review_mode_knob():
    from factory.state import Policy
    assert Policy().security_review_mode == "advisory"
