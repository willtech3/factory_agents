"""Regression tests for the 7 highest-leverage orchestrator fixes and surviving refutation pass findings."""
import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_config

from factory import checks, runner
from factory.budget import BudgetPaused
from factory.checks import CheckResult
from factory.gates import Decision, GateKind, InMemoryQueue, LogNotifier
from factory.onboarding import AppOnboarding
from factory.stages import RunContext
from factory.stages.implement import SETUP_CHECK, deterministic_findings, merge_findings, run_implement_one
from factory.stages.specify import run_specify
from factory.state import Policy, RunStage, WorkItemStage
from factory.store import RunStore
from factory.watcher import local_e2e_runner


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    (path / "README.md").write_text("# test\n")
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
        "HOME": str(path), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    }
    subprocess.run(["git", "add", "."], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True, env=env)
    return path


def _seed_approved_specs(ctx: RunContext, outcome: dict, specs: dict[str, dict]) -> None:
    ctx.event("run.created", {"outcome_id": outcome["outcome_id"], "title": outcome["title"], "budget_usd": 150.0})
    ctx.store.put_artifact(ctx.run_id, "intake", "outcome.json", json.dumps(outcome).encode())
    ctx.event("stage.entered", {"stage": RunStage.DECOMPOSING.value})
    work_items = [{"work_item_id": s["work_item_id"], "title": s["title"],
                   "depends_on": [specs[d]["work_item_id"] for d in s["dependencies"]["specs"] if d in specs]}
                  for s in specs.values()]
    ctx.event("artifact.produced", {"artifact_type": "decomposition", "revision": 1, "work_items": work_items})
    ctx.event("stage.entered", {"stage": RunStage.GATE_1_DECOMPOSITION.value})
    ctx.event("stage.entered", {"stage": RunStage.SPECIFYING.value})
    for sid, s in specs.items():
        wid = s["work_item_id"]
        ctx.event("work_item.stage", {"stage": WorkItemStage.SPEC_DRAFTING.value, "spec_id": sid}, work_item_id=wid)
        ctx.event("work_item.stage", {"stage": WorkItemStage.SPEC_JUDGING.value, "spec_id": sid}, work_item_id=wid)
        sha = ctx.store.put_artifact(ctx.run_id, "specify", f"final-{sid}.json", json.dumps(s).encode())
    ctx.event("stage.entered", {"stage": RunStage.GATE_2_SPEC_READINESS.value})
    for sid, s in specs.items():
        ctx.event("work_item.stage", {"stage": WorkItemStage.SPEC_APPROVED.value, "spec_id": sid},
                  work_item_id=s["work_item_id"], spec_id=sid, actor="human")
    ctx.event("artifact.produced", {"artifact_type": "gate2_approval", "path": "gate_2/approval-g2.json",
                                    "gate_id": "g2", "approved": sorted(specs), "guidance_for": []})


