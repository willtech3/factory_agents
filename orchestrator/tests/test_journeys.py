"""A UI change proved by the factory's own browser runner on a journey that is a run artifact (post-mortem 2026-10-11 §1).

The whole managed loop with the test doubles of tests/support/hosted.py: the implementer writes
/mnt/session/outputs/journeys/greet.json beside its report, the controller validates it, uploads it with the runner and
binds its sha256 into the verification record, and the real harness runs the runner on it (a scripted browser stands in
for Chromium). The change's diff holds no browser-test file; code that answers the factory's evidence protocol is refused."""
import json
import os
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from support.hosted import (
    REPORT,
    RUN_ID,
    Driver,
    artifact,
    context,
    push,
    remote,
    result,
    sandbox_double,  # noqa: F401  (autouse: no login shell, nothing to health-check)
    spec,
)

from factory import checks
from factory.sandbox_tools import environment_runner as er
from factory.stages.implement import LocalVerifier, latest_journeys, run_implement_one, standing_findings

SCRIPTED = Path(__file__).parent / "support" / "scripted_browser.py"
JOURNEY = {"version": 1, "name": "greet", "input": "pointer", "steps": [
    {"name": "Open the page", "actions": [{"do": "goto", "path": "/"}, {"do": "expect_visible", "role": "button", "name": "Show greeting"}]},
    {"name": "Click Show greeting", "actions": [{"do": "click", "role": "button", "name": "Show greeting"}, {"do": "expect_text", "text": "Hello"}]}]}
PAGE = {"targets": ["role button Show greeting"], "texts": [], "on_click": {"role button Show greeting": {"texts": ["Hello, visitor"]}},
        "viewport": {"width": 1024, "height": 700}}


@pytest.fixture(autouse=True)
def scripted_runner(monkeypatch):
    """The harness runs the factory runner on the uploaded journey; here the runner drives the scripted browser."""
    monkeypatch.setattr(er, "JOURNEY_COMMAND", f"{sys.executable} {SCRIPTED} --journey {{journey}} # {{runner}}")
    monkeypatch.setenv("SCRIPTED_PAGE", json.dumps(PAGE))


def sandbox(kw, scratch: Path):
    """The hosted session with its uploads on disk (the runner and the journey arrive as uploads)."""
    request = json.loads(kw["uploads"]["environment-request.json"])
    root = scratch / f"sandbox-{len(list(scratch.glob('sandbox-*')))}"
    repo, logs, evidence, outputs, uploads = (root / d for d in ("repo", "logs", "evidence", "outputs", "uploads"))
    for d in (repo, logs, evidence, outputs, uploads):
        d.mkdir(parents=True)
    for name, content in kw["uploads"].items():
        (uploads / name).parent.mkdir(parents=True, exist_ok=True)
        (uploads / name).write_bytes(content)
    with tarfile.open(uploads / "test-source.tar") as archive:
        archive.extractall(repo, filter="data")
    profile = json.loads((repo / request["profile_path"]).read_text())
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}", "BASE_URL": "http://localhost:8080",
           "FACTORY_EVIDENCE_DIR": str(evidence), "FACTORY_TEST_HEAD": request["head_sha"]}
    report = {**request, "status": "blocked", "phases": [], "services": []}
    er.run_change(profile, request["change"], repo, env, time.monotonic() + 300, logs, evidence, root, outputs, report, uploads)
    report["phases"].append({"phase": "stop", **er.command(profile["commands"]["stop"], repo, env, 30, logs / "stop.log")})
    report.pop("_uploaded_bytes", None)
    return result({**{p.name: p.read_bytes() for p in outputs.iterdir()}, "environment-result.json": json.dumps(report).encode()})


class JourneyDriver(Driver):
    def __init__(self, *a, journey=JOURNEY, journey_filename="journeys/greet.json", findings=None, **kw):
        super().__init__(*a, **kw)
        self.journey, self.journey_filename, self.findings = journey, journey_filename, findings

    def run(self, **kw):
        self.calls.append(kw)
        if kw.get("stage") == "verify":
            return sandbox(kw, self.scratch)
        if kw["agent_key"] == "implementer":
            push(self.origin, self.scratch, f"factory/{RUN_ID}/spec_hosted", self.change_files, len(self.calls))
            outputs = {"implementation_report.json": json.dumps(REPORT).encode()}
            if self.journey is not None:
                outputs[self.journey_filename] = json.dumps(self.journey).encode()
            if "findings.json" in kw["uploads"]:
                answered = json.loads(kw["uploads"]["findings.json"])["findings"]
                outputs["review_response.json"] = json.dumps({"responses": [{"finding_id": f["finding_id"], "disposition": "fixed",
                                                                            "explanation": "done"} for f in answered]}).encode()
            return result(outputs)
        if kw["agent_key"] in ("reviewer", "security_reviewer") and self.findings is not None:
            return result({"findings.json": json.dumps({"findings": self.findings, "checks_performed": [], "spec_conformance": []}).encode()})
        self.calls.pop()
        return super().run(**kw)


