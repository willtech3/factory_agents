"""ADR-0013 phase 2: `factory resume` replays the run's pipeline from its recorded steps. A controller that dies anywhere
leaves a run that the next one finishes exactly as an uninterrupted run would: no gate opened twice, no finished
session paid for again, the same final state. Fake agents at the session boundary, in-memory gate surface, no network."""
import json
import threading

import pytest
from support.events import assert_log_valid
from test_budget import CAPS, AutoApproveQueue, Driver

from conftest import make_config
from factory import runner
from factory.gates import Decision, GateDecision, GateKind, InMemoryQueue, LogNotifier
from factory.stages import RunContext
from factory.state import Policy, RunStage, WorkItemStage
from factory.store import RunStore

RUN = "run_replay00001"


class Platform(Driver):
    """The agents and the platform they run on, shared by every controller of the run: a session it created can be
    followed by id from a later controller, and `die_in_session` kills the controller while that session works."""

    def __init__(self, examples):
        super().__init__(examples)
        self.sessions: dict[str, object] = {}
        self.attached: list[str] = []
        self.die_in_session: int | None = None         # the n-th session created: its controller dies after creating it
        self.lock = threading.Lock()                    # the spec writers start their sessions from worker threads

    def run(self, **kw):
        if kw.get("attach_session_id"):
            self.attached.append(kw["attach_session_id"])
            return self.sessions[kw["attach_session_id"]]
        with self.lock:
            res = super().run(**{k: v for k, v in kw.items() if k != "on_created"})
            self.sessions[res.session_id] = res
            dies = self.die_in_session == len(self.calls)
            if dies:
                self.die_in_session = None
        kw["on_created"](res.session_id)
        if dies:
            raise SystemExit(f"controller died while {res.session_id} worked")
        return res


def context(tmp_path, root, platform, queue, design_only=True):
    return RunContext(cfg=make_config(root, tmp_path), store=RunStore(tmp_path), driver=platform, queue=queue, notifier=LogNotifier(),
                      forge=None, policy=Policy(run_budget_usd=500.0, design_only=design_only, stage_budget_usd=dict(CAPS)),
                      run_id=RUN)


def dying_at(ctx, n: int, written: bool):
    """The controller process exits at the n-th event it appends, before or after the event is written."""
    seen = {"n": 0}
    lock = threading.Lock()               # the spec writers append from worker threads
    append = ctx.event

    def event(*args, **kwargs):
        with lock:
            seen["n"] += 1
            mine = seen["n"]
            if mine == n and not written:
                raise SystemExit(f"controller died before event {n}")
            ev = append(*args, **kwargs)
        if mine == n and written:
            raise SystemExit(f"controller died after event {n}")
        return ev
    ctx.event = event
    return ctx


def outcome_of(ctx):
    st = ctx.store.state(RUN)
    events = ctx.store.events(RUN)
    return {"stage": st.stage, "items": {w: wi.stage for w, wi in st.work_items.items()},
            "sessions_finished": st.sessions_finished, "spent": round(st.list_cost_usd, 2),
            "gates_opened": sorted(e.payload["kind"] for e in events if e.kind == "gate.opened"),
            "gates_decided": sorted(e.payload["kind"] for e in events if e.kind == "gate.decided"),
            "final_specs": sorted(p.name for p in (ctx.cfg.runs_dir / RUN / "artifacts" / "specify").glob("final-*.json"))}


@pytest.fixture
def reference(tmp_path_factory, root, examples):
    """An uninterrupted design run: what every interrupted one must end as."""
    platform = Platform(examples)
    ctx = context(tmp_path_factory.mktemp("reference"), root, platform, AutoApproveQueue())
    assert runner.run(ctx, examples["outcome"]) == RunStage.COMPLETED
    return {"outcome": outcome_of(ctx), "sessions": platform.keys(), "events": len(ctx.store.events(RUN))}


