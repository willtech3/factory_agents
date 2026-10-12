"""ADR-0016: evals from recordings. A session step records what the session was asked as well as what it returned, a
run's recorded sessions export to a dataset with what the run made of them, `evals/run_eval.py` grades them again or asks
another agent version, and a changed prompt or rubric needs an eval receipt for its exact bytes. No network."""
import importlib.util
import json
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
from test_implement_bookkeeping import TEST_ID, _ctx, _spec

from factory import receipts, recordings
from factory.config import REPO_ROOT
from factory.stages import common
from factory.stages.implement import run_implement_one

URL = "https://github.com/w/app"


def _built(tmp_path, root, examples):
    tmp_path.mkdir(parents=True, exist_ok=True)
    ctx, verifier = _ctx(tmp_path, root, examples, mapping=[TEST_ID])
    verdict = run_implement_one(ctx, _spec(examples), URL, "main", verifier, {})
    assert verdict["result"] == "ready"
    return ctx


def _run_eval():
    spec = importlib.util.spec_from_file_location("run_eval", REPO_ROOT / "evals" / "run_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_session_step_records_what_it_was_asked(tmp_path, root, examples, monkeypatch):
    def implementer_inputs(ctx):
        [step] = [s for s in ctx.store.steps(ctx.run_id) if s.key.startswith("session/implement/")]
        return step, json.loads(ctx.store.get_blob(ctx.run_id, step.result["inputs"]))
    ctx = _built(tmp_path / "a", root, examples)
    step, inputs = implementer_inputs(ctx)
    assert inputs["agent_key"] == "implementer" and inputs["stage"] == "implement" and "Implement specification" in inputs["description"]
    assert inputs["work_item_id"] == "wi-smart-retries" and step.result["spec_id"] == "spec_smart-retries"
    spec = inputs["uploads"]["spec.json"]
    assert json.loads(ctx.store.get_blob(ctx.run_id, spec["blob"]))["spec_id"] == "spec_smart-retries"
    # an upload above the limit (a hosted verification's source archive) is recorded by sha256 only
    monkeypatch.setattr(common, "RECORDED_UPLOAD_BYTES", 100)
    _, inputs = implementer_inputs(_built(tmp_path / "b", root, examples))
    assert all((u["blob"] is None) == (u["bytes"] > 100) and len(u["sha256"]) == 64 for u in inputs["uploads"].values())
    assert inputs["uploads"]["spec.json"]["blob"] is None


def test_a_runs_sessions_become_eval_items_with_what_the_run_made_of_them(tmp_path, root, examples):
    ctx = _built(tmp_path, root, examples)
    found = {i.agent_key: i for i in recordings.items(ctx.store, ctx.run_id)}
    assert set(found) == {"implementer", "reviewer", "merge_judge"}
    implementer = found["implementer"]
    assert implementer.pipeline_step == "implement/spec_smart-retries/r1/author"
    assert implementer.labels["next"] == "judge" and {c["check"] for c in implementer.labels["checks"]} >= {"diff_scope", "report_schema"}
    assert found["reviewer"].labels["next"] == "judge" and len(found["reviewer"].labels["merged_findings"]) == 64
    assert found["merge_judge"].labels["result"] == "ready" and "merge_readiness.json" in found["merge_judge"].outputs
    assert [i.agent_key for i in recordings.items(ctx.store, ctx.run_id, {"reviewer"})] == ["reviewer"]


def test_an_export_carries_every_blob_and_is_added_to_once(tmp_path, root, examples):
    ctx = _built(tmp_path / "run", root, examples)
    dest = tmp_path / "dataset"
    assert recordings.export(ctx.store, ctx.run_id, dest) == 3 and recordings.export(ctx.store, ctx.run_id, dest) == 0
    [judge] = [i for i in recordings.load(dest) if i["agent_key"] == "merge_judge"]
    uploads = recordings.uploads(dest, judge)
    assert uploads["spec.json"] and json.loads(uploads["deterministic_checks.json"])
    assert recordings.spec_of(dest, judge)["spec_id"] == "spec_smart-retries"
    assert json.loads(recordings.blob(dest, judge["outputs"]["merge_readiness.json"]))["result"] == "ready"


def test_the_grade_is_the_pipelines_receipt_check_and_decision_rules(schemas_dir, examples):
    spec = examples["spec_smart_retries"]
    verdict = dict(examples["merge_readiness"])
    ok = recordings.grade("merge_judge", {"merge_readiness.json": json.dumps(verdict).encode()}, schemas_dir, spec)
    assert ok["ok"] and ok["result"] == "ready" and ok["rule_violations"] == []
    unmet = {**verdict, "criteria": [{**c, "status": "not_met"} if c["ac_id"] == "AC-1" else c for c in verdict["criteria"]]}
    broken = recordings.grade("merge_judge", {"merge_readiness.json": json.dumps(unmet).encode()}, schemas_dir, spec)
    assert not broken["ok"] and broken["rule_violations"] == ["AC-1 is not_met, not met"]
    assert not recordings.grade("reviewer", {"findings.json": b'{"findings": []}'}, schemas_dir)["ok"]
    assert recordings.grade("reviewer", {}, schemas_dir) == {"ok": False, "missing": "findings.json"}


class Replaying:
    """Stands in for the configured agent version: answers each session with what `answer` returns for it."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def run(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(outputs=self.answer(kw), list_cost_usd=0.5, session_id=f"sesn_eval_{len(self.calls)}")


def test_a_replay_asks_the_recorded_session_again_and_records_a_receipt(tmp_path, root, examples):
    ctx = _built(tmp_path / "run", root, examples)
    dest = tmp_path / "dataset"
    recordings.export(ctx.store, ctx.run_id, dest)
    [judge] = [i for i in recordings.load(dest) if i["agent_key"] == "merge_judge"]
    recorded = {n: recordings.blob(dest, sha) for n, sha in judge["outputs"].items()}
    repo = tmp_path / "repo"
    for rel in ("agents", "rubrics", "schemas"):
        shutil.copytree(REPO_ROOT / rel, repo / rel)
    run_eval = _run_eval()
    driver = Replaying(lambda kw: recorded)
    args = SimpleNamespace(recorded=dest, agent="merge_judge", replay=True, runs=2, receipt=True)
    summary = run_eval.run_recorded(args, driver=driver, root=repo)
    assert len(driver.calls) == 2 and driver.calls[0]["uploads"] == recordings.uploads(dest, judge)
    assert driver.calls[0]["description"] == judge["inputs"]["description"] and driver.calls[0]["agent_key"] == "merge_judge"
    assert summary["recorded"]["ok"] == 1 and summary["replayed"]["ok"] == 2 and not summary["regressed"]
    assert summary["same_result"][0] == 1.0 and summary["receipt"] == "recorded"
    receipt = receipts.load(repo)["merge_judge"]
    assert receipt["decision"] == "replayed" and receipt["files"] == receipts.fingerprints(repo, "merge_judge")
    # a version whose verdicts no longer validate is not recorded as having passed
    broken = run_eval.run_recorded(SimpleNamespace(recorded=dest, agent="merge_judge", replay=True, runs=2, receipt=False),
                                   driver=Replaying(lambda kw: {"merge_readiness.json": b"{}"}), root=repo)
    assert broken["replayed"]["ok"] == 0


def test_the_decision_rule_needs_the_replay_wholly_below_the_recorded_versions(tmp_path):
    run_eval = _run_eval()
    from graders import stochastic as sto

    def rate(ok, n):
        return {"n": n, "ok": ok, "posterior": sto.beta_credible_interval(ok, n)}
    assert run_eval.regressed(rate(19, 20), rate(2, 20))
    assert not run_eval.regressed(rate(19, 20), rate(17, 20)) and not run_eval.regressed(rate(1, 1), rate(0, 1))


def test_a_changed_prompt_or_rubric_needs_a_receipt(tmp_path):
    for rel in ("agents", "rubrics"):
        shutil.copytree(REPO_ROOT / rel, tmp_path / rel)
    (tmp_path / "evals").mkdir()
    shutil.copy(REPO_ROOT / "evals" / "receipts.json", tmp_path / "evals" / "receipts.json")
    assert receipts.stale(tmp_path) == {}
    prompt = tmp_path / "agents" / "reviewer.md"
    prompt.write_text(prompt.read_text() + "\nOne more instruction.\n")
    rubric = tmp_path / "rubrics" / "review_quality.md"
    rubric.write_text(rubric.read_text() + "\n")
    assert receipts.stale(tmp_path) == {"reviewer": ["agents/reviewer.md", "rubrics/review_quality.md"],
                                        "merge_judge": ["rubrics/review_quality.md"]}
    with pytest.raises(ValueError, match="reason"):
        receipts.record(tmp_path, "reviewer", "waived", reason=" ")
    receipts.record(tmp_path, "reviewer", "waived", reason="wording only", by="owner")
    assert receipts.stale(tmp_path) == {"merge_judge": ["rubrics/review_quality.md"]}


def test_the_repository_has_a_receipt_for_every_prompt_and_rubric():
    """What CI runs: evals/gate.py, with the standard library only."""
    out = subprocess.run([sys.executable, "-I", str(REPO_ROOT / "evals" / "gate.py")], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout
