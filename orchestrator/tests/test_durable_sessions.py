"""ADR-0013: an agent session is a durable step. A controller that dies leaves its sessions working, and the next one
follows them by id instead of paying for them again; changed inputs or a stop end them. No network."""
import json
from types import SimpleNamespace

import pytest
from conftest import make_config
from support.sessions import AutoQueue, SpecStageDriver, run_after_gate_1, stage_result

from factory.gates import Decision
from factory.sessions import SessionContext
from factory.stages.common import make_ask_human, run_session, session_step_key

KW = {"agent_key": "decomposer", "title": "run_budget000001 decompose", "run_id": "run_budget000001",
      "uploads": {"outcome.json": b"{}"}, "description": "Decompose the outcome.", "rubric_md": None, "max_iterations": 1,
      "budget_usd": 4.0}
KEY = session_step_key("decompose", "", KW["title"], 1)


class Crash(RuntimeError):
    """The controller dies while a session it started is still working."""


class StepDriver:
    """Starts sessions `sesn_1`, `sesn_2`, ... and returns each one finished; `crash` makes the next new session's
    controller die right after the session exists. Follows an existing session when asked to."""

    def __init__(self, cost=0.75):
        self.calls, self.interrupted, self.crash, self.made, self.cost = [], [], False, 0, cost

    def run(self, **kw):
        self.calls.append(kw)
        if kw.get("attach_session_id"):
            return stage_result({"decomposition.json": b"{}"}, sid=kw["attach_session_id"], cost=self.cost)
        self.made += 1
        sid = f"sesn_{self.made}"
        kw["on_created"](sid)
        if self.crash:
            self.crash = False
            raise Crash("controller died")
        return stage_result({"decomposition.json": b"{}"}, sid=sid, cost=self.cost)

    def interrupt_session(self, session_id):
        self.interrupted.append(session_id)
        return True

    def list_cost(self, session_id):
        return 0.4


def _run(tmp_path, root, examples, driver):
    return run_after_gate_1(tmp_path, root, examples, driver, AutoQueue(Decision.APPROVE))


def _kinds(ctx, kind):
    return [ev for ev in ctx.store.events(ctx.run_id) if ev.kind == kind]


def test_a_new_controller_reattaches_to_the_session_its_predecessor_left_running(tmp_path, root, examples):
    d = StepDriver()
    ctx = _run(tmp_path, root, examples, d)
    d.crash = True
    with pytest.raises(Crash):
        run_session(ctx, stage="decompose", **KW)
    step = ctx.store.step(ctx.run_id, KEY)
    assert step.status == "running" and step.session_id == "sesn_1"      # the session outlives its controller

    res = run_session(ctx, stage="decompose", **KW)
    assert res.session_id == "sesn_1" and d.made == 1                    # followed, not paid for again
    assert d.calls[-1]["attach_session_id"] == "sesn_1" and "on_created" not in d.calls[-1]
    assert [ev.payload["session_id"] for ev in _kinds(ctx, "session.reattached")] == ["sesn_1"]
    step = ctx.store.step(ctx.run_id, KEY)
    assert step.status == "done" and step.result["session_id"] == "sesn_1"
    assert ctx.store.get_blob(ctx.run_id, step.result["outputs"]["decomposition.json"]) == b"{}"
    state = ctx.store.state(ctx.run_id)
    assert state.sessions_finished == 1 and state.list_cost_usd == 0.75


def test_a_session_whose_inputs_changed_is_interrupted_and_charged_then_replaced(tmp_path, root, examples):
    d = StepDriver()
    ctx = _run(tmp_path, root, examples, d)
    d.crash = True
    with pytest.raises(Crash):
        run_session(ctx, stage="decompose", **KW)

    res = run_session(ctx, stage="decompose", **{**KW, "description": "Decompose the revised outcome."})
    assert d.interrupted == ["sesn_1"] and res.session_id == "sesn_2"
    (superseded,) = _kinds(ctx, "session.superseded")
    assert superseded.payload["session_id"] == "sesn_1" and superseded.payload["interrupted"] is True
    finished = {ev.payload["session_id"]: ev.payload for ev in _kinds(ctx, "session.finished")}
    assert finished["sesn_1"]["stop_reason"] == "superseded" and finished["sesn_1"]["list_cost_usd"] == 0.4
    state = ctx.store.state(ctx.run_id)
    assert state.sessions_finished == 2 and state.list_cost_usd == pytest.approx(1.15)
    step = ctx.store.step(ctx.run_id, KEY)
    assert step.status == "done" and step.session_id == "sesn_2" and step.attempt == 2


