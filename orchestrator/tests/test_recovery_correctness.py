"""Recovery and accounting regressions: a stopped spec's dependants never hide its build-recovery gate, merged findings
carry a schema-valid review id, a failed onboarding job charges an unaccounted session once, and the Console resumes a
managed run that stopped before its first hosted verification on the setup it started with.

Salvaged from willtech3/factory_agents#61 with its review findings addressed (fixed-point pruning, no replay naming
of a blocked dependant, no recipe swap on resume)."""
import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_config
from fastapi import HTTPException

from factory import build_recovery, checks, runner
from factory.gates import InMemoryQueue, LogNotifier
from factory.onboarding import AppOnboarding
from factory.stages import RunContext
from factory.stages.implement import merge_findings, review_id
from factory.state import Policy, RunStage, WorkItemStage
from factory.store import RunStore

SHA = "b" * 64


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    (path / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
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
    ctx.event("stage.entered", {"stage": RunStage.GATE_2_SPEC_READINESS.value})
    for sid, s in specs.items():
        ctx.event("work_item.stage", {"stage": WorkItemStage.SPEC_APPROVED.value, "spec_id": sid},
                  work_item_id=s["work_item_id"], spec_id=sid, actor="human")
    ctx.event("artifact.produced", {"artifact_type": "gate2_approval", "path": "gate_2/approval-g2.json",
                                    "gate_id": "g2", "approved": sorted(specs), "guidance_for": []})


@pytest.fixture
def chain(tmp_path, root, examples, monkeypatch):
    """spec_a <- spec_b <- spec_c (each depends on the one before), plus an independent spec_d. spec_a escalates."""
    outcome = copy.deepcopy(examples["outcome"])
    repo = _git_repo(tmp_path / "repo")
    outcome["target_repositories"] = [{"url": str(repo), "default_branch": "main", "paths_in_scope": []}]
    base = copy.deepcopy(examples["spec_smart_retries"])

    def spec(sid, deps):
        return {**copy.deepcopy(base), "spec_id": sid, "work_item_id": "wi-" + sid[5:], "title": sid, "dependencies": {"specs": deps}}
    specs = {"spec_a": spec("spec_a", []), "spec_b": spec("spec_b", ["spec_a"]), "spec_c": spec("spec_c", ["spec_b"]),
             "spec_d": spec("spec_d", [])}
    runs = tmp_path / "runs"
    ctx = RunContext(cfg=make_config(root, runs), store=RunStore(runs), driver=None, queue=InMemoryQueue(),
                     notifier=LogNotifier(), forge=None, policy=Policy(), run_id="run_chain")
    _seed_approved_specs(ctx, outcome, specs)
    implemented = []

    def fake_implement_one(c, spec, repo_url, base_branch, verifier, deps, **kw):
        sid, wid = spec["spec_id"], spec["work_item_id"]
        implemented.append(sid)
        for stage in (WorkItemStage.IMPLEMENTING, WorkItemStage.REVIEWING, WorkItemStage.MERGE_JUDGING):
            c.event("work_item.stage", {"stage": stage.value}, work_item_id=wid, spec_id=sid)
        c.store.put_derived_artifact(c.run_id, "review", f"{sid}-r1-findings.merged.json", b'{"findings": []}')
        c.store.put_derived_artifact(c.run_id, "implement", f"{sid}-r1-test_results.json", b'{"commands": [], "e2e": []}')
        if sid == "spec_a":
            return {"result": "escalate", "summary_for_human": "spec_a needs the owner"}
        return {"result": "ready", "summary_for_human": "ready", "_head_sha": "abc", "_branch": f"factory/{c.run_id}/{sid}"}

    def fake_integrate(c, spec, verdict, *a, **kw):
        sid, wid = spec["spec_id"], spec["work_item_id"]
        c.event("work_item.stage", {"stage": WorkItemStage.INTEGRATING.value}, work_item_id=wid, spec_id=sid)
        c.event("cr.opened", {"id": 42, "url": "https://github.com/x/y/pull/42"}, work_item_id=wid, spec_id=sid)
        c.event("work_item.stage", {"stage": WorkItemStage.CR_OPEN.value, "cr_id": 42}, work_item_id=wid, spec_id=sid)
        return {"id": 42, "url": "https://github.com/x/y/pull/42"}

    monkeypatch.setattr(runner, "run_implement_one", fake_implement_one)
    monkeypatch.setattr(runner, "run_integrate", fake_integrate)
    return SimpleNamespace(ctx=ctx, outcome=outcome, specs=specs, implemented=implemented)


def _escalations(ctx):
    return [(e.spec_id, e.payload["reason"]) for e in ctx.store.events(ctx.run_id) if e.kind == "run.escalated"]


def test_a_stopped_spec_blocks_its_whole_dependency_chain_and_keeps_its_recovery_gate(chain):
    ctx = chain.ctx
    assert runner.implement_and_integrate(ctx, chain.outcome, chain.specs, {}, None) == RunStage.ESCALATED
    st = ctx.store.state(ctx.run_id)
    assert sorted(chain.implemented) == ["spec_a", "spec_d"]          # b and c never start
    assert st.work_items["wi-d"].stage == WorkItemStage.CR_OPEN
    assert st.work_items["wi-b"].stage == st.work_items["wi-c"].stage == WorkItemStage.SPEC_APPROVED
    # no spec_id=None "unsatisfiable dependencies" escalation for the transitive dependant
    assert _escalations(ctx) == [("spec_a", "spec_a: merge judge said escalate")]
    assert runner.resumable_build_escalation(st, ctx.store.events(ctx.run_id))
    assert any(e.kind == "gate.opened" and e.payload.get("kind") == "build_recovery" for e in ctx.store.events(ctx.run_id))


def test_a_replay_names_the_spec_that_stopped_not_a_dependant_blocked_behind_it(chain):
    ctx = chain.ctx
    runner.implement_and_integrate(ctx, chain.outcome, chain.specs, {}, None)
    # the run is escalated; drive the stage again as a resume would after the stage moved on
    ctx.event("run.resumed", {"stage": RunStage.IMPLEMENTING.value, "from": RunStage.ESCALATED.value})
    assert ctx.store.state(ctx.run_id).stage == RunStage.IMPLEMENTING
    assert runner.implement_and_integrate(ctx, chain.outcome, chain.specs, {}, None) == RunStage.ESCALATED
    assert [sid for sid, _ in _escalations(ctx)] == ["spec_a", "spec_a"]
    assert build_recovery.snapshot(ctx.store, ctx.run_id)["spec_id"] == "spec_a"


def test_a_real_dependency_cycle_is_still_unsatisfiable(chain):
    ctx, specs = chain.ctx, chain.specs
    cyc = {"spec_b": {**specs["spec_b"], "dependencies": {"specs": ["spec_c"]}},
           "spec_c": {**specs["spec_c"], "dependencies": {"specs": ["spec_b"]}}}
    assert runner.implement_and_integrate(ctx, chain.outcome, cyc, {}, None) == RunStage.ESCALATED
    assert _escalations(ctx) == [(None, "unsatisfiable dependencies among ['spec_b', 'spec_c']")]


def test_merged_findings_review_id_is_schema_valid_for_hyphenated_spec_ids(schemas_dir, examples):
    merged = merge_findings([copy.deepcopy(examples["findings_round1"])], "spec_smart-retries", 1, "base1234", "head5678",
                            "factory/run/spec_smart-retries")
    assert merged["review_id"] == "rev_smartretries01head56"
    res = checks.schema_valid(merged, schemas_dir / "review_findings.schema.json", schemas_dir)
    assert res.passed, res.detail
    assert review_id("spec_a", 1, "") == "rev_a0100000"                 # short ids still meet the {8,} length


def test_a_failed_onboarding_job_charges_an_unaccounted_session_once(tmp_path, root):
    runs = tmp_path / "runs"
    ob = AppOnboarding(make_config(root, runs), client=SimpleNamespace())
    app_id = "app_0123456789ab"
    (runs / "_apps" / app_id).mkdir(parents=True)
    ob.write({"app_id": app_id, "name": "Test", "kind": "greenfield", "repository_url": "https://github.com/x/app",
              "description": "d", "status": "discovering", "phase": "discover", "budget_usd": 10.0, "spent_usd": 0.0,
              "reserved_usd": 3.0, "owner_pid": None, "answers": {},
              "sessions": [{"session_id": "sesn_1", "stage": "onboarding-discovery", "cost_usd": None}],
              "credentials": [], "evidence": [], "created_at": "2026-10-14T00:00:00Z", "updated_at": "2026-10-14T00:00:00Z"})

    def fail(aid, cap):
        raise ValueError("simulated failure")
    ob._discover = fail
    ob._job(app_id, "discover", 3.0)
    after = ob.get(app_id)
    assert after["spent_usd"] == 3.0 and after["reserved_usd"] == 0
    assert after["sessions"][0]["accounted"] is True and after["sessions"][0]["charged_usd"] == 3.0
    # a second operation that fails before starting any session charges nothing more
    with ob.locked(app_id) as r:
        r["reserved_usd"] = 3.0
        ob.write(r)
    ob._job(app_id, "discover", 3.0)
    assert ob.get(app_id)["spent_usd"] == 3.0


# ---------------------------------------------------------------- Console resume of a managed run


def _managed_run(tmp_path, root, *, app_id="app_0123456789ab", sha=SHA, design_only=False):
    runs = tmp_path / "runs"
    cfg = make_config(root, runs, forge_token="ghp_testtoken", forge_repo="", preview_mode="managed",
                      app_id=app_id, test_profile_sha256=sha)
    ctx = RunContext(cfg=cfg, store=RunStore(runs), driver=None, queue=InMemoryQueue(), notifier=LogNotifier(),
                     forge=None, policy=Policy(), run_id="run_managedx1")
    outcome = {"outcome_id": "out_1", "title": "T", "statement": "S", "owner": {"name": "O", "handle": "o"},
               "target_repositories": [{"url": "https://github.com/acme/sample", "default_branch": "main", "paths_in_scope": []}]}
    ctx.event("run.created", {"outcome_id": "out_1", "title": "T", "policy": runner.policy_snapshot(ctx), "forge": "github",
                              "gate_surface": "web", "design_only": design_only, "budget_usd": 150.0})
    ctx.store.put_artifact(ctx.run_id, "intake", "outcome.json", json.dumps(outcome).encode())
    ctx.event("stage.entered", {"stage": RunStage.DECOMPOSING.value})
    ctx.event("run.failed", {"reason": "network blip before implementation"})
    from factory.web.app import Console
    console = Console(make_config(root, runs, forge_token="ghp_testtoken", forge_repo=""))
    return console, ctx


def _settings(sha):
    def run_settings(self, app_id, target):
        assert target["url"] == "https://github.com/acme/sample"
        return {"FACTORY_PREVIEW": "managed", "FACTORY_APP_ID": app_id, "FACTORY_TEST_PROFILE_SHA256": sha,
                "FACTORY_TEST_VAULT_IDS": "vlt_1", "FACTORY_MANAGED_VERIFY_BUDGET_USD": "4"}
    return run_settings


def test_console_resume_before_hosted_verification_restores_the_recorded_app_and_its_vaults(tmp_path, root, monkeypatch):
    console, ctx = _managed_run(tmp_path, root)
    monkeypatch.setattr(AppOnboarding, "run_settings", _settings(SHA))
    env = console.resume_environment(ctx.run_id, console.run_detail(ctx.run_id))
    assert env["FACTORY_PREVIEW"] == "managed"
    assert env["FACTORY_APP_ID"] == "app_0123456789ab"
    assert env["FACTORY_TEST_PROFILE_SHA256"] == SHA
    assert env["FACTORY_TEST_VAULT_IDS"] == "vlt_1"


def test_console_resume_refuses_a_recipe_that_changed_since_the_run_started(tmp_path, root, monkeypatch):
    console, ctx = _managed_run(tmp_path, root)
    monkeypatch.setattr(AppOnboarding, "run_settings", _settings("c" * 64))
    with pytest.raises(HTTPException) as exc:
        console.resume_environment(ctx.run_id, console.run_detail(ctx.run_id))
    assert exc.value.status_code == 409


def test_console_resume_without_a_recorded_app_stays_unbound(tmp_path, root, monkeypatch):
    console, ctx = _managed_run(tmp_path, root, app_id=None, sha=None)
    monkeypatch.setattr(AppOnboarding, "run_settings", lambda *a: pytest.fail("no recorded app to bind"))
    env = console.resume_environment(ctx.run_id, console.run_detail(ctx.run_id))
    assert env["FACTORY_PREVIEW"] == "managed" and "FACTORY_APP_ID" not in env


def test_console_resume_of_a_design_only_run_never_needs_the_app_binding(tmp_path, root, monkeypatch):
    console, ctx = _managed_run(tmp_path, root, design_only=True)
    monkeypatch.setattr(AppOnboarding, "run_settings", lambda *a: pytest.fail("a design-only run verifies nothing"))
    env = console.resume_environment(ctx.run_id, console.run_detail(ctx.run_id))
    assert "FACTORY_APP_ID" not in env
