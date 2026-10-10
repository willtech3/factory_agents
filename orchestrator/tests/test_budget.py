"""The run budget (`outcome.constraints.budget_usd`) is a cap, not a label: before every session the orchestrator
checks spent plus the next step's caps against it, pauses the run with a budget gate when the step would not fit,
and `factory resume` continues after the owner raises the budget. Fake driver, in-memory queue, no network."""
import json
import subprocess
from types import SimpleNamespace

import pytest
from conftest import make_config

from factory import checks, runner
from factory.budget import BudgetPaused, reserve_budget
from factory.gates import Decision, GateDecision, GateKind, InMemoryQueue, LogNotifier, parse_budget
from factory.stages import RunContext
from factory.stages.common import enter_item_stage
from factory.state import Policy, RunStage, WorkItemStage
from factory.store import RunStore


class AutoApproveQueue(InMemoryQueue):
    """Gates 1 and 2 are approved the moment they open (the owner is not what these tests are about). The budget
    gate stays open, as on a real surface: the orchestrator exits and a human decides it later."""

    def open(self, gate):
        super().open(gate)
        if gate.kind != GateKind.BUDGET:
            self.decisions[gate.gate_id] = GateDecision(gate_id=gate.gate_id, decision=Decision.APPROVE, decided_by="owner",
                                                        artifact_shas=tuple(a.sha256 for a in gate.artifacts))


class Driver:
    """Decomposer, spec writers, and judge answer with the Ledgerline artefacts; every session started is recorded,
    with a list cost per agent so the run's spend is predictable."""

    COST = {"decomposer": 6.0, "spec_writer": 1.0, "spec_judge": 1.0}

    def __init__(self, examples):
        self.examples = examples
        self.calls = []

    def run(self, **kw):
        self.calls.append(kw)
        key = kw["agent_key"]
        if key == "decomposer":
            outputs = {"decomposition.json": json.dumps(self.examples["decomposition"]).encode(), "decomposition.md": b"# d"}
        elif key == "spec_writer":
            wid = kw["work_item_id"]
            spec = dict(self.examples["spec_smart_retries"], spec_id=f"spec_{wid.removeprefix('wi-')}", work_item_id=wid)
            outputs = {"spec.json": json.dumps(spec).encode(), "spec.md": b"# s"}
        elif key == "spec_judge":
            outputs = {"verdict.json": json.dumps(self.examples["verdict_round2"]).encode()}
        else:
            raise AssertionError(f"unexpected session {key}")
        return SimpleNamespace(session_id=f"sesn_{len(self.calls)}", agent_id=key, agent_version=1, outcome_result="satisfied",
                               outcome_iterations=1, list_cost_usd=self.COST[key], stop_reason="end_turn", events_seen=1,
                               custom_tool_calls=[], outputs=outputs)

    def keys(self):
        return [c["agent_key"] for c in self.calls]


def _ctx(tmp_path, root, driver, queue, budget, run_id="run_budget00001", design_only=True):
    return RunContext(cfg=make_config(root, tmp_path), store=RunStore(tmp_path), driver=driver, queue=queue, notifier=LogNotifier(),
                      forge=None, policy=Policy(run_budget_usd=budget, design_only=design_only), run_id=run_id)


def _kinds(ctx):
    return [e.kind for e in ctx.store.events(ctx.run_id)]


def _payload(ctx, kind, n=-1):
    return [e.payload for e in ctx.store.events(ctx.run_id) if e.kind == kind][n]


def _validate_log(ctx, schemas_dir):
    for ev in ctx.store.events(ctx.run_id):
        assert checks.schema_valid(ev.to_dict(), schemas_dir / "run_event.schema.json", schemas_dir).passed, ev.kind


def test_parse_budget_reads_the_forms_a_human_writes():
    assert parse_budget("budget_usd: 80") == 80.0
    assert parse_budget("Budget: $80.50") == 80.5
    assert parse_budget("fine, raise the budget to 120 and go on") == 120.0
    assert parse_budget("$90") == 90.0
    assert parse_budget("looks good\n75") == 75.0
    assert parse_budget("fix the tests in 3 files") is None
    assert parse_budget("") is None and parse_budget(None) is None


def test_reserve_budget_counts_spent_plus_the_caps_about_to_be_committed(tmp_path, root):
    ctx = _ctx(tmp_path, root, None, InMemoryQueue(), budget=50.0)
    ctx.event("run.created", {"outcome_id": "o", "title": "t", "budget_usd": 50.0})
    ctx.event("stage.entered", {"stage": "decomposing"})
    ctx.event("session.finished", {"session_id": "s1", "list_cost_usd": 20.0}, actor="platform")
    reserve_budget(ctx, "merge_judge")                 # 20 + 8 fits
    reserve_budget(ctx, "review", 2)                   # 20 + 30 = 50: exactly the budget still fits
    ctx.event("session.finished", {"session_id": "s2", "list_cost_usd": 0.01}, actor="platform")
    with pytest.raises(BudgetPaused) as info:          # 20.01 + 30 does not
        reserve_budget(ctx, "review", 2)
    e = info.value
    assert (e.stage, e.sessions, e.cap_usd, e.spent_usd, e.budget_usd) == ("review", 2, 30.0, 20.01, 50.0)
    assert "factory resume run_budget00001" in str(e)
    st = ctx.store.state(ctx.run_id)
    assert st.stage == RunStage.PAUSED_BUDGET and e.gate_id in st.open_gates
    pause = _payload(ctx, "budget.reached")
    assert pause["next"] == {"stage": "review", "label": "reviewer", "sessions": 2, "cap_usd": 30.0}
    assert pause["needed_usd"] == 50.01 and pause["stage"] == "decomposing" and pause["gate_id"] == e.gate_id
    gate = ctx.queue.gates[e.gate_id]
    assert gate.kind == GateKind.BUDGET and gate.options == (Decision.APPROVE, Decision.REJECT)
    assert json.loads(ctx.store.get_artifact(ctx.run_id, "budget", "paused-1.json"))["needed_usd"] == 50.01
    assert gate.artifacts[0].sha256 == ctx.store.sha256(ctx.run_id, "budget", "paused-1.json")
    # approving a budget gate without the new budget is not a usable decision
    with pytest.raises(ValueError):
        ctx.queue.decide(GateDecision(gate_id=e.gate_id, decision=Decision.APPROVE, decided_by="x", artifact_shas=(gate.artifacts[0].sha256,)))


def test_reserve_budget_counts_a_review_round_with_its_security_pass_as_one_batch(tmp_path, root):
    """The review round starts its general passes and the security pass together, so the check covers all of them:
    two reviewer caps plus the security reviewer's, recorded as one step of three sessions."""
    ctx = _ctx(tmp_path, root, None, InMemoryQueue(), budget=60.0)
    ctx.event("run.created", {"outcome_id": "o", "title": "t", "budget_usd": 60.0})
    ctx.event("stage.entered", {"stage": "decomposing"})
    ctx.event("session.finished", {"session_id": "s1", "list_cost_usd": 25.0}, actor="platform")
    reserve_budget(ctx, "review", 2)                                        # 25 + 30 fits
    reserve_budget(ctx, "review", 2, extra={"security_review": 0})          # nothing extra to commit
    with pytest.raises(BudgetPaused) as info:                               # 25 + 30 + 10 does not
        reserve_budget(ctx, "review", 2, extra={"security_review": 1})
    e = info.value
    assert (e.stage, e.sessions, e.cap_usd, e.spent_usd) == ("review", 3, 40.0, 25.0)
    assert _payload(ctx, "budget.reached")["next"] == {"stage": "review", "label": "reviewer", "sessions": 3, "cap_usd": 40.0}
    # only the security pass left to run (the general passes were reused on resume): its own cap, under its own name
    ctx2 = _ctx(tmp_path, root, None, InMemoryQueue(), budget=30.0, run_id="run_budget00002")
    ctx2.event("run.created", {"outcome_id": "o", "title": "t", "budget_usd": 30.0})
    ctx2.event("stage.entered", {"stage": "decomposing"})
    ctx2.event("session.finished", {"session_id": "s1", "list_cost_usd": 25.0}, actor="platform")
    with pytest.raises(BudgetPaused) as info:                               # 25 + 10 does not fit in 30
        reserve_budget(ctx2, "security_review", 1)
    assert (info.value.stage, info.value.sessions, info.value.cap_usd) == ("security_review", 1, 10.0)
    assert _payload(ctx2, "budget.reached")["next"]["label"] == "security reviewer"