def test_rank1_implement_and_integrate_escalation_blocks_dependants_and_keeps_build_recovery_intact(tmp_path, root, examples, monkeypatch):
    """Rank #1: when Spec A escalates in Stage 3, implement_and_integrate must move Spec A to ESCALATED and its
    dependant Spec B to BLOCKED without emitting a false 'unsatisfiable dependencies' escalation (which had spec_id=None
    and broke build_recovery.snapshot / resumable_build_escalation), while still completing independent Spec C."""
    outcome = copy.deepcopy(examples["outcome"])
    repo = _git_repo(tmp_path / "repo")
    outcome["target_repositories"] = [{"url": str(repo), "default_branch": "main", "paths_in_scope": []}]

    base_spec = copy.deepcopy(examples["spec_smart_retries"])
    spec_a = {**copy.deepcopy(base_spec), "spec_id": "spec_a", "work_item_id": "wi-a", "title": "A", "dependencies": {"specs": []}}
    spec_b = {**copy.deepcopy(base_spec), "spec_id": "spec_b", "work_item_id": "wi-b", "title": "B", "dependencies": {"specs": ["spec_a"]}}
    spec_c = {**copy.deepcopy(base_spec), "spec_id": "spec_c", "work_item_id": "wi-c", "title": "C", "dependencies": {"specs": []}}
    specs = {"spec_a": spec_a, "spec_b": spec_b, "spec_c": spec_c}

    runs = tmp_path / "runs"
    ctx = RunContext(cfg=make_config(root, runs), store=RunStore(runs), driver=None, queue=InMemoryQueue(),
                     notifier=LogNotifier(), forge=None, policy=Policy(), run_id="run_rank1_esc")
    _seed_approved_specs(ctx, outcome, specs)

    def fake_implement_one(c, spec, repo_url, base_branch, verifier, deps, **kw):
        sid, wid = spec["spec_id"], spec["work_item_id"]
        c.event("work_item.stage", {"stage": WorkItemStage.IMPLEMENTING.value}, work_item_id=wid, spec_id=sid)
        c.event("work_item.stage", {"stage": WorkItemStage.REVIEWING.value}, work_item_id=wid, spec_id=sid)
        c.event("work_item.stage", {"stage": WorkItemStage.MERGE_JUDGING.value}, work_item_id=wid, spec_id=sid)
        c.store.put_derived_artifact(c.run_id, "review", f"{sid}-r1-findings.merged.json", b'{"findings": []}')
        c.store.put_derived_artifact(c.run_id, "implement", f"{sid}-r1-test_results.json", b'{"commands": [], "e2e": []}')
        if sid == "spec_a":
            return {"result": "escalate", "summary_for_human": "Spec A needs owner clarification"}
        return {"result": "ready", "summary_for_human": "Spec C ready", "_head_sha": "abc", "_branch": f"factory/{c.run_id}/{sid}"}

    def fake_integrate(c, spec, verdict, *a, **kw):
        sid, wid = spec["spec_id"], spec["work_item_id"]
        c.event("work_item.stage", {"stage": WorkItemStage.INTEGRATING.value}, work_item_id=wid, spec_id=sid)
        c.event("cr.opened", {"id": 42, "url": "https://github.com/x/y/pull/42"}, work_item_id=wid, spec_id=sid)
        c.event("work_item.stage", {"stage": WorkItemStage.CR_OPEN.value, "cr_id": 42}, work_item_id=wid, spec_id=sid)
        return {"id": 42, "url": "https://github.com/x/y/pull/42"}

    monkeypatch.setattr(runner, "run_implement_one", fake_implement_one)
    monkeypatch.setattr(runner, "run_integrate", fake_integrate)

    final_stage = runner.implement_and_integrate(ctx, outcome, specs, {}, None)
    assert final_stage == RunStage.ESCALATED
    st = ctx.store.state(ctx.run_id)
    assert st.stage == RunStage.ESCALATED
    assert st.work_items["wi-a"].stage == WorkItemStage.ESCALATED
    assert st.work_items["wi-b"].stage == WorkItemStage.BLOCKED
    assert st.work_items["wi-c"].stage == WorkItemStage.CR_OPEN
    escalations = [(e.spec_id, e.payload["reason"]) for e in ctx.store.events(ctx.run_id) if e.kind == "run.escalated"]
    assert escalations == [("spec_a", "spec_a: merge judge said escalate")]
    # Because the false 'unsatisfiable dependencies' escalation (spec_id=None) was not emitted, build recovery is intact
    assert runner.resumable_build_escalation(st, ctx.store.events(ctx.run_id)) is True