def test_a_session_a_stop_interrupted_is_not_followed(tmp_path, root, examples):
    """`_drive` interrupts the sessions in flight when someone stops the run and records their end; the next
    controller starts the work again rather than following a session that will never finish it."""
    d = StepDriver()
    ctx = _run(tmp_path, root, examples, d)
    d.crash = True
    with pytest.raises(Crash):
        run_session(ctx, stage="decompose", **KW)
    ctx.event("session.finished", {"session_id": "sesn_1", "list_cost_usd": 0.3, "outcome_result": "interrupted",
                                   "stop_reason": "interrupted"}, stage="decompose", actor="platform")

    res = run_session(ctx, stage="decompose", **KW)
    assert res.session_id == "sesn_2" and d.interrupted == [] and not _kinds(ctx, "session.reattached")
    assert ctx.store.state(ctx.run_id).list_cost_usd == pytest.approx(1.05)


def test_a_session_that_never_started_fails_its_step(tmp_path, root, examples):
    class NoCreate(StepDriver):
        down = True                     # the platform refuses the first create, then recovers

        def run(self, **kw):
            if self.down:
                self.down = False
                raise RuntimeError("sessions.create timed out")
            return super().run(**kw)

    d = NoCreate()
    ctx = _run(tmp_path, root, examples, d)
    with pytest.raises(RuntimeError, match="timed out"):
        run_session(ctx, stage="decompose", **KW)
    step = ctx.store.step(ctx.run_id, KEY)
    assert step.status == "failed" and step.session_id is None and "timed out" in step.error
    assert run_session(ctx, stage="decompose", **KW).session_id == "sesn_1"


def test_a_session_reported_finished_twice_is_charged_once(tmp_path, root, examples):
    """The controller died after recording the session's end but before closing its step: the next one reattaches,
    the session returns at once, and its spend is not counted again."""
    d = StepDriver()
    ctx = _run(tmp_path, root, examples, d)
    d.crash = True
    with pytest.raises(Crash):
        run_session(ctx, stage="decompose", **KW)
    ctx.event("session.finished", {"session_id": "sesn_1", "list_cost_usd": 0.75, "outcome_result": "satisfied",
                                   "stop_reason": "end_turn"}, stage="decompose", actor="platform")

    assert run_session(ctx, stage="decompose", **KW).session_id == "sesn_1"
    assert len(_kinds(ctx, "session.finished")) == 1
    state = ctx.store.state(ctx.run_id)
    assert state.sessions_finished == 1 and state.list_cost_usd == 0.75


def test_a_replayed_question_is_not_asked_again(tmp_path, root, examples):
    """A reattached controller replays the session's unanswered tool calls; an answered question returns its answer."""
    ctx = _run(tmp_path, root, examples, SpecStageDriver(examples))
    handler = make_ask_human(ctx, "writer", poll_seconds=0, timeout_hours=0)
    sctx = SessionContext("sesn_1", ctx.run_id, "specify", "wi-test", custom_tool_use_id="sevt_q1")
    first = json.loads(handler({"question": "Which default?", "blocking": False, "default_if_unanswered": "A"}, sctx))
    again = json.loads(handler({"question": "Which default?", "blocking": False, "default_if_unanswered": "A"}, sctx))
    assert first["answer"] == again["answer"] == "A"
    assert len(_kinds(ctx, "question.asked")) == 1 and len(_kinds(ctx, "question.answered")) == 1
    assert len(ctx.queue.questions) == 1


def test_a_question_open_when_the_controller_died_is_waited_on_not_asked_again(tmp_path, root, examples, monkeypatch):
    from factory.gates import Answer
    from factory.stages import common
    ctx = _run(tmp_path, root, examples, SpecStageDriver(examples))
    handler = make_ask_human(ctx, "writer", poll_seconds=1)
    sctx = SessionContext("sesn_1", ctx.run_id, "specify", "wi-test", custom_tool_use_id="sevt_q2")
    call = {"question": "Keep the old endpoint?", "blocking": True, "default_if_unanswered": "no"}

    def die(seconds):
        raise Crash("controller died while waiting")

    monkeypatch.setattr(common.time, "sleep", die)
    with pytest.raises(Crash):
        handler(call, sctx)
    (asked,) = _kinds(ctx, "question.asked")
    ctx.queue.reply(Answer(question_id=asked.payload["question_id"], answer="yes", answered_by="priya.n"))

    assert json.loads(handler(call, sctx)) == {"answer": "yes", "answered_by": "priya.n"}
    assert len(_kinds(ctx, "question.asked")) == 1 and len(ctx.queue.questions) == 1
    assert [ev.payload["answer"] for ev in _kinds(ctx, "question.answered")] == ["yes"]


