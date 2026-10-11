"""Run boundaries: an onboarded app never starts with local verification by accident, a Gate 2 selection must carry its
prerequisites, and the owner's approval notes reach the implementer without changing the approved spec.

Offline only: Gate 2 runs are parked with the real event log, store and file queue (test_gate_recovery's fixture), the
Console is driven through its endpoints with the child process spawn captured, and no session, forge or app runs."""
import json

import pytest
import test_gate_recovery as tgr
from conftest import make_config
from fastapi import HTTPException
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from factory import cli, runner
from factory.approvals import MAX_GUIDANCE_CHARS, approval_guidance, presented_specs, subset_problems
from factory.gates import InMemoryQueue, LogNotifier
from factory.onboarding import AppOnboarding
from factory.stages import RunContext
from factory.stages import implement as implement_stage
from factory.state import Policy, RunStage
from factory.store import RunStore
from factory.surfaces.filequeue import FileQueue
from factory.web.app import create_app

RUN = tgr.RUN


def dependent_specs():
    a, b, c = tgr.spec("spec_a", "wi-a"), tgr.spec("spec_b", "wi-b"), tgr.spec("spec_c", "wi-a")
    b["dependencies"] = {"specs": ["spec_a"]}                      # b needs a, presented at the same gate
    c["dependencies"] = {"specs": ["spec_b", "spec_elsewhere"]}    # c needs b; spec_elsewhere is outside this run
    return {"spec_a": a, "spec_b": b, "spec_c": c}


@pytest.fixture
def parked(tmp_path, root, monkeypatch):
    """A run stopped idle at Gate 2 over three specs with a dependency chain a <- b <- c, and an implementation stand-in
    that records what it was handed (nothing executes)."""
    monkeypatch.setattr(tgr, "SPECS", dependent_specs())
    gate, shas = tgr.park(tmp_path, root, design_only=False)
    seen = {}

    def implement(ctx, outcome, specs, spec_md, spec_verdict, resume=None, guidance=None):
        seen.update(specs=sorted(specs), guidance=guidance, resume=resume)
        return RunStage.IMPLEMENTING
    monkeypatch.setattr(runner, "implement_and_integrate", implement)
    return {"gate": gate, "shas": shas, "seen": seen}


def approve(tmp_path, root, gate, decision, comments="", subset=None, *, direct=False):
    """Record the decision the way a surface does (`record_decision`), or straight into decisions.json (a surface that
    skipped validation), then resume the parked run on this code."""
    queue = FileQueue(tmp_path / "runs" / "_queue")
    if direct:
        rec = queue._load("gates.json")[gate.gate_id]
        (tmp_path / "runs" / "_queue" / "decisions.json").write_text(json.dumps({gate.gate_id: {
            "decision": decision, "decided_by": "file", "comments": comments, "approved_subset": subset or [],
            "artifact_shas": [a["sha256"] for a in rec["artifacts"]]}}))
    else:
        queue.record_decision(gate.gate_id, decision, "will", comments, subset)
    ctx = tgr.context(tmp_path, root, design_only=False)
    return ctx, runner.resume(ctx)


def approved_events(ctx):
    return [e.spec_id for e in ctx.store.events(RUN) if e.kind == "work_item.stage" and e.payload["stage"] == "spec_approved"]


# ---------------------------------------------------------------------------------------------- the selection rule

def test_a_selection_must_carry_its_presented_prerequisites():
    specs = dependent_specs()
    assert subset_problems(specs, ["spec_a"]) == []
    assert subset_problems(specs, ["spec_a", "spec_b"]) == []
    assert subset_problems(specs, ["spec_a", "spec_b", "spec_c"]) == [], "a dependency outside the gate's specs is not required"
    assert subset_problems(specs, ["spec_b"]) == ["spec_b depends on spec_a, presented at this gate but not selected"]
    assert subset_problems(specs, ["spec_a", "spec_c"]) == ["spec_c depends on spec_b, presented at this gate but not selected"]
    assert subset_problems(specs, ["spec_x"])[0] == "not presented at this gate: spec_x"
    assert subset_problems(specs, []) == ["select at least one spec"]