def test_rank2_and_s1_merge_judge_another_round_loops_back_to_implementer_and_handles_final_round(tmp_path, root, examples):
    """Rank #2 + Surviving Fix S1: when the Merge Judge returns another_round while rounds remain, run_implement_one
    synthesizes standing findings from the verdict, transitions MERGE_JUDGING -> IMPLEMENTING, and runs the next round;
    even if Round 2 also returns another_round and triggers the final response-only Round 3 (IMPLEMENTING -> MERGE_JUDGING)."""
    repo = _git_repo(tmp_path / "repo")

    class Verifier:
        def checkout(self, branch):
            return repo

        def prepare_environment(self, r, manifest=None):
            self.setup = CheckResult(SETUP_CHECK, True, "ok", {"env_dir": "v", "installer": "uv", "python": "3.14", "install": "ok"})
            return self.setup

        def analyse(self, r, base_sha, head_sha):
            f = "app/services/billing/stripe_webhook_handler.rb"
            return {"changed": [f], "deleted": [], "added_lines": {f: ["x = 1"]}, "existing_tests": [], "patch": "diff"}

        def run_commands(self, r, commands, timeout=0):
            return [{"command": c, "exit_code": 0, "stdout_tail": "ok", "stderr_tail": "",
                     "tests": {"framework": "pytest", "report": "junit", "status": "reported",
                               "passed": ["tests/test_retries.py::test_retry_schedule"], "failed": [], "skipped": []}} for c in commands]

        def collect_test_ids(self, r, timeout=0):
            return ["tests/test_retries.py::test_retry_schedule"]

        def run_entries(self, urls, entries, repo=None, timeout=0):
            return []

    spec = copy.deepcopy(examples["spec_smart_retries"])
    spec["test_plan"]["e2e"] = []
    for ac in spec["acceptance_criteria"]:
        ac["user_visible"] = False
    spec["test_plan"]["commands"] = ["python -m pytest -q"]
    shutil_src = root / "examples" / "ledgerline" / "service_manifest.json"
    (repo / "service_manifest.json").write_bytes(shutil_src.read_bytes())

    class Driver:
        def __init__(self, judge_results):
            self.judge_results = list(judge_results)
            self.calls = []

        def run(self, **kw):
            self.calls.append(kw)
            key = kw["agent_key"]
            sid = f"sesn_{len(self.calls)}"
            if key == "implementer":
                report = {
                    "spec_id": spec["spec_id"],
                    "criteria": [{"ac_id": ac["ac_id"], "tests": ["tests/test_retries.py::test_retry_schedule"]}
                                 for ac in spec["acceptance_criteria"] if ac.get("verification") == "automated_test"],
                    "commands_run": [{"command": "python -m pytest -q", "exit_code": 0}],
                    "files_changed": ["app/services/billing/stripe_webhook_handler.rb"],
                }
                out = {"implementation_report.json": json.dumps(report).encode()}
                if "findings.json" in kw["uploads"]:
                    fs = json.loads(kw["uploads"]["findings.json"])["findings"]
                    out["review_response.json"] = json.dumps({
                        "response_id": "rsp_0123456789",
                        "review_id": "rev_0123456789",
                        "spec_id": spec["spec_id"],
                        "round": 1,
                        "new_head_sha": "abc1234",
                        "tests_after": {"commands": ["python -m pytest -q"], "all_passed": True},
                        "responses": [{"finding_id": f["finding_id"], "disposition": "fixed", "explanation": "addressed judge finding"} for f in fs],
                    }).encode()
                return SimpleNamespace(session_id=sid, agent_id="imp", agent_version=1, outcome_result="satisfied",
                                       outcome_iterations=1, list_cost_usd=1.0, stop_reason="end_turn", events_seen=1,
                                       custom_tool_calls=[], outputs=out)
            if key == "reviewer":
                f = copy.deepcopy(examples["findings_round1"])
                f["findings"] = []
                return SimpleNamespace(session_id=sid, agent_id="rev", agent_version=1, outcome_result=None,
                                       outcome_iterations=0, list_cost_usd=1.0, stop_reason="end_turn", events_seen=1,
                                       custom_tool_calls=[], outputs={"findings.json": json.dumps(f).encode()})
            res_kind = self.judge_results.pop(0)
            v = copy.deepcopy(examples["merge_readiness"])
            v["result"] = res_kind
            v["criteria"] = [{"ac_id": ac["ac_id"], "status": "met", "evidence": "tests/test_retries.py::test_retry_schedule"}
                             for ac in spec["acceptance_criteria"]]
            if res_kind == "another_round":
                v["criteria"][0]["status"] = "not_met"
                v["criteria"][0]["evidence"] = "missing edge case assertion"
            return SimpleNamespace(session_id=sid, agent_id="mj", agent_version=1, outcome_result=None,
                                   outcome_iterations=0, list_cost_usd=0.5, stop_reason="end_turn", events_seen=1,
                                   custom_tool_calls=[], outputs={"merge_readiness.json": json.dumps(v).encode()})

    runs = tmp_path / "runs"
    driver = Driver(["another_round", "another_round", "ready"])
    ctx = RunContext(cfg=make_config(root, runs), store=RunStore(runs), driver=driver, queue=InMemoryQueue(),
                     notifier=LogNotifier(), forge=None,
                     policy=Policy(reviewer_passes=1, security_review=False, review_max_rounds=2, preview_wait_seconds=0),
                     run_id="run_rank2_mj")
    _seed_approved_specs(ctx, examples["outcome"], {spec["spec_id"]: spec})
    ctx.event("stage.entered", {"stage": RunStage.IMPLEMENTING.value})

    verdict = run_implement_one(ctx, spec, "https://github.com/w/app", "main", Verifier(), {}, spec_md="# spec")
    assert verdict["result"] == "ready" and verdict["round"] == 3
    assert [c["agent_key"] for c in driver.calls] == [
        "implementer", "reviewer", "merge_judge",
        "implementer", "reviewer", "merge_judge",
        "implementer", "merge_judge",
    ]
    # Round 2 implementer saw the synthesized F-1 finding from Round 1's merge judge
    r2_imp_findings = json.loads(driver.calls[3]["uploads"]["findings.json"])["findings"]
    assert any("AC-1" in f["claim"] and f["severity"] == "blocking" for f in r2_imp_findings)
    # Full event log replays cleanly with no IllegalTransition (verifies Surviving Fix S1: IMPLEMENTING -> MERGE_JUDGING on round 3)
    st = ctx.store.state(ctx.run_id)
    assert st.work_items[spec["work_item_id"]].stage == WorkItemStage.MERGE_JUDGING


