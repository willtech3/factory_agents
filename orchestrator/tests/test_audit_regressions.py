"""Behavioral regressions from the October 10 audit; no paid sessions or live mutations."""
import json
import multiprocessing
from types import SimpleNamespace

import httpx
import pytest
from conftest import make_config
from test_session_stream import FakeStream, Page, ev

from factory.config import AgentRef
from factory.gates import Decision, Gate, GateKind, Question
from factory.sessions import SessionDriver
from factory.surfaces.filequeue import FileQueue


def _queue_burst(root, worker):
    q = FileQueue(root)
    for n in range(20):
        q.ask(Question(run_id="run_test0001", asked_by="writer", question=f"{worker}-{n}",
                       why_it_matters="", default_if_unanswered="", blocking=True,
                       question_id=f"q_{worker}_{n}"))


def test_parallel_queue_writers_keep_every_question(tmp_path):
    # Each process has its own queue and Python locks. Atomic rename alone loses updates.
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(4) as pool:
        pool.starmap(_queue_burst, [(tmp_path, w) for w in range(4)])
    assert len(FileQueue(tmp_path).open_questions()) == 80


def test_queue_rejects_invalid_decision_before_persisting(tmp_path):
    q = FileQueue(tmp_path)
    gate = Gate(run_id="run_test0001", kind=GateKind.DECOMPOSITION_REVIEW, artifacts=(),
                assignees=(), summary="", options=(Decision.APPROVE, Decision.REQUEST_CHANGES))
    q.open(gate)
    with pytest.raises(ValueError):
        q.record_decision(gate.gate_id, "request_changes", "owner", "")
    assert q.decision(gate.gate_id) is None


def driver(tmp_path, root, streams, histories, send=None):
    cfg = make_config(root, tmp_path, environment_id="env_test")
    cfg = type(cfg)(**{**cfg.__dict__, "agents": {"implementer": AgentRef("agent_i", 1)}})
    calls = {"streams": 0, "sends": []}
    streams, histories = iter(streams), iter(histories)

    class Events:
        def stream(self, **kw):
            calls["streams"] += 1
            return next(streams)

        def list(self, **kw):
            return next(histories)

        def send(self, **kw):
            calls["sends"].append(kw)
            if send:
                send(len(calls["sends"]))

    class Sessions:
        events = Events()
        def create(self, **kw): return SimpleNamespace(id="sesn_test")
        def retrieve(self, **kw):
            return SimpleNamespace(status="idle", usage=SimpleNamespace(list_cost=SimpleNamespace(amount="123")))

    class Files:
        def upload(self, **kw): return SimpleNamespace(id="file_test")
        def list(self, **kw): return Page([])

    return SessionDriver(cfg, client=SimpleNamespace(beta=SimpleNamespace(sessions=Sessions(), files=Files()))), calls


def run_driver(d, **kw):
    return d.run(agent_key="implementer", title="test", run_id="run_test0001", stage="implement", uploads={},
                 description="test", rubric_md=None, max_iterations=1, budget_usd=2, **kw)


def test_session_history_reads_beyond_first_page(tmp_path, root, monkeypatch):
    monkeypatch.setattr("factory.sessions.time.sleep", lambda _: None)
    idle = ev("done", "session.status_idle", stop_reason=SimpleNamespace(type="end_turn"))
    page = Page([ev("first", "agent.thinking")])
    page.has_more = True
    page.get_next_page = lambda: Page([ev("graded", "span.outcome_evaluation_end", result="satisfied", iteration=2), idle])
    d, _ = driver(tmp_path, root, [FakeStream([])], [page])
    res = run_driver(d)
    assert (res.stop_reason, res.outcome_result, res.outcome_iterations) == ("end_turn", "satisfied", 3)


def test_clean_stream_eof_is_not_session_completion(tmp_path, root, monkeypatch):
    monkeypatch.setattr("factory.sessions.time.sleep", lambda _: None)
    idle = ev("done", "session.status_idle", stop_reason=SimpleNamespace(type="end_turn"))
    d, calls = driver(tmp_path, root, [FakeStream([]), FakeStream([idle])], [Page([]), Page([])])
    assert run_driver(d).stop_reason == "end_turn"
    assert calls["streams"] == 2


def test_tool_result_delivery_retries_without_repeating_side_effect(tmp_path, root, monkeypatch):
    monkeypatch.setattr("factory.sessions.time.sleep", lambda _: None)
    tool = ev("tool", "agent.custom_tool_use", name="open_change_request", input={})
    idle = ev("done", "session.status_idle", stop_reason=SimpleNamespace(type="end_turn"))

    def flaky_send(n):
        if n == 1:
            raise httpx.ReadError("result delivery interrupted")

    d, calls = driver(tmp_path, root, [FakeStream([]), FakeStream([idle])],
                      [Page([tool]), Page([tool])], flaky_send)
    handled = []
    res = run_driver(d, custom_tools={"open_change_request": lambda *_: handled.append(1) or '{"id": 42}'})
    assert len(handled) == 1
    assert len(calls["sends"]) == 2
    assert calls["sends"][0] == calls["sends"][1]
    assert len(res.custom_tool_calls) == 1


