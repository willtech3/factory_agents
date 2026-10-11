"""One spec through implement -> review -> merge judge with fakes (a real git repository only for the shas; no network):
the hand-offs and bookkeeping the change request and the Console rely on (post-mortem issues 1, 5, 16, 18)."""
import copy
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

from conftest import make_config

from factory.checks import CheckResult
from factory.gates import InMemoryQueue, LogNotifier
from factory.runner import _sessions_for
from factory.stages import RunContext
from factory.stages.implement import SETUP_CHECK, run_implement_one
from factory.state import Policy, WorkItemStage
from factory.store import RunStore

RUN, SID, WID = "run_loop0000001", "spec_smart-retries", "wi-smart-retries"
TEST_ID = "tests/test_retries.py::test_retry_schedule"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path, root: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.email", "dev@example.com"); _git(repo, "config", "user.name", "dev")
    shutil.copy(root / "examples" / "ledgerline" / "service_manifest.json", repo / "service_manifest.json")
    _git(repo, "add", "."); _git(repo, "commit", "-q", "-m", "base")
    return repo


class Verifier:
    """The orchestrator's checkout, canned: a real repository for `rev-parse`, a one-file diff inside allowed_paths, and
    a pytest run whose passed set is what the test chooses."""

    def __init__(self, repo: Path, passed: list[str], collected: list[str]):
        self.repo, self.passed, self.collected = repo, passed, collected

    def checkout(self, branch: str) -> Path:
        return self.repo

    def prepare_environment(self, repo, manifest=None):
        self.setup = CheckResult(SETUP_CHECK, True, "canned environment", {"env_dir": "canned", "installer": "none", "python": "3", "install": "nothing"})
        return self.setup

    def analyse(self, repo, base_sha, head_sha):
        f = "app/services/billing/stripe_webhook_handler.rb"
        return {"changed": [f], "deleted": [], "added_lines": {f: ["attempts.create!(event_id: event.id)"]}, "existing_tests": [],
                "patch": f"diff --git a/{f} b/{f}\n+attempts.create!\n"}

    def run_commands(self, repo, commands, timeout=0):
        return [{"command": c, "exit_code": 0, "stdout_tail": "ok", "stderr_tail": "",
                 "tests": {"framework": "pytest", "report": "junit", "status": "reported",
                           "passed": self.passed, "failed": [], "skipped": []}} for c in commands]

    def collect_test_ids(self, repo, timeout=0):
        return self.collected

    def run_e2e(self, preview_url, entries, repo=None, target="preview", timeout=0):
        return []


class Driver:
    """Implementer, one reviewer pass, merge judge. Every agent writes the placeholder provenance real sessions wrote."""

    PLACEHOLDER = {"agent_name": "x", "agent_id": "agent_unknown", "agent_version": 1, "session_id": "sesn_unknown",
                   "model": "claude", "produced_at": "2026-10-10T12:00:00Z"}

    def __init__(self, ctx, examples, mapping):
        self.ctx, self.examples, self.mapping, self.calls = ctx, examples, mapping, []

    def run(self, **kw):
        self.calls.append(kw)
        key = kw["agent_key"]
        sid = f"sesn_{key}_{sum(1 for c in self.calls if c['agent_key'] == key)}"
        self.ctx.event("session.started", {"session_id": sid, "agent_id": f"agent_{key}", "agent_key": key, "title": kw["title"]},
                       stage=kw["stage"], actor="platform", work_item_id=kw.get("work_item_id"))
        if key == "implementer":
            report = {"spec_id": SID, "criteria": [{"ac_id": ac, "tests": self.mapping} for ac in ("AC-1", "AC-2", "AC-3", "AC-4", "AC-5", "AC-7", "AC-8")],
                      "commands_run": [{"command": "python -m pytest -q", "exit_code": 0}], "files_changed": ["app/services/billing/stripe_webhook_handler.rb"],
                      "produced_by": dict(self.PLACEHOLDER)}
            out = {"implementation_report.json": json.dumps(report).encode()}
            if "findings.json" in kw["uploads"]:
                ids = [f["finding_id"] for f in json.loads(kw["uploads"]["findings.json"])["findings"]]
                out["review_response.json"] = json.dumps({"responses": [{"finding_id": i, "disposition": "fixed", "explanation": "mapped real tests"} for i in ids],
                                                          "produced_by": dict(self.PLACEHOLDER)}).encode()
            return self._res(sid, f"agent_{key}", out, 3.3)
        if key == "reviewer":
            f = copy.deepcopy(self.examples["findings_round1"])
            f["findings"] = []
            f["produced_by"] = dict(self.PLACEHOLDER)
            return self._res(sid, f"agent_{key}", {"findings.json": json.dumps(f).encode()}, 2.6)
        v = copy.deepcopy(self.examples["merge_readiness"])
        v.update(result="ready", round=7, produced_by=dict(self.PLACEHOLDER))
        return self._res(sid, f"agent_{key}", {"merge_readiness.json": json.dumps(v).encode()}, 0.9)

    @staticmethod
    def _res(sid, agent, outputs, cost):
        return SimpleNamespace(session_id=sid, agent_id=agent, agent_version=1, outcome_result="satisfied", outcome_iterations=1,
                               list_cost_usd=cost, stop_reason="end_turn", events_seen=1, custom_tool_calls=[], outputs=outputs)