def test_run_pauses_before_a_session_it_cannot_afford_and_resumes_when_the_budget_is_raised(tmp_path, root, examples, schemas_dir):
    """Budget $10, decompose cap $15: the run parks before its first session. Raised to $30 from the CLI flag, the
    decomposer runs ($6) and Gate 1 is approved, but four spec writers ($40 of caps) do not fit: it parks again in
    the specifying stage, before any writer starts. The owner approves that gate with $100 and the run completes."""
    outcome = examples["outcome"]
    driver, queue = Driver(examples), AutoApproveQueue()
    ctx = _ctx(tmp_path, root, driver, queue, budget=10.0)

    assert runner.run(ctx, outcome) == RunStage.PAUSED_BUDGET
    assert driver.calls == []                                  # nothing started: the check comes before the session
    assert _kinds(ctx) == ["run.created", "stage.entered", "budget.reached", "gate.opened"]
    assert _payload(ctx, "run.created")["budget_usd"] == 10.0
    pause1 = _payload(ctx, "budget.reached")
    assert pause1["next"]["stage"] == "decompose" and pause1["needed_usd"] == 15.0 and pause1["stage"] == "decomposing"
    assert ctx.store.state(ctx.run_id).budget_usd == 10.0

    # nothing decides the pause yet: resume refuses without touching the run
    ctx2 = _ctx(tmp_path, root, driver, queue, budget=10.0)
    with pytest.raises(RuntimeError, match="approve gate .* reject it .* --budget"):
        runner.resume(ctx2)
    assert _kinds(ctx2)[-1] == "gate.opened"

    # --budget 30 is the decision: the gate closes, the budget is raised, decomposition re-enters its stage
    assert runner.resume(ctx2, budget_usd=30.0) == RunStage.PAUSED_BUDGET
    assert driver.keys() == ["decomposer"]
    kinds = _kinds(ctx2)
    i = kinds.index("budget.reached") + 2
    assert kinds[i:i + 3] == ["gate.decided", "budget.raised", "run.resumed"]
    decided = [e.payload for e in ctx2.store.events(ctx2.run_id) if e.kind == "gate.decided"]
    assert decided[0]["kind"] == "budget" and decided[0]["decision"] == "approve" and decided[0]["comments"] == "budget_usd: 30"
    assert decided[0]["gate_id"] == pause1["gate_id"]
    assert _payload(ctx2, "budget.raised") | {"gate_id": None} == {"budget_usd": 30.0, "previous_budget_usd": 10.0, "spent_usd": 0.0,
                                                                     "gate_id": None, "decided_by": "cli"}
    assert _payload(ctx2, "run.resumed", 0) == {"stage": "decomposing", "from": "paused_budget", "with_feedback": False}
    assert [e.payload["stage"] for e in ctx2.store.events(ctx2.run_id) if e.kind == "stage.entered"] == \
        ["decomposing", "gate_1_decomposition", "specifying"]      # decomposing is not re-entered on resume
    assert decided[1]["kind"] == "gate_1_decomposition" and decided[1]["decision"] == "approve"
    pause2 = _payload(ctx2, "budget.reached")
    assert pause2 == {**pause2, "pause": 2, "budget_usd": 30.0, "spent_usd": 6.0, "sessions_finished": 1, "stage": "specifying",
                      "next": {"stage": "specify", "label": "spec writer", "sessions": 4, "cap_usd": 40.0}, "needed_usd": 46.0}
    st = ctx2.store.state(ctx2.run_id)
    assert st.stage == RunStage.PAUSED_BUDGET and st.list_cost_usd == 6.0 and st.budget_usd == 30.0
    assert pause2["gate_id"] in st.open_gates and pause1["gate_id"] not in st.open_gates
    assert sorted(p.name for p in (tmp_path / ctx.run_id / "artifacts" / "budget").glob("paused-*.json")) == ["paused-1.json", "paused-2.json"]

    # the owner approves the second gate from the surface with the new cap; resume applies it and the run completes
    gate2 = queue.gates[pause2["gate_id"]]
    queue.decide(GateDecision(gate_id=gate2.gate_id, decision=Decision.APPROVE, decided_by="priya", comments="budget_usd: 100",
                              artifact_shas=tuple(a.sha256 for a in gate2.artifacts)))
    ctx3 = _ctx(tmp_path, root, driver, queue, budget=30.0)
    assert runner.resume(ctx3) == RunStage.COMPLETED
    assert driver.keys() == ["decomposer"] + ["spec_writer"] * 4 + ["spec_judge"]
    raised = [e.payload for e in ctx3.store.events(ctx3.run_id) if e.kind == "budget.raised"]
    assert raised[-1]["budget_usd"] == 100.0 and raised[-1]["previous_budget_usd"] == 30.0 and raised[-1]["decided_by"] == "priya"
    assert _payload(ctx3, "run.resumed") == {"stage": "specifying", "from": "paused_budget", "with_feedback": False}
    stages = [e.payload["stage"] for e in ctx3.store.events(ctx3.run_id) if e.kind == "stage.entered"]
    assert stages == ["decomposing", "gate_1_decomposition", "specifying", "gate_2_spec_readiness"]   # specifying not re-entered
    st = ctx3.store.state(ctx3.run_id)
    assert st.stage == RunStage.COMPLETED and st.budget_usd == 100.0 and st.list_cost_usd == 11.0 and st.sessions_finished == 6
    assert not st.open_gates and all(wi.stage == WorkItemStage.SPEC_APPROVED for wi in st.work_items.values())
    _validate_log(ctx3, schemas_dir)