def test_the_console_refuses_a_selection_without_its_prerequisite_and_records_nothing(parked, tmp_path, root):
    app = create_app(make_config(root, tmp_path / "runs"), demo_thread=False)
    client, gate = TestClient(app), parked["gate"]
    r = client.post(f"/api/gates/{gate.gate_id}/decision", json={"decision": "approve_subset", "approved_subset": ["spec_b"]})
    assert r.status_code == 409 and "spec_b depends on spec_a" in r.json()["detail"]
    assert FileQueue(tmp_path / "runs" / "_queue").decision(gate.gate_id) is None
    r = client.post(f"/api/gates/{gate.gate_id}/decision", json={"decision": "approve_subset", "approved_subset": ["spec_a", "spec_b"]})
    assert r.status_code == 200 and sorted(r.json()["recorded"]["artifact_shas"]) == sorted(parked["shas"].values())


def test_factory_decide_refuses_the_same_selection(parked, tmp_path, root, monkeypatch):
    monkeypatch.setattr(cli.cfgmod, "load", lambda: make_config(root, tmp_path / "runs"))
    gate = parked["gate"]
    out = CliRunner().invoke(cli.app, ["decide", gate.gate_id, "approve_subset", "--spec", "spec_c", "--spec", "spec_a"])
    assert out.exit_code == 1 and "spec_c depends on spec_b" in out.output
    assert FileQueue(tmp_path / "runs" / "_queue").decision(gate.gate_id) is None
    out = CliRunner().invoke(cli.app, ["decide", gate.gate_id, "approve_subset", "--spec", "spec_a"])
    assert out.exit_code == 0, out.output


def test_the_runner_never_implements_an_invalid_selection_that_reached_the_queue(parked, tmp_path, root):
    with pytest.raises(RuntimeError, match="not dependency-valid.*spec_b depends on spec_a.*nothing was implemented"):
        approve(tmp_path, root, parked["gate"], "approve_subset", subset=["spec_b"], direct=True)
    assert parked["seen"] == {}
    assert approved_events(tgr.context(tmp_path, root)) == []


def test_a_valid_selection_implements_only_the_selection(parked, tmp_path, root):
    ctx, _ = approve(tmp_path, root, parked["gate"], "approve_subset", subset=["spec_a", "spec_b"])
    assert parked["seen"]["specs"] == ["spec_a", "spec_b"] and approved_events(ctx) == ["spec_a", "spec_b"]


def test_presented_specs_are_read_back_by_the_hash_the_gate_showed(parked, tmp_path):
    rec = FileQueue(tmp_path / "runs" / "_queue")._load("gates.json")[parked["gate"].gate_id]
    assert sorted(presented_specs(tmp_path / "runs", RUN, rec["artifacts"])) == ["spec_a", "spec_b", "spec_c"]
    final = tmp_path / "runs" / RUN / "artifacts" / "specify" / "final-spec_b.json"
    final.write_text(final.read_text().replace('"spec_b"', '"spec_b" ', 1))
    with pytest.raises(ValueError, match="spec_b this gate presented"):
        presented_specs(tmp_path / "runs", RUN, rec["artifacts"])


# ---------------------------------------------------------------------------------------------- approval notes

def test_approval_notes_are_routed_per_spec_and_bounded():
    specs = dependent_specs()
    notes = approval_guidance("spec_a: keep the catalog order\nwi-b: say why the picker hides retired models\nship it small", specs)
    assert "keep the catalog order" in notes["spec_a"] and "ship it small" in notes["spec_a"] and "picker" not in notes["spec_a"]
    assert "retired models" in notes["spec_b"] and "keep the catalog order" not in notes["spec_b"]
    assert notes["spec_c"].count("- ") == 1, "spec_c (work item wi-a) hears only the general line, not spec_a's"
    assert approval_guidance("", specs) == {} and approval_guidance("  \n ", specs) == {}
    long = approval_guidance("x" * (MAX_GUIDANCE_CHARS * 2), {"spec_a": specs["spec_a"]})["spec_a"]
    assert len(long) < MAX_GUIDANCE_CHARS + 600 and "notes cut at" in long
    assert all(n.startswith("# The owner's notes at Gate 2 approval") and "never change" in n for n in notes.values())