def test_a_reference_run_records_one_step_per_unit_of_the_pipeline(tmp_path, root, examples, reference):
    platform = Platform(examples)
    ctx = context(tmp_path, root, platform, AutoApproveQueue())
    runner.run(ctx, examples["outcome"])
    keys = {s.key.split("/")[0] if s.key.startswith("session/") else s.key for s in ctx.store.steps(RUN)}
    assert {"decompose#1", "gate_1#1", "specify#1", "gate_2#1", "approve#1", "session"} <= keys
    assert all(s.status == "done" for s in ctx.store.steps(RUN))
    assert reference["outcome"]["stage"] == RunStage.COMPLETED
    assert set(reference["outcome"]["items"].values()) == {WorkItemStage.SPEC_APPROVED}


def test_resuming_a_finished_design_pipeline_is_refused_and_replaying_it_does_nothing(tmp_path, root, examples):
    platform = Platform(examples)
    ctx = context(tmp_path, root, platform, AutoApproveQueue())
    runner.run(ctx, examples["outcome"])
    with pytest.raises(RuntimeError, match="can be resumed"):
        runner.resume(context(tmp_path, root, platform, ctx.queue))
    n, calls = len(ctx.store.events(RUN)), len(platform.calls)
    assert runner.pipeline(context(tmp_path, root, platform, ctx.queue), examples["outcome"]) == RunStage.COMPLETED
    # every step returned its recorded result: no session, and nothing but the design-only completion is written again
    assert len(platform.calls) == calls and [e.kind for e in ctx.store.events(RUN)[n:]] == ["run.completed"]


@pytest.mark.parametrize("written", [False, True], ids=["before-the-write", "after-the-write"])
def test_a_controller_that_dies_at_any_event_is_finished_by_the_next_one(tmp_path, root, examples, reference, written):
    """Every point of the run, both sides of every write. Event 1 is run.created: before it there is no run to resume."""
    for n in range(2, reference["events"] + 1):
        runs = tmp_path / f"{n}"
        platform, queue = Platform(examples), AutoApproveQueue()
        with pytest.raises(SystemExit):
            runner.run(dying_at(context(runs, root, platform, queue), n, written), examples["outcome"])
        ctx = context(runs, root, platform, queue)
        if ctx.store.state(RUN).stage == RunStage.COMPLETED:
            continue                                     # it died after its last write: nothing left to do
        assert runner.resume(ctx) == RunStage.COMPLETED, n
        assert outcome_of(ctx) == reference["outcome"], n
        assert platform.keys() == reference["sessions"], f"event {n}: a finished session was paid for again"
        assert_log_valid(ctx.store, RUN, ctx.cfg.schemas_dir)


def test_a_controller_that_dies_while_a_session_works_is_followed_by_the_next_one(tmp_path, root, examples, reference):
    for n in range(1, len(reference["sessions"]) + 1):
        runs = tmp_path / f"{n}"
        platform, queue = Platform(examples), AutoApproveQueue()
        platform.die_in_session = n
        with pytest.raises(SystemExit):
            runner.run(context(runs, root, platform, queue), examples["outcome"])
        ctx = context(runs, root, platform, queue)
        assert runner.resume(ctx) == RunStage.COMPLETED, n
        assert platform.keys() == reference["sessions"] and len(platform.attached) == 1, n
        assert outcome_of(ctx) == reference["outcome"], n


def test_a_gate_still_open_when_its_controller_died_is_waited_on_not_opened_again(tmp_path, root, examples, monkeypatch):
    """Gate 1 is open and undecided when the controller dies; the owner decides while no controller runs."""
    from factory.stages import common

    class Silent(InMemoryQueue):
        """The owner decides nothing until asked to."""

    platform, queue = Platform(examples), Silent()

    def die_while_waiting(seconds):
        raise SystemExit("controller died at Gate 1")
    monkeypatch.setattr(common.time, "sleep", die_while_waiting)
    with pytest.raises(SystemExit):
        runner.run(context(tmp_path, root, platform, queue), examples["outcome"])
    monkeypatch.undo()
    (gate,) = queue.gates.values()
    assert gate.kind == GateKind.DECOMPOSITION_REVIEW
    queue.decide(GateDecision(gate_id=gate.gate_id, decision=Decision.APPROVE, decided_by="owner",
                              artifact_shas=tuple(a.sha256 for a in gate.artifacts)))
    auto = AutoApproveQueue()
    auto.gates, auto.decisions = queue.gates, queue.decisions
    ctx = context(tmp_path, root, platform, auto)
    assert runner.resume(ctx) == RunStage.COMPLETED
    opened = [e.payload["gate_id"] for e in ctx.store.events(RUN) if e.kind == "gate.opened" and e.payload["kind"] == gate.kind.value]
    assert opened == [gate.gate_id]
    resumed = next(e.payload for e in ctx.store.events(RUN) if e.kind == "run.resumed")
    assert resumed["stage"] == "gate_1_decomposition" and resumed["gate_id"] == gate.gate_id