def test_rejecting_the_budget_gate_closes_the_run_on_resume(tmp_path, root, examples):
    driver, queue = Driver(examples), AutoApproveQueue()
    ctx = _ctx(tmp_path, root, driver, queue, budget=5.0, run_id="run_budget00002")
    assert runner.run(ctx, examples["outcome"]) == RunStage.PAUSED_BUDGET
    gate = queue.gates[_payload(ctx, "budget.reached")["gate_id"]]
    queue.decide(GateDecision(gate_id=gate.gate_id, decision=Decision.REJECT, decided_by="priya", comments="not worth more",
                              artifact_shas=tuple(a.sha256 for a in gate.artifacts)))
    ctx2 = _ctx(tmp_path, root, driver, queue, budget=5.0, run_id="run_budget00002")
    assert runner.resume(ctx2) == RunStage.CLOSED
    assert driver.calls == []
    assert _kinds(ctx2)[-2:] == ["gate.decided", "stage.entered"]
    assert _payload(ctx2, "gate.decided")["decision"] == "reject" and ctx2.store.state(ctx2.run_id).stage == RunStage.CLOSED
    # a budget that is not above what was spent is refused, and so is a cap on a run that is not paused or failed
    ctx3 = _ctx(tmp_path, root, driver, queue, budget=5.0, run_id="run_budget00002")
    with pytest.raises(RuntimeError, match="paused at its budget, can be resumed"):
        runner.resume(ctx3, budget_usd=50.0)