def test_a_replayed_open_change_request_returns_the_first_one(tmp_path, root, examples):
    from factory.forge import ChangeRequest
    from factory.stages.integrate import make_open_change_request
    ctx = _run(tmp_path, root, examples, SpecStageDriver(examples))
    calls = []
    forge = SimpleNamespace(kind="github", create_change_request=lambda **kw: calls.append(kw) or ChangeRequest(7, "https://example.test/pull/7", "b", "main"))
    handler = make_open_change_request(ctx, forge, "spec_test", "wi-test", "b", "main")
    call = {"source_branch": "b", "target_branch": "main", "title": "Change", "description_markdown": "Evidence"}
    first, again = json.loads(handler(call, None)), json.loads(handler(call, None))
    assert first == again and first["url"] == "https://example.test/pull/7"
    assert len(calls) == 1 and len(_kinds(ctx, "cr.opened")) == 1


def test_a_crash_leaves_sessions_running(tmp_path, root, examples, monkeypatch):
    import signal

    from factory.cli import _drive
    monkeypatch.setattr(signal, "signal", lambda *a: None)      # keep the test process's own handlers

    class Active(StepDriver):
        def __init__(self):
            super().__init__()
            self.active = {"sesn_9": {"stage": "implement"}}

        def interrupt_active(self):
            raise AssertionError("a crash must not interrupt the sessions in flight")

    ctx = _run(tmp_path, root, examples, Active())

    def pipeline(ctx):
        raise KeyError("controller bug")

    with pytest.raises(KeyError):
        _drive(ctx, pipeline)
    (failed,) = _kinds(ctx, "run.failed")
    assert failed.payload["sessions_left_running"] == ["sesn_9"] and failed.payload["interrupted_sessions"] == []


def test_the_driver_follows_an_existing_session_without_creating_one(tmp_path, root, monkeypatch):
    """Reattaching drains the session's history: a tool call the first controller answered is not answered again, one
    it never answered is handled now, and nothing is created or uploaded."""
    from test_session_stream import FakeStream, Page, ev

    from factory import sessions
    from factory.config import AgentRef
    from factory.sessions import SessionDriver
    monkeypatch.setattr(sessions.time, "sleep", lambda s: None)

    answered = ev("e1", "agent.custom_tool_use", name="ask_human", input={"question": "q1"})
    result = ev("e2", "user.custom_tool_result", custom_tool_use_id="e1")
    pending = ev("e3", "agent.custom_tool_use", name="ask_human", input={"question": "q2"})
    idle = ev("e4", "session.status_idle", stop_reason=SimpleNamespace(type="end_turn"))
    sent, handled = [], []

    class Events:
        def stream(self, session_id): return FakeStream([idle])
        def list(self, session_id, **kw): return Page([answered, result, pending])
        def send(self, **kw): sent.append(kw)

    class Sessions:
        events = Events()
        def create(self, **kw): raise AssertionError("reattaching must not create a session")
        def retrieve(self, **kw): return SimpleNamespace(status="idle", usage=SimpleNamespace(list_cost=SimpleNamespace(amount="250")))

    class Files:
        def upload(self, **kw): raise AssertionError("reattaching must not upload")
        def list(self, **kw): return Page([])

    class Client:
        beta = SimpleNamespace(sessions=Sessions(), files=Files())

    cfg = make_config(root, tmp_path, environment_id="env_1")
    cfg = type(cfg)(**{**cfg.__dict__, "agents": {"implementer": AgentRef("agent_i", 1)}})
    created = []

    def ask(tool_input, sctx):
        handled.append((tool_input["question"], sctx.custom_tool_use_id))
        return json.dumps({"answer": "yes"})

    res = SessionDriver(cfg, client=Client()).run(
        agent_key="implementer", title="t", run_id="run_x", stage="implement", uploads={"a.json": b"{}"}, description="d",
        rubric_md=None, max_iterations=1, budget_usd=1.0, custom_tools={"ask_human": ask},
        attach_session_id="sesn_old", on_created=created.append)
    assert res.session_id == "sesn_old" and res.stop_reason == "end_turn" and res.list_cost_usd == 2.5
    assert handled == [("q2", "e3")] and [s["events"][0]["custom_tool_use_id"] for s in sent] == ["e3"]
    assert created == []