def test_a_request_for_changes_is_replayed_with_the_feedback_the_owner_wrote_then(tmp_path, root, examples):
    """Gate 1 asks for changes, the second decomposition runs, the controller dies at Gate 1 again. The replay gives the
    second decomposition the same feedback (recorded with the gate's step), so its step is reused, not re-run."""

    class ChangesOnce(AutoApproveQueue):
        asked = False

        def open(self, gate):
            InMemoryQueue.open(self, gate)
            if gate.kind == GateKind.DECOMPOSITION_REVIEW and not self.asked:
                self.asked = True
                self.decisions[gate.gate_id] = GateDecision(gate_id=gate.gate_id, decision=Decision.REQUEST_CHANGES,
                                                            decided_by="owner", comments="wi-smart-retries: split the schema change",
                                                            artifact_shas=tuple(a.sha256 for a in gate.artifacts))
            elif gate.kind == GateKind.DECOMPOSITION_REVIEW:
                raise SystemExit("controller died at the second Gate 1")
            else:
                AutoApproveQueue.open(self, gate)

    platform, queue = Platform(examples), ChangesOnce()
    with pytest.raises(SystemExit):
        runner.run(context(tmp_path, root, platform, queue), examples["outcome"])
    assert platform.keys() == ["decomposer", "decomposer"]
    resumed_queue = AutoApproveQueue()
    resumed_queue.gates, resumed_queue.decisions = queue.gates, queue.decisions
    ctx = context(tmp_path, root, platform, resumed_queue)
    assert runner.resume(ctx) == RunStage.COMPLETED
    assert platform.keys()[:2] == ["decomposer", "decomposer"] and platform.keys().count("decomposer") == 2
    second = platform.calls[1]
    assert b"split the schema change" in second["uploads"]["prior/feedback.md"]


def test_a_spec_pass_paused_at_its_budget_reuses_the_writers_it_already_paid_for(tmp_path, root, examples):
    """The judge's reservation does not fit: the writers have finished, the run pauses. The owner raises the budget by
    what the gate asked for (the judge's cap), and the replay re-enters the specification step: the finished writers are
    reused without reserving their caps again, and only the judge runs."""
    platform = Platform(examples)
    queue = AutoApproveQueue()
    caps = {**CAPS, "decompose": 6.0, "specify": 1.0}
    ctx = context(tmp_path, root, platform, queue)
    ctx.policy = Policy(run_budget_usd=16.0, design_only=True, stage_budget_usd=caps)
    assert runner.run(ctx, examples["outcome"]) == RunStage.PAUSED_BUDGET
    writers = platform.keys().count("spec_writer")
    assert writers and "spec_judge" not in platform.keys()
    ctx2 = context(tmp_path, root, platform, queue)
    ctx2.policy = Policy(run_budget_usd=16.0, design_only=True, stage_budget_usd=caps)
    needed = [e.payload for e in ctx.store.events(RUN) if e.kind == "budget.reached"][-1]["needed_usd"]
    assert runner.resume(ctx2, budget_usd=needed) == RunStage.COMPLETED
    assert platform.keys().count("spec_writer") == writers and platform.keys().count("spec_judge") == 1