def test_resume_after_a_pause_during_a_redecomposition_keeps_the_owners_feedback(tmp_path, root, examples):
    """Gate 1 request-changes, then the re-run pauses before the decomposer starts: on resume the decomposer gets
    the owner's comments and the decomposition they were about, exactly as it would have without the pause."""
    class Gate1Changes(AutoApproveQueue):
        asked = 0

        def open(self, gate):
            InMemoryQueue.open(self, gate)
            if gate.kind == GateKind.DECOMPOSITION_REVIEW and not self.asked:
                self.asked += 1
                self.decisions[gate.gate_id] = GateDecision(gate_id=gate.gate_id, decision=Decision.REQUEST_CHANGES, decided_by="owner",
                                                            comments="wi-smart-retries: split out the schema change",
                                                            artifact_shas=tuple(a.sha256 for a in gate.artifacts))
            elif gate.kind != GateKind.BUDGET:
                self.decisions[gate.gate_id] = GateDecision(gate_id=gate.gate_id, decision=Decision.APPROVE, decided_by="owner",
                                                            artifact_shas=tuple(a.sha256 for a in gate.artifacts))

    driver, queue = Driver(examples), Gate1Changes()
    ctx = _ctx(tmp_path, root, driver, queue, budget=20.0, run_id="run_budget00003")   # one decomposer fits ($15 cap), two do not
    assert runner.run(ctx, examples["outcome"]) == RunStage.PAUSED_BUDGET
    assert driver.keys() == ["decomposer"] and "prior/feedback.md" not in driver.calls[0]["uploads"]
    assert _payload(ctx, "budget.reached")["stage"] == "decomposing"
    ctx2 = _ctx(tmp_path, root, driver, queue, budget=20.0, run_id="run_budget00003")
    assert runner.resume(ctx2, budget_usd=200.0) == RunStage.COMPLETED
    assert _payload(ctx2, "run.resumed", 0)["with_feedback"] is True
    second = driver.calls[1]
    assert second["agent_key"] == "decomposer" and "re-run after human review" in second["description"]
    assert b"split out the schema change" in second["uploads"]["prior/feedback.md"]
    assert json.loads(second["uploads"]["prior/decomposition.json"])["revision"] == examples["decomposition"]["revision"]


def _git_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    (path / "README").write_text("x")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
           "HOME": str(path), "PATH": "/usr/bin:/bin:/usr/local/bin"}
    subprocess.run(["git", "add", "."], cwd=path, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True, env=env)
    return path


def test_implementation_pauses_before_the_implementer_and_resumes_from_its_artefacts(tmp_path, root, examples, monkeypatch):
    """The budget check sits in the implement loop too. A run paused there resumes through the implementation path
    (`resume_points`), not by re-running design stages; the work item that never started still leaves SPEC_APPROVED."""
    from factory.stages.implement import run_implement_one

    run_id = "run_budget00004"
    spec = dict(examples["spec_smart_retries"])
    sid, wid = spec["spec_id"], spec["work_item_id"]
    ctx = _ctx(tmp_path, root, None, InMemoryQueue(), budget=30.0, run_id=run_id, design_only=False)
    ctx.event("run.created", {"outcome_id": "o", "title": "t", "budget_usd": 30.0})
    ctx.store.put_artifact(run_id, "intake", "outcome.json", json.dumps(examples["outcome"]).encode())
    ctx.event("stage.entered", {"stage": "decomposing"})
    ctx.event("artifact.produced", {"artifact_type": "decomposition", "revision": 1, "work_items": examples["decomposition"]["work_items"]})
    for stage in ("gate_1_decomposition", "specifying", "gate_2_spec_readiness"):
        ctx.event("stage.entered", {"stage": stage})
    for s in ("spec_drafting", "spec_judging"):
        ctx.event("work_item.stage", {"stage": s, "spec_id": sid}, work_item_id=wid)
    ctx.event("work_item.stage", {"stage": "spec_approved", "spec_id": sid}, work_item_id=wid, spec_id=sid, actor="human")
    ctx.store.put_artifact(run_id, "specify", f"final-{sid}.json", json.dumps(spec).encode())
    ctx.event("stage.entered", {"stage": "implementing"})

    repo = _git_repo(tmp_path / "clone")
    verifier = SimpleNamespace(checkout=lambda branch: repo)
    with pytest.raises(BudgetPaused) as info:          # implement cap $40 > $30: refused before the session
        run_implement_one(ctx, spec, "https://github.com/w/app", "main", verifier, {})
    assert info.value.stage == "implement" and info.value.sessions == 1
    st = ctx.store.state(run_id)
    assert st.stage == RunStage.PAUSED_BUDGET and st.work_items[wid].stage == WorkItemStage.IMPLEMENTING
    assert _payload(ctx, "budget.reached")["stage"] == "implementing"

    seen = {}

    def fake_implement(c, outcome, specs, spec_md, spec_verdict, resume=None):
        seen.update(specs=sorted(specs), resume=resume, budget=c.policy.run_budget_usd)
        c.event("run.completed", {"specs_with_change_requests": sorted(specs)})
        return RunStage.COMPLETED

    monkeypatch.setattr(runner, "implement_and_integrate", fake_implement)
    ctx2 = _ctx(tmp_path, root, None, InMemoryQueue(), budget=30.0, run_id=run_id, design_only=False)
    assert runner.resume(ctx2, budget_usd=120.0) == RunStage.COMPLETED
    assert seen["specs"] == [sid] and seen["budget"] == 120.0
    assert seen["resume"][sid].round_no == 1 and seen["resume"][sid].report is None
    assert _payload(ctx2, "run.resumed") == {"stage": "implementing", "from": "paused_budget", "points": {sid: {"round": 1, "implementer_done": False}}}
    assert ctx2.store.state(run_id).budget_usd == 120.0