def test_rank3_deterministic_and_merged_findings_validate_against_schema(schemas_dir, examples):
    """Rank #3: deterministic_findings emits violates as a list[str], and merge_findings produces a schema-valid
    review_id (no hyphens) and guards against empty passes."""
    det = deterministic_findings([CheckResult("ac_test_mapping", False, "AC-1: test not found")])
    assert isinstance(det[0]["violates"], list) and len(det[0]["violates"]) == 1

    f1 = copy.deepcopy(examples["findings_round1"])
    merged = merge_findings([f1], "spec_smart-retries", 1, "base1234", "head5678", "factory/run/spec_smart-retries")
    merged["findings"].extend(det)
    res = checks.schema_valid(merged, schemas_dir / "review_findings.schema.json", schemas_dir)
    assert res.passed, res.detail

    empty_merged = merge_findings([], "spec_a", 1, "base", "head", "br", previous=f1)
    assert empty_merged["produced_by"]["agent_id"] == "orchestrator"


def test_rank4_and_s2_resume_specify_after_budget_pause_reuses_persisted_drafts_and_routes_issues(tmp_path, root, examples):
    """Rank #4 + Surviving Fix S2: (a) a budget pause after Round 1 writers finish (before Round 1 judge runs) reuses
    all 4 persisted Round 1 writer specs on resume without re-running the writers or emitting backward SPEC_DRAFTING
    events; (b) resuming after a revise verdict with round_offset=1 only rewrites the work items with issues."""
    outcome, decomposition = examples["outcome"], examples["decomposition"]

    class SpecDriver:
        def __init__(self, revise_first=False):
            self.revise_first = revise_first
            self.calls = []

        def run(self, **kw):
            self.calls.append(kw)
            round_no = int(kw["title"].rsplit(" r", 1)[1])
            if kw["agent_key"] == "spec_writer":
                wid = kw["work_item_id"]
                spec = {**copy.deepcopy(examples["spec_smart_retries"]),
                        "spec_id": f"spec_{wid.removeprefix('wi-')}", "work_item_id": wid}
                return SimpleNamespace(session_id=f"s_{len(self.calls)}", agent_id="w", agent_version=1,
                                       outcome_result="satisfied", outcome_iterations=1, list_cost_usd=1.0,
                                       stop_reason="end_turn", events_seen=1, custom_tool_calls=[],
                                       outputs={"spec.json": json.dumps(spec).encode(), "spec.md": f"# {wid} r{round_no}".encode()})
            verdict = copy.deepcopy(examples["verdict_round2"])
            verdict["round"] = round_no
            if self.revise_first and round_no == 1:
                verdict["overall"].update(result="revise", blocking_issue_count=1)
                verdict["coherence"] = {k: [] for k in verdict["coherence"]}
                for ps in verdict["per_spec"]:
                    ps["issues"] = [{"issue_id": "I-1", "severity": "blocking", "location": ps["spec_id"],
                                     "description": "fix retry backoff", "suggested_resolution": "specify jitter"}] if ps["spec_id"] == "spec_smart-retries" else []
            return SimpleNamespace(session_id=f"j_{len(self.calls)}", agent_id="j", agent_version=1,
                                   outcome_result=None, outcome_iterations=0, list_cost_usd=1.0,
                                   stop_reason="end_turn", events_seen=1, custom_tool_calls=[],
                                   outputs={"verdict.json": json.dumps(verdict).encode()})

    caps = {"decompose": 4.0, "specify": 3.0, "judge": 9.0, "implement": 15.0, "review": 6.0, "security_review": 6.0, "merge_judge": 3.0, "integrate": 2.0}
    runs = tmp_path / "runs"
    driver = SpecDriver(revise_first=True)
    ctx = RunContext(cfg=make_config(root, runs), store=RunStore(runs), driver=driver, queue=InMemoryQueue(),
                     notifier=LogNotifier(), forge=None,
                     policy=Policy(run_budget_usd=12.0, stage_budget_usd=dict(caps), spec_judge_max_rounds=2),
                     run_id="run_rank4_spec")
    ctx.event("run.created", {"outcome_id": outcome["outcome_id"], "title": outcome["title"], "budget_usd": 12.0})
    ctx.store.put_artifact(ctx.run_id, "intake", "outcome.json", json.dumps(outcome).encode())
    ctx.event("stage.entered", {"stage": RunStage.DECOMPOSING.value})
    ctx.store.put_artifact(ctx.run_id, "decompose", "r1-decomposition.json", json.dumps(decomposition).encode())
    ctx.event("artifact.produced", {"artifact_type": "decomposition", "revision": 1, "work_items": decomposition["work_items"]})
    ctx.event("stage.entered", {"stage": RunStage.GATE_1_DECOMPOSITION.value})
    ctx.event("gate.decided", {"gate_id": "g1", "kind": GateKind.DECOMPOSITION_REVIEW.value, "decision": Decision.APPROVE.value, "comments": ""})

    # 4 writers ($12 cap <= $12) run and spend $4; Round 1 judge ($9 cap + $4 = $13 > $12) pauses!
    with pytest.raises(BudgetPaused):
        run_specify(ctx, outcome, decomposition)
    assert [c["agent_key"] for c in driver.calls] == ["spec_writer"] * 4

    # Resume with budget=14.0: Round 1 writers are reused (0 new writer calls for Round 1!), Round 1 judge runs ($1)
    # and returns "revise" for spec_smart-retries only, then Round 2 runs ONLY the 1 writer with issues ($3 cap + $5 spent = $8 <= $14),
    # and pauses before Round 2 judge ($9 cap + $6 spent = $15 > $14).
    ctx.policy = Policy(run_budget_usd=14.0, stage_budget_usd=dict(caps), spec_judge_max_rounds=2)
    with pytest.raises(BudgetPaused):
        run_specify(ctx, outcome, decomposition)
    assert [c["agent_key"] for c in driver.calls] == ["spec_writer"] * 4 + ["spec_judge", "spec_writer"]
    r2_writer = driver.calls[-1]
    assert r2_writer["work_item_id"] == "wi-smart-retries"
    assert "prior/spec.json" in r2_writer["uploads"] and "judge_issues.json" in r2_writer["uploads"]