def _spec(examples):
    spec = copy.deepcopy(examples["spec_smart_retries"])
    spec["test_plan"]["e2e"] = []                                   # no preview in this harness
    for ac in spec["acceptance_criteria"]:
        ac["user_visible"] = False
    spec["test_plan"]["commands"] = ["python -m pytest -q"]
    return spec


def _ctx(tmp_path, root, examples, mapping, passed=(TEST_ID,), collected=(TEST_ID,)):
    repo = _repo(tmp_path, root)
    ctx = RunContext(cfg=make_config(root, tmp_path / "runs"), store=RunStore(tmp_path / "runs"), driver=None, queue=InMemoryQueue(),
                     notifier=LogNotifier(), forge=None, run_id=RUN,
                     policy=Policy(reviewer_passes=1, security_review=False, review_max_rounds=1, preview_wait_seconds=0))
    ctx.driver = Driver(ctx, examples, list(mapping))
    ctx.event("run.created", {"policy": {"preview_mode": "local"}, "budget_usd": 50, "outcome_id": "o", "title": "t"})
    for stage in ("decomposing", "gate_1_decomposition"):
        ctx.event("stage.entered", {"stage": stage})
    ctx.event("artifact.produced", {"artifact_type": "decomposition", "revision": 1, "work_items": examples["decomposition"]["work_items"]})
    ctx.event("stage.entered", {"stage": "specifying"})
    for st in ("spec_drafting", "spec_judging"):
        ctx.event("work_item.stage", {"stage": st, "spec_id": SID}, work_item_id=WID)
    ctx.event("stage.entered", {"stage": "gate_2_spec_readiness"})
    ctx.event("work_item.stage", {"stage": "spec_approved", "spec_id": SID}, work_item_id=WID, spec_id=SID, actor="human")
    ctx.event("stage.entered", {"stage": "implementing"})
    return ctx, Verifier(repo, list(passed), list(collected))