def _loop(tmp_path, root, **driver_kw):
    origin = remote(tmp_path)
    driver = JourneyDriver(origin, tmp_path, **driver_kw)
    ctx = context(tmp_path, root, driver)
    verdict = run_implement_one(ctx, spec("factory:journey greet"), str(origin), "main",
                                LocalVerifier(str(origin), tmp_path / "runs" / RUN_ID / "verify"), {})
    return ctx, driver, verdict


@pytest.mark.parametrize("journey_filename", ["journeys/greet.json", "greet.json"])
def test_a_ui_change_is_proved_by_the_factory_runner_and_adds_no_browser_test_files(tmp_path, root, journey_filename):
    # The live provider lists a nested output using only its basename. Both shapes must reach the hosted runner.
    ctx, driver, verdict = _loop(tmp_path, root, journey_filename=journey_filename)
    assert verdict["result"] == "ready"
    verify = next(kw for kw in driver.calls if kw.get("stage") == "verify")
    assert {"tools/browser_runner.py", "journeys/greet.json"} <= set(verify["uploads"])
    request = json.loads(verify["uploads"]["environment-request.json"])
    import hashlib
    digest = hashlib.sha256(json.dumps(JOURNEY).encode()).hexdigest()
    assert request["journeys"] == {"greet": digest} and request["change"]["e2e"][0]["journey"] == {"name": "greet", "sha256": digest}
    hosted = artifact(ctx, "implement", "spec_hosted-r1-hosted_verification.json")
    assert hosted["bound"] and hosted["journeys"] == [{"name": "greet", "sha256": digest}]
    row = artifact(ctx, "implement", "spec_hosted-r1-test_results.json")["e2e"][0]
    assert row["exit_code"] == 0 and row["visual"]["status"] == "passed" and row["visual"]["runner"].startswith("factory browser runner")
    assert row["visual"]["viewport_observed"] == {"width": 1024, "height": 700}
    det = {d["check"]: d["passed"] for d in artifact(ctx, "implement", "spec_hosted-r1-deterministic_checks.json")}
    assert det["visual_e2e"] and det["e2e_preview"] and det["factory_proof_in_diff"]
    # the journey is evidence for the reviewers and the judge, never a file in the change
    for key in ("reviewer", "merge_judge"):
        assert "journeys/greet.json" in next(kw for kw in driver.calls if kw["agent_key"] == key)["uploads"]
    assert "target\": \"verification\"" in next(kw for kw in driver.calls if kw["agent_key"] == "reviewer")["description"]
    changed = LocalVerifier(str(tmp_path / "origin.git"), tmp_path / "check").checkout(f"factory/{RUN_ID}/spec_hosted")
    from subprocess import run as sh
    files = sh(["git", "diff", "--name-only", "origin/main..HEAD"], cwd=changed, capture_output=True, text=True, check=True).stdout.split()
    assert files == ["app/main.py"]


def test_a_missing_or_malformed_journey_blocks_the_entry_before_any_browser_runs(tmp_path, root):
    ctx, driver, verdict = _loop(tmp_path, root, journey=None)
    assert verdict["result"] != "ready"
    row = artifact(ctx, "implement", "spec_hosted-r1-test_results.json")["e2e"][0]
    assert row["exit_code"] is None and "wrote no journeys/greet.json" in row["visual"]["summary"]
    verify = next(kw for kw in driver.calls if kw.get("stage") == "verify")
    assert "journeys/greet.json" not in verify["uploads"] and "tools/browser_runner.py" not in verify["uploads"]
    bad = {**JOURNEY, "steps": JOURNEY["steps"][:1]}
    ctx, _, _ = _loop(tmp_path / "b", root, journey=bad)
    summary = artifact(ctx, "implement", "spec_hosted-r1-test_results.json")["e2e"][0]["visual"]["summary"]
    assert "not a runnable journey" in summary and "1 steps but the spec's visual contract has 2" in summary