def test_rank5_and_s3_web_resume_environment_restores_managed_binding_before_first_hosted_verification(tmp_path, root):
    """Rank #5 + Surviving Fix S3: when a managed run pauses or fails before verification.hosted runs, Console.resume_environment
    restores FACTORY_APP_ID and FACTORY_TEST_PROFILE_SHA256 from policy_snapshot / onboarded app binding."""
    from factory.web.app import Console

    runs = tmp_path / "runs"
    sha = "b" * 64
    cfg = make_config(root, runs, forge="github", forge_repo="", forge_token="ghp_testtoken",
                      preview_mode="managed", app_id="app_0123456789ab", test_profile_sha256=sha)
    ctx = RunContext(cfg=cfg, store=RunStore(runs), driver=None, queue=InMemoryQueue(),
                     notifier=LogNotifier(), forge=None, policy=Policy(), run_id="run_rank5mng")
    outcome = {
        "outcome_id": "out_1", "title": "Test", "statement": "S",
        "owner": {"name": "Will", "handle": "will"},
        "target_repositories": [{"url": "https://github.com/willtech3/sample", "default_branch": "main", "paths_in_scope": []}],
    }
    ctx.event("run.created", {"outcome_id": "out_1", "title": "Test", "policy": runner.policy_snapshot(ctx),
                              "forge": "github", "gate_surface": "web", "design_only": False, "budget_usd": 150.0})
    ctx.store.put_artifact(ctx.run_id, "intake", "outcome.json", json.dumps(outcome).encode())
    ctx.event("stage.entered", {"stage": RunStage.DECOMPOSING.value})
    ctx.event("run.failed", {"reason": "network blip before implementation"})

    console = Console(make_config(root, runs, forge="github", forge_repo="", forge_token="ghp_testtoken"))
    detail = console.run_detail(ctx.run_id)
    env = console.resume_environment(ctx.run_id, detail)
    assert env["FORGE_TOKEN"] == "ghp_testtoken"
    assert env["FORGE_REPO"] == "willtech3/sample"
    assert env["FACTORY_PREVIEW"] == "managed"
    assert env["FACTORY_APP_ID"] == "app_0123456789ab"
    assert env["FACTORY_TEST_PROFILE_SHA256"] == sha