def test_approval_notes_reach_implementation_bound_to_the_approved_specs(parked, tmp_path, root):
    ctx, _ = approve(tmp_path, root, parked["gate"], "approve", "spec_b: log the model id\nkeep tests in bun")
    guidance = parked["seen"]["guidance"]
    assert "log the model id" in guidance["spec_b"] and "log the model id" not in guidance["spec_a"]
    record = json.loads(ctx.store.get_artifact(RUN, "gate_2", f"approval-{parked['gate'].gate_id}.json"))
    assert record["approved"] == {sid: parked["shas"][f"{sid}.json"] for sid in ("spec_a", "spec_b", "spec_c")}
    assert record["guidance"] == guidance and record["decided_by"] == "will"
    final = json.loads(ctx.store.get_artifact(RUN, "specify", "final-spec_b.json"))
    assert "log the model id" not in json.dumps(final), "the notes never edit the approved spec"


def test_a_resumed_implementation_keeps_the_notes_and_refuses_a_changed_spec(parked, tmp_path, root):
    ctx, _ = approve(tmp_path, root, parked["gate"], "approve_subset", "spec_a: pin the schema version", ["spec_a"])
    ctx.event("stage.entered", {"stage": "implementing"}, stage="implement")
    ctx.event("run.failed", {"reason": "crash", "interrupted_sessions": []})
    parked["seen"].clear()
    runner.resume(tgr.context(tmp_path, root, design_only=False))
    assert "pin the schema version" in parked["seen"]["guidance"]["spec_a"] and parked["seen"]["specs"] == ["spec_a"]
    ctx.event("run.failed", {"reason": "crash again", "interrupted_sessions": []})
    final = tmp_path / "runs" / RUN / "artifacts" / "specify" / "final-spec_a.json"
    final.write_text(final.read_text() + " ")
    with pytest.raises(RuntimeError, match="no longer matches the spec Gate 2 .* approved"):
        runner.resume(tgr.context(tmp_path, root, design_only=False))


def test_runs_approved_before_approval_records_resume_without_guidance(tmp_path, root):
    ctx = tgr.context(tmp_path, root)
    ctx.event("run.created", {"outcome_id": "o", "title": "t"})
    assert runner.approved_record(ctx, ctx.store.events(RUN), {"spec_a": {}}) == {}


def test_the_implementer_receives_the_notes_as_an_upload_and_a_clause(tmp_path, root, monkeypatch):
    from test_implement_loop import _remote

    origin = _remote(tmp_path)

    class Captured(Exception):
        pass
    calls = []

    def first_session(ctx, **kw):
        calls.append(kw)
        raise Captured
    monkeypatch.setattr(implement_stage, "run_session", first_session)
    runs = tmp_path / "runs"
    ctx = RunContext(cfg=make_config(root, runs), store=RunStore(runs), driver=None, queue=InMemoryQueue(), notifier=LogNotifier(),
                     forge=None, policy=Policy(), run_id=RUN)
    ctx.event("run.created", {"outcome_id": "o", "title": "t"})
    ctx.event("artifact.produced", {"artifact_type": "decomposition", "revision": 1, "work_items": [{"work_item_id": "wi-a", "depends_on": []}]})
    for st in ("spec_drafting", "spec_judging", "spec_approved"):
        ctx.event("work_item.stage", {"stage": st}, work_item_id="wi-a")
    spec = {**tgr.spec("spec_a", "wi-a"), "acceptance_criteria": []}
    with pytest.raises(Captured):
        implement_stage.run_implement_one(ctx, spec, str(origin), "main", implement_stage.LocalVerifier(str(origin), runs / RUN / "verify"),
                                          {}, guidance="# The owner's notes\n- pin the schema version\n")
    assert calls[0]["uploads"]["gate2_guidance.md"].decode().endswith("- pin the schema version\n")
    assert "gate2_guidance.md" in calls[0]["description"] and "never change its acceptance criteria" in calls[0]["description"]
    calls.clear()
    with pytest.raises(Captured):
        implement_stage.run_implement_one(ctx, spec, str(origin), "main", implement_stage.LocalVerifier(str(origin), runs / RUN / "verify2"), {})
    assert "gate2_guidance.md" not in calls[0]["uploads"] and "gate2_guidance.md" not in calls[0]["description"]