def test_a_run_that_failed_while_specifying_is_resumable(tmp_path, root, examples):
    """Before phase 2 only a run that reached implementation (or stopped idle at Gate 2) could be resumed."""
    platform, queue = Platform(examples), AutoApproveQueue()
    ctx = context(tmp_path, root, platform, queue)

    class Boom(Exception):
        pass
    calls = {"n": 0}
    real_run = platform.run

    def flaky(**kw):
        if kw.get("agent_key") == "spec_judge" and not calls["n"]:
            calls["n"] += 1
            raise Boom("the judge's platform call failed")
        return real_run(**kw)
    platform.run = flaky
    with pytest.raises(Boom):
        runner.run(ctx, examples["outcome"])
    ctx.event("run.failed", {"reason": "Boom: the judge's platform call failed", "interrupted_sessions": [], "sessions_left_running": []})
    ctx2 = context(tmp_path, root, platform, queue)
    assert runner.resume(ctx2) == RunStage.COMPLETED
    resumed = next(e.payload for e in ctx2.store.events(RUN) if e.kind == "run.resumed")
    assert resumed["stage"] == "specifying" and resumed["from"] == "failed"
    assert platform.keys().count("decomposer") == 1          # the decomposition step was not run again


def test_a_session_whose_output_was_refused_runs_again_and_the_others_are_reused(tmp_path, root, examples):
    """One writer leaves out spec.md: its output is refused and the run fails. The resume starts a new session for that
    writer only; the decomposer and the other writers come back from their steps."""
    platform, queue = Platform(examples), AutoApproveQueue()
    real_run = platform.run
    broken = {"wid": None}

    def no_spec_md_once(**kw):
        res = real_run(**kw)
        if kw.get("agent_key") == "spec_writer" and broken["wid"] is None and not kw.get("attach_session_id"):
            broken["wid"] = kw["work_item_id"]
            res = type(res)(**{**res.__dict__, "outputs": {"spec.json": res.outputs["spec.json"]}})
            platform.sessions[res.session_id] = res
        return res
    platform.run = no_spec_md_once
    with pytest.raises(RuntimeError, match="no spec.md"):
        runner.run(context(tmp_path, root, platform, queue), examples["outcome"])
    ctx = context(tmp_path, root, platform, queue)
    ctx.event("run.failed", {"reason": "RuntimeError: no spec.md", "interrupted_sessions": [], "sessions_left_running": []})
    writers = platform.keys().count("spec_writer")
    assert runner.resume(context(tmp_path, root, platform, queue)) == RunStage.COMPLETED
    again = [c["work_item_id"] for c in platform.calls[len(platform.calls) - 2:] if c["agent_key"] == "spec_writer"]
    assert platform.keys().count("spec_writer") == writers + 1 and again == [broken["wid"]]
    assert platform.keys().count("decomposer") == 1


def test_a_spec_implemented_before_the_controller_died_is_not_implemented_again(tmp_path, root, examples, monkeypatch):
    """Each spec's implementation and its change request are steps of their own. The controller dies opening the second
    change request; the resume opens it, and builds nothing again."""
    built, opened = [], []

    def implement(ctx, spec, repo_url, base_branch, verifier, deps, *, spec_md="", resume_from=None, guidance=""):
        built.append(spec["spec_id"])
        return {"result": "ready", "round": 1, "_head_sha": f"head-{spec['spec_id']}", "_branch": f"factory/{spec['spec_id']}"}

    def integrate(ctx, spec, verdict, findings, response, test_results, summary, base_branch, **kw):
        sid = spec["spec_id"]
        if len(opened) == 1 and not integrate.died:
            integrate.died = True
            raise SystemExit("controller died opening a change request")
        opened.append(sid)
        ctx.event("cr.opened", {"id": len(opened), "url": f"https://example.test/pull/{len(opened)}", "branch": verdict["_branch"]},
                  work_item_id=spec["work_item_id"], spec_id=sid)
        return {"id": len(opened), "url": f"https://example.test/pull/{len(opened)}"}
    integrate.died = False
    # the review and test evidence the change request quotes: none in this test, read as empty documents
    monkeypatch.setattr(runner, "_latest", lambda ctx, stage, prefix, suffix: "x")
    monkeypatch.setattr(runner, "_latest_json_or", lambda *a: {"responses": []})
    monkeypatch.setattr(runner, "run_implement_one", implement)
    monkeypatch.setattr(runner, "run_integrate", integrate)

    platform, queue = Platform(examples), AutoApproveQueue()
    ctx = context(tmp_path, root, platform, queue, design_only=False)
    real_get = ctx.store.get_artifact
    ctx.store.get_artifact = lambda run_id, stage, name: b"{}" if name == "x" else real_get(run_id, stage, name)
    with pytest.raises(SystemExit):
        runner.run(ctx, examples["outcome"])
    assert len(built) == 2 and len(opened) == 1
    ctx2 = context(tmp_path, root, platform, queue, design_only=False)
    ctx2.store.get_artifact = ctx.store.get_artifact
    assert runner.resume(ctx2) == RunStage.COMPLETED
    approved = sorted(e.spec_id for e in ctx2.store.events(RUN) if e.kind == "work_item.stage" and e.payload["stage"] == "spec_approved")
    assert sorted(built) == sorted(set(built)) and len(built) == len(approved)       # each spec built once
    assert sorted(opened) == approved and platform.keys().count("spec_judge") == 1