def test_rank6_onboarding_job_marks_failed_sessions_accounted_and_does_not_double_charge(tmp_path, root):
    """Rank #6: when an onboarding job fails with an unaccounted session, _job charges reserved_usd once and marks the
    session accounted=True so a subsequent failure before session start does not charge reserved_usd a second time."""
    runs = tmp_path / "runs"
    cfg = make_config(root, runs)
    ob = AppOnboarding(cfg, client=SimpleNamespace())
    app_id = "app_0123456789ab"
    app_dir = runs / "_apps" / app_id
    app_dir.mkdir(parents=True)
    record = {
        "app_id": app_id, "name": "Test", "kind": "greenfield",
        "repository_url": "https://github.com/willtech3/app", "description": "desc",
        "status": "discovering", "phase": "discover", "budget_usd": 10.0, "spent_usd": 0.0,
        "reserved_usd": 3.0, "owner_pid": None, "answers": {},
        "sessions": [{"session_id": "sesn_1", "stage": "onboarding-discovery", "cost_usd": None}],
        "credentials": [], "evidence": [], "created_at": "2026-10-14T00:00:00Z", "updated_at": "2026-10-14T00:00:00Z",
    }
    ob.write(record)

    def fail_discover(aid, cap):
        raise ValueError("simulated failure after session start")

    ob._discover = fail_discover
    ob._job(app_id, "discover", 3.0)
    after_first = ob.get(app_id)
    assert after_first["spent_usd"] == 3.0 and after_first["reserved_usd"] == 0
    assert after_first["sessions"][0]["accounted"] is True

    # A second operation that fails BEFORE starting any new session must not charge reserved_usd again
    with ob.locked(app_id) as r:
        r["reserved_usd"] = 3.0
        ob.write(r)
    ob._job(app_id, "discover", 3.0)
    after_second = ob.get(app_id)
    assert after_second["spent_usd"] == 3.0


def test_rank7_stage5_watcher_prepares_virtualenv_before_running_e2e(tmp_path, root, examples, monkeypatch):
    """Rank #7: local_e2e_runner prepares the per-run virtualenv on first checkout before calling LocalVerifier.run_e2e."""
    runs = tmp_path / "runs"
    ctx = RunContext(cfg=make_config(root, runs), store=RunStore(runs), driver=None, queue=InMemoryQueue(),
                     notifier=LogNotifier(), forge=SimpleNamespace(authenticated_url=lambda u: u),
                     policy=Policy(), run_id="run_rank7_e2e")
    calls = []

    class FakeVerifier:
        def __init__(self, url, workdir):
            self.setup = None

        def checkout(self, branch):
            calls.append(("checkout", branch))
            return tmp_path

        def prepare_environment(self, repo, manifest=None):
            calls.append(("prepare_environment", repo))
            self.setup = CheckResult(SETUP_CHECK, True, "ok", {})
            return self.setup

        def run_e2e(self, url, entries, repo=None):
            calls.append(("run_e2e", url, self.setup is not None and self.setup.passed))
            return [{"command": "true", "exit_code": 0}]

    import factory.stages.implement as imp_mod
    monkeypatch.setattr(imp_mod, "LocalVerifier", FakeVerifier)
    runner_fn = local_e2e_runner(ctx, examples["outcome"], "main")
    res = runner_fn("http://127.0.0.1:9000", [{"command": "true", "target": "preview"}])
    assert res[0]["exit_code"] == 0
    assert [c[0] for c in calls] == ["checkout", "prepare_environment", "run_e2e"]
    assert calls[2][2] is True