# ---------------------------------------------------------------------------------------------- starting a run on an onboarded app

BINDING = {"FACTORY_PREVIEW": "managed", "FACTORY_APP_ID": "app_evergreen", "FACTORY_TEST_PROFILE_SHA256": "b" * 64,
           "FACTORY_TEST_VAULT_IDS": "vlt_model", "FACTORY_MANAGED_VERIFY_BUDGET_USD": "4"}
REPO = "https://github.com/willtech3/evergreen"


@pytest.fixture
def console(tmp_path, root, monkeypatch):
    """The Console with a saved outcome for REPO; the orchestrator child process is captured, never started."""
    cfg = make_config(root, tmp_path / "runs", forge_token="t", agents={"x": object()})
    monkeypatch.setattr("factory.web.app.cfgmod.missing", lambda cfg, design_only=False: [])
    spawned = []

    class Proc:
        pid = 4242
    monkeypatch.setattr("factory.web.app.subprocess.Popen", lambda args, **kw: spawned.append({"args": args, "env": kw["env"]}) or Proc())
    app = create_app(cfg, demo_thread=False)
    console = app.state.console
    console.outcomes_dir.mkdir(parents=True, exist_ok=True)
    (console.outcomes_dir / "out_evergreen.json").write_text(json.dumps({
        **tgr.OUTCOME, "outcome_id": "out_evergreen", "target_repositories": [{"url": REPO, "default_branch": "main"}]}))
    (console.outcomes_dir / "out_otherrepo.json").write_text(json.dumps({
        **tgr.OUTCOME, "outcome_id": "out_otherrepo", "target_repositories": [{"url": "https://github.com/willtech3/other", "default_branch": "main"}]}))
    return {"client": TestClient(app), "spawned": spawned, "cfg": cfg}


def app_record(app_id, status="ready", url=REPO):
    return {"app_id": app_id, "name": app_id, "kind": "existing", "repository_url": url, "status": status}


def start(console, outcome="out_evergreen", mode="live", app_id=None, preview="local"):
    return console["client"].post("/api/runs", json={"outcome_id": outcome, "mode": mode, "app_id": app_id, "settings": {"preview": preview}})


def test_a_generic_start_on_an_onboarded_repository_binds_its_hosted_setup(console, monkeypatch):
    monkeypatch.setattr(AppOnboarding, "list", lambda self: [app_record("app_evergreen")])
    asked = []
    monkeypatch.setattr(AppOnboarding, "run_settings", lambda self, app_id, target: asked.append((app_id, target["url"])) or dict(BINDING))
    r = start(console)                                             # no app_id, and the screen's default local preview
    assert r.status_code == 200 and r.json()["app_id"] == "app_evergreen" and r.json()["preview"] == "managed"
    env = console["spawned"][0]["env"]
    assert env["FACTORY_PREVIEW"] == "managed" and env["FACTORY_APP_ID"] == "app_evergreen" and env["FACTORY_TEST_VAULT_IDS"] == "vlt_model"
    assert asked == [("app_evergreen", REPO)], "the server re-checks the app's verified setup against the run's repository"


def test_an_unready_onboarded_app_refuses_a_live_start_instead_of_running_locally(console, tmp_path):
    folder = tmp_path / "runs" / "_apps" / "app_0123456789ab"
    folder.mkdir(parents=True)
    (folder / "app.json").write_text(json.dumps({**app_record("app_0123456789ab", status="needs_input"), "budget_usd": 10,
                                                 "spent_usd": 0, "credentials": [], "updated_at": "2026-10-10T00:00:00Z"}))
    r = start(console)
    assert r.status_code == 409 and "Verify this app's current setup" in r.json()["detail"]
    assert console["spawned"] == []


