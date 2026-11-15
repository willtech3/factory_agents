"""The security pass: deterministic pre-checks, pass tagging through the merge, and the standing rule."""

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


def test_prechecks_include_the_dependency_delta_and_dangerous_api_grep_the_prompt_promises(tmp_path, monkeypatch):
    """agents/security_reviewer.md tells the reviewer security_prechecks.json carries a dependency delta and a
    dangerous-API grep; both read only the diff, so the managed (execute-nothing) variant has them too."""
    from factory.stages.implement import managed_prechecks

    monkeypatch.setattr(security.shutil, "which", lambda name: None)
    added = {"pyproject.toml": ['requests = ">=2"'], "app/x.py": ["data = pickle.loads(blob)", "r = subprocess.run(cmd, shell=True)", "ok = 1"],
             ".github/workflows/ci.yml": ["- run: echo hi"]}
    for out in (security.prechecks(tmp_path, list(added), added, deleted_files=["requirements.txt", "app/old.py"]),
                managed_prechecks(added, ["requirements.txt", "app/old.py"])):
        assert out["dependency_delta"]["files"] == [".github/workflows/ci.yml", "pyproject.toml"]
        assert out["dependency_delta"]["deleted_manifest_files"] == ["requirements.txt"]
        assert [(h["file"], h["added_line_index"]) for h in out["dangerous_api_grep"]] == [("app/x.py", 1), ("app/x.py", 2)]
        assert out["dangerous_api_grep"][0]["pattern"].startswith("pickle.loads")


def test_a_security_finding_written_as_the_prompt_asks_is_schema_valid(schemas_dir, examples):
    """The prompt asks for confidence, scope and (blocking or major) a CVSS 4.0 vector, and evidence.executed; the
    schema's Finding is closed (additionalProperties false), so without them every such findings.json was refused."""
    import copy

    doc = copy.deepcopy(examples["findings_round1"])
    finding = dict(_f("F-1", "blocking", "security", "app/routes.py:42", repro="curl -s /api/notes/2 -H 'Authorization: Bearer u1'"),
                   cwe="CWE-639", attack_path="GET /api/notes/{id} -> notes.get(id) with no owner check", exploitability="trivial", violates=["AC-2"],
                   impact="high", confidence="proven", scope="introduced",
                   cvss_vector="CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/VC:H/VI:N/VA:N/SC:N/SI:N/SA:N")
    finding["evidence"].update(executed=True, observed="200 {\"owner\": \"u2\"}")
    doc["findings"] = [finding]
    schema = schemas_dir / "review_findings.schema.json"
    res = checks.schema_valid(doc, schema, schemas_dir)
    assert res.passed, res.detail
    for bad in ({"confidence": "certain"}, {"scope": "everywhere"}, {"cvss_vector": "CVSS:3.1/AV:N"}):
        assert not checks.schema_valid({**doc, "findings": [{**finding, **bad}]}, schema, schemas_dir).passed