def test_result_accepted_before_disconnect_is_not_sent_again(tmp_path, root, monkeypatch):
    monkeypatch.setattr("factory.sessions.time.sleep", lambda _: None)
    tool = ev("tool", "agent.custom_tool_use", name="open_change_request", input={})
    ack = ev("ack", "user.custom_tool_result", custom_tool_use_id="tool")
    idle = ev("done", "session.status_idle", stop_reason=SimpleNamespace(type="end_turn"))

    def lost_response(_):
        raise httpx.ReadError("server accepted the result; response lost")

    d, calls = driver(tmp_path, root, [FakeStream([]), FakeStream([])],
                      [Page([tool]), Page([tool, ack, idle])], lost_response)
    handled = []
    assert run_driver(d, custom_tools={"open_change_request": lambda *_: handled.append(1) or '{}'}).stop_reason == "end_turn"
    assert len(handled) == len(calls["sends"]) == 1


def test_accepted_iteration_cutoff_counts_each_session_once(tmp_path, root, examples):
    from test_session_cutoff import AutoQueue, Driver, _ctx

    from factory.stages.specify import run_specify
    ctx = _ctx(tmp_path, root, examples, Driver(examples, judge_truncations=0, writer_iterations_exhausted=True),
               AutoQueue(Decision.APPROVE))
    run_specify(ctx, examples["outcome"], examples["decomposition"])
    st = ctx.store.state(ctx.run_id)
    assert st.sessions_finished == 5
    assert st.list_cost_usd == pytest.approx(4 * 1.2 + 2)


def test_missing_output_still_records_session_spend(tmp_path, root, examples):
    from test_session_cutoff import AutoQueue, _ctx, _result

    from factory.stages.specify import run_specify
    result = _result({"spec.json": json.dumps(examples["spec_smart_retries"]).encode()})
    ctx = _ctx(tmp_path, root, examples, SimpleNamespace(run=lambda **kw: result), AutoQueue(Decision.APPROVE))
    decomposition = {**examples["decomposition"], "work_items": examples["decomposition"]["work_items"][:1]}
    with pytest.raises(RuntimeError, match="no spec.md"):
        run_specify(ctx, examples["outcome"], decomposition)
    assert ctx.store.state(ctx.run_id).sessions_finished == 1
    assert ctx.store.state(ctx.run_id).list_cost_usd == .5


@pytest.mark.parametrize("blocking", [False, True])
def test_defaulted_questions_do_not_remain_waiting(tmp_path, root, examples, blocking):
    from test_session_cutoff import AutoQueue, Driver, _ctx

    from factory.sessions import SessionContext
    from factory.stages.common import make_ask_human
    ctx = _ctx(tmp_path, root, examples, Driver(examples), AutoQueue(Decision.APPROVE))
    handler = make_ask_human(ctx, "writer", timeout_hours=0)
    answer = handler({"question": "Which default?", "blocking": blocking, "default_if_unanswered": "A"},
                     SessionContext("s", ctx.run_id, "specify", "wi-test"))
    assert json.loads(answer)["answer"] == "A"
    assert not ctx.store.state(ctx.run_id).open_questions


def test_new_decomposition_removes_superseded_pending_items():
    from factory.state import RunEvent, replay
    events = [RunEvent(run_id="run_test0001", seq=n, kind="artifact.produced", actor={},
                       payload={"artifact_type": "decomposition", "revision": n + 1,
                                "work_items": [{"work_item_id": wid}]}) for n, wid in enumerate(["wi-old", "wi-new"])]
    assert set(replay("run_test0001", events).work_items) == {"wi-new"}


@pytest.mark.parametrize("stop, outcome", [("terminated", None), ("end_turn", "interrupted"), ("end_turn", "failed")])
def test_interrupted_session_outputs_never_advance_the_pipeline(tmp_path, root, examples, stop, outcome):
    from test_session_cutoff import AutoQueue, _ctx, _result

    from factory.stages.common import SessionTruncated, run_session
    result = _result({"spec.json": b"{}"}, stop_reason=stop, outcome_result=outcome)
    ctx = _ctx(tmp_path, root, examples, SimpleNamespace(run=lambda **kw: result), AutoQueue(Decision.APPROVE))
    with pytest.raises(SessionTruncated, match="did not finish"):
        run_session(ctx, stage="specify", rubric_md="rubric")
    assert ctx.store.state(ctx.run_id).list_cost_usd == 0.5
    assert ctx.store.get_artifact(ctx.run_id, "specify", "unfinished1-spec.json") == b"{}"


def test_nested_evidence_downloads_and_cannot_escape_artifact_root(tmp_path, root):
    from fastapi.testclient import TestClient

    from factory.store import RunStore
    from factory.web.app import create_app
    cfg = make_config(root, tmp_path)
    store = RunStore(cfg.runs_dir)
    store.put_artifact("run_test0001", "review", "probes/nested/output.txt", b"probe output")
    (cfg.runs_dir / "run_test0001" / "private.txt").write_text("private")
    with TestClient(create_app(cfg, demo_thread=False)) as client:
        response = client.get("/api/artifacts/run_test0001/review/probes/nested/output.txt")
        assert response.status_code == 200 and response.text == "probe output"
        assert client.get("/api/artifacts/run_test0001/review/%2E%2E/%2E%2E/private.txt").status_code == 404


def test_workspace_activity_links_require_a_local_record(tmp_path):
    from factory.web.app import local_activity_url
    assert local_activity_url(tmp_path, "run_test0001") is None
    run = tmp_path / "run_test0001"
    run.mkdir()
    (run / "events.jsonl").write_text("")
    assert local_activity_url(tmp_path, "run_test0001") == "#/runs/run_test0001"
    app = tmp_path / "_apps" / "app_123456789abc"
    app.mkdir(parents=True)
    (app / "app.json").write_text("{}")
    assert local_activity_url(tmp_path, app.name) == "#/apps/app_123456789abc"
    assert local_activity_url(tmp_path, "../run_test0001") is None