def test_resumed_rounds_do_not_repeat_item_stage_events(tmp_path, root, examples):
    """`enter_item_stage` skips a stage the item is already in: a resumed loop re-walks steps whose outputs it
    reuses, and REVIEWING -> REVIEWING would be an illegal transition on replay."""
    ctx = _ctx(tmp_path, root, None, InMemoryQueue(), budget=100.0, run_id="run_budget00005")
    ctx.event("run.created", {"outcome_id": "o", "title": "t"})
    ctx.event("artifact.produced", {"artifact_type": "decomposition", "revision": 1, "work_items": examples["decomposition"]["work_items"]})
    wid = "wi-smart-retries"
    for s in ("spec_drafting", "spec_judging", "spec_approved", "implementing"):
        ctx.event("work_item.stage", {"stage": s}, work_item_id=wid)
    enter_item_stage(ctx, wid, "spec_x", WorkItemStage.REVIEWING, review_round=1)
    enter_item_stage(ctx, wid, "spec_x", WorkItemStage.REVIEWING, review_round=1)      # no-op the second time
    enter_item_stage(ctx, wid, "spec_x", WorkItemStage.RESPONDING)
    stages = [e.payload["stage"] for e in ctx.store.events(ctx.run_id) if e.kind == "work_item.stage"]
    assert stages[-2:] == ["reviewing", "responding"] and stages.count("reviewing") == 1
    assert ctx.store.state(ctx.run_id).work_items[wid].stage == WorkItemStage.RESPONDING


def test_state_tracks_the_budget_through_pause_and_raise():
    from factory.state import RunEvent, RunState, reduce

    st = RunState(run_id="r")
    seq = [0]

    def ev(kind, payload):
        e = RunEvent(run_id="r", seq=seq[0], kind=kind, actor={"type": "system"}, payload=payload)
        seq[0] += 1
        reduce(st, e)

    ev("run.created", {"outcome_id": "o", "budget_usd": 60})
    ev("stage.entered", {"stage": "decomposing"})
    ev("session.finished", {"session_id": "s", "list_cost_usd": 6.4})
    assert st.budget_usd == 60.0 and st.sessions_finished == 1 and st.list_cost_usd == 6.4
    ev("budget.reached", {"budget_usd": 60, "spent_usd": 6.4, "stage": "decomposing"})
    assert st.stage == RunStage.PAUSED_BUDGET
    ev("budget.raised", {"budget_usd": 90, "previous_budget_usd": 60})
    assert st.budget_usd == 90.0 and st.stage == RunStage.PAUSED_BUDGET
    ev("run.resumed", {"stage": "decomposing", "from": "paused_budget"})
    assert st.stage == RunStage.DECOMPOSING
    legacy = RunState(run_id="old")
    reduce(legacy, RunEvent(run_id="old", seq=0, kind="run.created", actor={"type": "system"}, payload={"outcome_id": "o"}))
    assert legacy.budget_usd is None                   # logs from before the cap carry no budget