def test_a_repository_onboarded_twice_is_ambiguous(console, monkeypatch):
    monkeypatch.setattr(AppOnboarding, "list", lambda self: [app_record("app_one"), app_record("app_two")])
    r = start(console)
    assert r.status_code == 409 and "several apps" in r.json()["detail"] and console["spawned"] == []


def test_non_onboarded_and_design_runs_start_as_before(console, monkeypatch):
    monkeypatch.setattr(AppOnboarding, "list", lambda self: [app_record("app_evergreen")])
    monkeypatch.setattr(AppOnboarding, "run_settings", lambda self, app_id, target: (_ for _ in ()).throw(AssertionError("bound")))
    r = start(console, outcome="out_otherrepo")
    assert r.status_code == 200 and r.json()["app_id"] is None and console["spawned"][-1]["env"]["FACTORY_PREVIEW"] == "local"
    r = start(console, mode="design")
    assert r.status_code == 200 and "--design-only" in console["spawned"][-1]["args"] and "FACTORY_APP_ID" not in r.json()["settings"]


def test_a_new_repository_the_run_creates_is_never_bound():
    onboarding = AppOnboarding.__new__(AppOnboarding)
    onboarding.list = lambda: [app_record("app_evergreen")]
    assert onboarding.binding_for({"url": REPO, "create": True}) is None
    assert onboarding.binding_for({"url": "https://github.com/willtech3/other"}) is None


def test_the_cli_binds_the_same_way_and_refuses_an_explicit_local_preview(tmp_path, root, monkeypatch):
    (tmp_path / "runs" / "_apps" / "app_0123456789ab").mkdir(parents=True)
    (tmp_path / "runs" / "_apps" / "app_0123456789ab" / "app.json").write_text("{}")
    monkeypatch.setattr(AppOnboarding, "binding_for", lambda self, target, app_id=None: dict(BINDING) if target["url"] == REPO else None)
    cfg = make_config(root, tmp_path / "runs", preview_mode="local")
    outcome = {"target_repositories": [{"url": REPO}]}
    bound = cli._bind_onboarded_app(cfg, outcome, explicit_preview=None)
    assert (bound.preview_mode, bound.app_id, bound.test_profile_sha256, bound.test_vault_ids, bound.managed_verify_budget_usd) == \
           ("managed", "app_evergreen", "b" * 64, ("vlt_model",), 4.0)
    with pytest.raises(cli.typer.Exit):
        cli._bind_onboarded_app(cfg, outcome, explicit_preview="local")
    assert cli._bind_onboarded_app(cfg, {"target_repositories": [{"url": "https://github.com/x/y"}]}, None) is cfg
    monkeypatch.setattr(AppOnboarding, "binding_for", lambda self, target, app_id=None: (_ for _ in ()).throw(HTTPException(409, "several apps")))
    with pytest.raises(cli.typer.Exit):
        cli._bind_onboarded_app(cfg, outcome, explicit_preview=None)


def test_notes_for_unselected_contracts_do_not_become_general_guidance():
    selected = {"spec_a": dependent_specs()["spec_a"]}
    notes = approval_guidance("spec_b: change the unrelated picker\nwi-b: remove the old menu\nkeep it small", selected)
    assert "picker" not in notes["spec_a"] and "menu" not in notes["spec_a"]
    assert "keep it small" in notes["spec_a"]


def test_recording_approval_refuses_bytes_changed_since_the_gate(parked, tmp_path, root):
    from factory.gates import Decision, GateDecision
    ctx = tgr.context(tmp_path, root, design_only=False)
    gate = parked["gate"]
    d = GateDecision(gate_id=gate.gate_id, decision=Decision.APPROVE, decided_by="owner",
                     artifact_shas=tuple(parked["shas"].values()))
    path = tmp_path / "runs" / RUN / "artifacts" / "specify" / "final-spec_a.json"
    path.write_text(path.read_text() + " ")
    with pytest.raises(RuntimeError, match="approved artifacts"):
        runner.record_approval(ctx, d, dependent_specs())