def test_journey_recovery_uses_latest_round_across_flat_and_nested_outputs(tmp_path):
    ctx = SimpleNamespace(cfg=SimpleNamespace(runs_dir=tmp_path), run_id=RUN_ID)
    directory = tmp_path / RUN_ID / "artifacts" / "implement"
    directory.mkdir(parents=True)
    (directory / "spec_hosted-r1-journeys").mkdir()
    (directory / "spec_hosted-r1-journeys/greet.json").write_bytes(b"old nested journey")
    (directory / "spec_hosted-r2-greet.json").write_bytes(b"new flat journey")
    (directory / "spec_hosted-r9-implementation_report.json").write_bytes(b"unrelated report")
    (directory / "other_spec-r9-greet.json").write_bytes(b"another spec")
    assert latest_journeys(ctx, "spec_hosted", spec("factory:journey greet")) == {"greet": b"new flat journey"}
    (directory / "spec_hosted-r3-journeys").mkdir()
    (directory / "spec_hosted-r3-journeys/greet.json").write_bytes(b"newest nested journey")
    assert latest_journeys(ctx, "spec_hosted", spec("factory:journey greet")) == {"greet": b"newest nested journey"}


def test_conflicting_flat_and_nested_journeys_cannot_silently_choose_different_evidence(tmp_path):
    ctx = SimpleNamespace(cfg=SimpleNamespace(runs_dir=tmp_path), run_id=RUN_ID)
    directory = tmp_path / RUN_ID / "artifacts" / "implement"
    (directory / "spec_hosted-r1-journeys").mkdir(parents=True)
    (directory / "spec_hosted-r1-journeys/greet.json").write_bytes(b"nested journey")
    (directory / "spec_hosted-r1-greet.json").write_bytes(b"different flat journey")
    with pytest.raises(ValueError, match="conflicting journey outputs for greet"):
        latest_journeys(ctx, "spec_hosted", spec("factory:journey greet"))


def test_the_harness_refuses_a_journey_that_differs_from_the_one_bound(tmp_path):
    uploads = tmp_path / "uploads"
    (uploads / "journeys").mkdir(parents=True)
    (uploads / "tools").mkdir()
    (uploads / "tools" / "browser_runner.py").write_text("")
    (uploads / "journeys" / "greet.json").write_text(json.dumps(JOURNEY))
    entry = {"journey": {"name": "greet", "sha256": "0" * 64}}
    assert er.journey_command(entry, uploads) == (None, "the journey is missing or differs from the one the controller bound")
    import hashlib
    entry["journey"]["sha256"] = hashlib.sha256((uploads / "journeys" / "greet.json").read_bytes()).hexdigest()
    command, refused = er.journey_command(entry, uploads)
    assert refused is None and str(uploads / "journeys" / "greet.json") in command


def test_code_answering_the_evidence_protocol_is_refused(tmp_path, root):
    """evergreen_chat#120 committed a 1,001-line browser_journey.py that read FACTORY_VISUAL_REQUEST."""
    runner = "import os, json\nreq = json.load(open(os.environ['FACTORY_VISUAL_REQUEST']))\n"
    ctx, _, verdict = _loop(tmp_path, root, change_files={"app/main.py": "X = 1\n", "verification/greet/browser_journey.py": runner})
    det = {d["check"]: d for d in artifact(ctx, "implement", "spec_hosted-r1-deterministic_checks.json")}
    assert not det["factory_proof_in_diff"]["passed"] and "verification/greet/browser_journey.py" in det["factory_proof_in_diff"]["detail"]
    assert verdict["result"] != "ready"
    assert checks.factory_proof_in_diff({"tests/e2e/chat.spec.ts": ["await page.goto(baseURL)"]}).passed   # a kept e2e suite is fine


def test_a_finding_about_the_journeys_own_plumbing_never_stands(tmp_path, root):
    plumbing = {"finding_id": "F-1", "severity": "blocking", "category": "test_quality", "status": "open", "target": "verification",
                "claim": "the journey reports a 403 redirect as failed", "evidence": {"locations": ["journeys/greet.json:1"]}}
    assert standing_findings([plumbing]) == [] and standing_findings([{**plumbing, "target": "change"}])
    ctx, _, verdict = _loop(tmp_path, root, findings=[plumbing])
    assert verdict["result"] == "ready"
    backlog = (tmp_path / "runs" / "_backlog" / "verification-findings.jsonl").read_text().splitlines()
    assert json.loads(backlog[0])["finding"]["claim"] == plumbing["claim"]
    assert any(e.kind == "verification.backlog" for e in ctx.store.events(RUN_ID))