def test_a_run_that_ends_stops_and_charges_the_sessions_it_never_followed_again(tmp_path, root, examples):
    """The resumed run took another path (here: the owner closed it at Gate 1) and never reached the step its
    predecessor's session works under. Nobody will read that session's result: it is stopped and charged once the run has ended, and kept
    while the run is only paused."""
    from factory.stages.common import close_orphaned_sessions, orphaned_sessions
    d = StepDriver()
    ctx = _run(tmp_path, root, examples, d)
    d.crash = True
    with pytest.raises(Crash):
        run_session(ctx, stage="decompose", **KW)
    assert orphaned_sessions(ctx) == ["sesn_1"]
    assert close_orphaned_sessions(ctx) == [] and d.interrupted == []      # the run is not over: a resume may follow it

    ctx.event("stage.entered", {"stage": "closed"}, actor="human")
    assert close_orphaned_sessions(ctx) == ["sesn_1"] and d.interrupted == ["sesn_1"]
    assert ctx.store.step(ctx.run_id, KEY).status == "failed" and orphaned_sessions(ctx) == []
    (finished,) = _kinds(ctx, "session.finished")
    assert finished.payload["session_id"] == "sesn_1" and finished.payload["list_cost_usd"] == 0.4


def test_the_console_keeps_a_followed_session_working_across_a_resume(tmp_path):
    """The counterpart of test_experience_review's orphan: a session the resumed controller follows is still working,
    and one it does not (a log from before durable steps) still reads as cut short."""
    from factory.state import replay
    from factory.store import RunStore
    from factory.summary import summarize
    from factory.web.app import crew_roster
    store, actor = RunStore(tmp_path), {"type": "system", "id": "test"}
    rid = "run_followed0001"
    store.append(rid, "run.created", actor, {"policy": {"preview_mode": "local"}, "budget_usd": 50, "outcome_id": "o", "title": "Follow"})
    for stage in ("decomposing", "gate_1_decomposition", "specifying", "gate_2_spec_readiness", "implementing"):
        store.append(rid, "stage.entered", actor, {"stage": stage})
    store.append(rid, "session.started", actor, {"session_id": "kept", "agent_key": "implementer", "title": "implement"}, stage="implement")
    store.append(rid, "session.started", actor, {"session_id": "lost", "agent_key": "reviewer", "title": "review"}, stage="review")
    store.append(rid, "run.failed", actor, {"reason": "KeyError: 'x'", "interrupted_sessions": [], "sessions_left_running": ["kept"]})
    store.append(rid, "run.resumed", actor, {"stage": "implementing", "from": "failed", "points": {}, "sessions_to_follow": ["kept"]})
    events = store.events(rid)
    s = summarize(events, replay(rid, events), controller_alive=True)
    assert s["working"] == ["implementer"] and s["live_sessions"] == 1
    roster = {r["role"]: r for r in crew_roster(events)}
    assert roster["implementer"]["state"] == "working" and roster["implementer"]["live_count"] == 1
    assert roster["reviewer"]["state"] == "interrupted" and roster["reviewer"]["interrupted_by"] == "resume"


def test_the_state_counts_a_sessions_spend_once_however_often_its_end_is_reported(tmp_path, root, examples):
    ctx = _run(tmp_path, root, examples, SpecStageDriver(examples))
    for _ in range(2):
        ctx.event("session.finished", {"session_id": "sesn_7", "list_cost_usd": 1.25, "stop_reason": "end_turn"},
                  stage="decompose", actor="platform")
    state = ctx.store.state(ctx.run_id)
    assert state.sessions_finished == 1 and state.list_cost_usd == 1.25 and state.finished_sessions == {"sesn_7": "end_turn"}


def test_the_inputs_hash_names_the_work_not_the_credential_that_reaches_it():
    from factory.stages.common import session_inputs_sha
    repo = {"type": "github_repository", "url": "https://github.com/o/r", "authorization_token": "ghp_old",
            "checkout": {"type": "branch", "name": "factory/x"}}
    kw = {**KW, "repository": repo}
    rotated = {**KW, "repository": {**repo, "authorization_token": "ghp_new"}}
    moved = {**KW, "repository": {**repo, "checkout": {"type": "branch", "name": "factory/y"}}}
    assert session_inputs_sha("implement", kw) == session_inputs_sha("implement", rotated)
    assert session_inputs_sha("implement", kw) != session_inputs_sha("implement", moved)
    assert session_inputs_sha("implement", kw) != session_inputs_sha("implement", {**kw, "uploads": {"outcome.json": b"{ }"}})