def test_one_round_to_ready_with_every_hand_off_and_the_bookkeeping(tmp_path, root, examples):
    ctx, verifier = _ctx(tmp_path, root, examples, mapping=[TEST_ID])
    spec, md = _spec(examples), "# Smart retries\n\nThe readable contract."
    verdict = run_implement_one(ctx, spec, "https://github.com/w/app", "main", verifier, {}, spec_md=md)
    calls = {c["agent_key"]: c for c in ctx.driver.calls}
    assert set(calls) == {"implementer", "reviewer", "merge_judge"} and len(ctx.driver.calls) == 3

    # issue 1: every consumer gets this spec's markdown beside a spec.json without private keys
    for c in calls.values():
        assert c["uploads"]["spec.md"] == md.encode() and "_markdown" not in json.loads(c["uploads"]["spec.json"])
    assert "(spec.md is the readable form)" in calls["implementer"]["description"]
    # issue 18: the implementer may push; reviewer and merge judge are read-only
    assert "work on branch" in calls["implementer"]["description"] and "never commit" not in calls["implementer"]["description"]
    assert all("never commit, never push" in calls[k]["description"] for k in ("reviewer", "merge_judge"))
    # issue 5: the passing set came from the verifier's pytest run and the mapping was checked against it
    det = {d["check"]: d for d in verdict["deterministic_checks"]}
    assert det[SETUP_CHECK]["passed"] and det["ac_test_mapping"]["passed"] and "saw pass" in det["ac_test_mapping"]["detail"]
    test_results = ctx.store.get_json(RUN, "implement", f"{SID}-r1-test_results.json")
    assert test_results["commands"][0]["tests"]["passed"] == [TEST_ID]
    # issue 16: the merge judge was told the real round, its verdict carries it, and provenance is the orchestrator's
    assert verdict["result"] == "ready" and verdict["round"] == 1 and "round 1." in calls["merge_judge"]["description"]
    assert verdict["produced_by"]["session_id"] == "sesn_merge_judge_1" and verdict["produced_by"]["agent_id"] == "agent_merge_judge"
    assert verdict["produced_by"]["list_cost_usd"] == 0.9 and verdict["produced_by"]["model"] == "claude-opus-5-5"
    stored = ctx.store.get_json(RUN, "merge_judge", f"{SID}-r1-merge_readiness.json")
    assert stored["produced_by"]["session_id"] == "sesn_merge_judge_1" and stored["round"] == 1
    report = ctx.store.get_json(RUN, "implement", f"{SID}-r1-implementation_report.json")
    assert report["produced_by"]["session_id"] == "sesn_implementer_1"
    [path] = (tmp_path / "runs" / RUN / "artifacts" / "review").glob(f"{SID}-r1-base*-head*-p1-findings.json")
    findings = ctx.store.get_json(RUN, "review", path.name)
    assert findings["produced_by"]["session_id"] == "sesn_reviewer_1" and findings["produced_by"]["agent_name"] == "Adversarial reviewer"
    # issue 16: every session.finished and artifact.produced names the spec and the work item
    events = ctx.store.events(RUN)
    finished = [e for e in events if e.kind == "session.finished"]
    assert [(e.stage, e.spec_id, e.work_item_id) for e in finished] == [("implement", SID, WID), ("review", SID, WID), ("merge_judge", SID, WID)]
    assert all(e.spec_id == SID and e.work_item_id == WID for e in events if e.kind == "artifact.produced" and e.stage in ("implement", "review", "merge_judge"))
    sessions = _sessions_for(ctx, SID, WID)
    assert [s["session_id"] for s in sessions] == ["sesn_implementer_1", "sesn_reviewer_1", "sesn_merge_judge_1"]
    assert all(s["duration_seconds"] is not None and s["duration"] != "n/a" and s["agent_key"] for s in sessions)
    st = ctx.store.state(RUN)                                                   # the log replays
    assert st.work_items[WID].stage == WorkItemStage.MERGE_JUDGING and st.list_cost_usd == 3.3 + 2.6 + 0.9


def test_a_fabricated_mapping_costs_a_round_and_the_judge_sees_round_two(tmp_path, root, examples):
    ctx, verifier = _ctx(tmp_path, root, examples, mapping=["tests/does_not_exist.py::nope"])
    verdict = run_implement_one(ctx, _spec(examples), "https://github.com/w/app", "main", verifier, {}, spec_md="# s")
    keys = [c["agent_key"] for c in ctx.driver.calls]
    assert keys == ["implementer", "reviewer", "implementer", "merge_judge"]
    second = ctx.driver.calls[2]
    det_findings = [f for f in json.loads(second["uploads"]["findings.json"])["findings"] if f["finding_id"].startswith("DET-")]
    assert [f["finding_id"] for f in det_findings] == ["DET-ac_test_mapping"] and "not found" in det_findings[0]["claim"]
    assert verdict["round"] == 2 and "round 2." in ctx.driver.calls[3]["description"]
    assert verdict["result"] == "another_round" and verdict["summary_for_human"].startswith("Overridden")   # the mapping is still fabricated
    assert (tmp_path / "runs" / RUN / "artifacts" / "merge_judge" / f"{SID}-r2-merge_readiness.json").is_file()