def test_a_controller_that_died_at_a_sessions_budget_gate_continues_from_the_owners_decision(tmp_path, root, examples,
                                                                                           monkeypatch):
    """One writer hits its own cap; its budget gate opens and the controller dies waiting. The owner approves while no
    controller runs. The replay finds that gate decided (not a new one), re-runs that writer with the raised cap, and
    reuses everything else."""
    from factory.stages import common

    class CappedOnce(Platform):
        capped = False

        def run(self, **kw):
            res = super().run(**kw)
            if kw.get("agent_key") == "spec_writer" and not kw.get("attach_session_id") and not self.capped:
                self.capped = True
                res = type(res)(**{**res.__dict__, "stop_reason": "budget_reached"})
                self.sessions[res.session_id] = res
            return res

    class OwnerLater(AutoApproveQueue):
        def open(self, gate):
            if gate.kind == GateKind.BUDGET:
                InMemoryQueue.open(self, gate)             # the owner decides this one later
            else:
                AutoApproveQueue.open(self, gate)

    platform, queue = CappedOnce(examples), OwnerLater()

    def die_while_waiting(seconds):
        raise SystemExit("controller died at the session's budget gate")
    monkeypatch.setattr(common.time, "sleep", die_while_waiting)
    with pytest.raises(SystemExit):
        runner.run(context(tmp_path, root, platform, queue), examples["outcome"])
    monkeypatch.undo()
    (gate,) = [g for g in queue.gates.values() if g.kind == GateKind.BUDGET]
    queue.decide(GateDecision(gate_id=gate.gate_id, decision=Decision.APPROVE, decided_by="owner", artifact_shas=()))
    ctx = context(tmp_path, root, platform, queue)
    assert ctx.store.state(RUN).stage == RunStage.PAUSED_BUDGET
    writers = platform.keys().count("spec_writer")
    assert runner.resume(ctx) == RunStage.COMPLETED
    assert platform.keys().count("spec_writer") == writers + 1            # only the capped writer runs again
    assert [g for g in queue.gates.values() if g.kind == GateKind.BUDGET] == [gate]
    assert next(e.payload for e in ctx.store.events(RUN) if e.kind == "run.resumed")["stage"] == "specifying"


def test_an_older_controllers_run_stopped_mid_design_is_refused_with_a_reason(tmp_path, root, examples):
    ctx = context(tmp_path, root, Platform(examples), AutoApproveQueue())
    ctx.event("run.created", {"outcome_id": "o", "title": "t", "policy": {"preview_mode": "local"}, "budget_usd": 50}, stage="intake")
    ctx.store.put_artifact(RUN, "intake", "outcome.json", json.dumps(examples["outcome"]).encode())
    ctx.event("stage.entered", {"stage": "decomposing"}, stage="decompose")
    ctx.store.put_artifact(RUN, "decompose", "r1-decomposition.json", json.dumps(examples["decomposition"]).encode())
    ctx.event("run.failed", {"reason": "crash", "interrupted_sessions": []})
    n = len(ctx.store.events(RUN))
    with pytest.raises(RuntimeError, match="older controller"):
        runner.resume(context(tmp_path, root, Platform(examples), AutoApproveQueue()))
    assert len(ctx.store.events(RUN)) == n and not ctx.store.steps(RUN)
