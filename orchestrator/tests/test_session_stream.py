"""A dropped event stream is reopened; every event is handled once. No network: a fake client."""
from types import SimpleNamespace

import httpx

from conftest import make_config
from support.sessions import FakeStream, Page, ev


def test_stream_drop_is_reopened_and_events_are_not_handled_twice(tmp_path, root, monkeypatch):
    from factory.sessions import SessionDriver
    import factory.sessions as sessions
    monkeypatch.setattr(sessions.time, "sleep", lambda s: None)

    idle = ev("e3", "session.status_idle", stop_reason=SimpleNamespace(type="end_turn"))
    msg = ev("e2", "agent.message", content=[SimpleNamespace(type="text", text="I stopped: a write was denied.")])
    streams = iter([
        FakeStream([msg], then_raise=httpx.RemoteProtocolError("peer closed connection without sending complete message body")),
        FakeStream([idle]),
    ])
    listed = [[ev("e1", "agent.thinking")], [ev("e1", "agent.thinking"), msg]]       # the catch-up after the reconnect repeats e1 and e2
    calls = {"stream": 0, "list": 0, "custom": 0}

    class Events:
        def stream(self, session_id): calls["stream"] += 1; return next(streams)
        def list(self, session_id, **kw): calls["list"] += 1; return Page(listed[min(calls["list"] - 1, 1)])
        def send(self, **kw): calls["custom"] += 1
    class Sessions:
        events = Events()
        def create(self, **kw): calls["create"] = kw; return SimpleNamespace(id="sesn_test")
        def retrieve(self, **kw): return SimpleNamespace(status="idle", usage=SimpleNamespace(list_cost=SimpleNamespace(amount="123")))
    class Files:
        def upload(self, **kw): return SimpleNamespace(id="file_x")
        def list(self, **kw): return Page([])
    class Client:
        beta = SimpleNamespace(sessions=Sessions(), files=Files())

    cfg = make_config(root, tmp_path, environment_id="env_1")
    from factory.config import AgentRef
    cfg = type(cfg)(**{**cfg.__dict__, "agents": {"implementer": AgentRef("agent_i", 1)}})
    d = SessionDriver(cfg, client=Client())
    res = d.run(agent_key="implementer", title="t", run_id="run_x", stage="implement", uploads={}, description="d", rubric_md=None,
                max_iterations=1, budget_usd=1.0, repository={"type": "github_repository", "url": "https://github.com/example/test"},
                environment_id="env_test", vault_ids=["vault_test"], system_override="Run the immutable test harness")
    assert res.stop_reason == "end_turn" and res.list_cost_usd == 1.23 and res.events_seen == 3
    assert res.last_message == "I stopped: a write was denied."
    assert calls["stream"] == 2 and calls["list"] == 2 and calls["custom"] == 0
    assert calls["create"]["environment_id"] == "env_test"
    assert calls["create"]["vault_ids"] == ["vault_test"]
    assert calls["create"]["agent"]["system"] == "Run the immutable test harness"
    assert calls["create"]["budget"]["max_list_cost"]["amount"] == "100"


def test_a_tool_call_answered_before_a_drop_is_not_answered_again(tmp_path, root, monkeypatch):
    """ask_human is answered on the first stream, which then drops. The catch-up listing after the reconnect repeats the
    call before its result is listed, and the reopened stream repeats it again: the owner is asked once and exactly one
    result is sent (each repeat would re-send the cached answer)."""
    from factory.sessions import SessionDriver
    import factory.sessions as sessions
    monkeypatch.setattr(sessions.time, "sleep", lambda s: None)

    idle = ev("e3", "session.status_idle", stop_reason=SimpleNamespace(type="end_turn"))
    msg = ev("e2", "agent.message", content=[SimpleNamespace(type="text", text="Which zone should reports use?")])
    ask = ev("e4", "agent.custom_tool_use", name="ask_human", input={"question": "Which zone?"})
    streams = iter([
        FakeStream([msg, ask], then_raise=httpx.RemoteProtocolError("peer closed connection without sending complete message body")),
        FakeStream([ask, idle]),
    ])
    listed = [[ev("e1", "agent.thinking")], [ev("e1", "agent.thinking"), msg, ask]]   # no user.custom_tool_result listed yet
    calls = {"list": 0, "sent": [], "asked": 0}

    def ask_human(tool_input, sctx):
        calls["asked"] += 1
        return "UTC"

    class Events:
        def stream(self, session_id): return next(streams)
        def list(self, session_id, **kw): calls["list"] += 1; return Page(listed[min(calls["list"] - 1, 1)])
        def send(self, **kw): calls["sent"].append(kw["events"][0])
    class Sessions:
        events = Events()
        def create(self, **kw): return SimpleNamespace(id="sesn_test")
        def retrieve(self, **kw): return SimpleNamespace(status="idle", usage=SimpleNamespace(list_cost=SimpleNamespace(amount="0")))
    class Client:
        beta = SimpleNamespace(sessions=Sessions(), files=SimpleNamespace(upload=lambda **kw: SimpleNamespace(id="file_x"),
                                                                          list=lambda **kw: Page([])))

    cfg = make_config(root, tmp_path, environment_id="env_1")
    from factory.config import AgentRef
    cfg = type(cfg)(**{**cfg.__dict__, "agents": {"implementer": AgentRef("agent_i", 1)}})
    res = SessionDriver(cfg, client=Client()).run(
        agent_key="implementer", title="t", run_id="run_x", stage="implement", uploads={}, description="d", rubric_md=None,
        max_iterations=1, budget_usd=1.0, repository={"type": "github_repository", "url": "https://github.com/example/test"},
        custom_tools={"ask_human": ask_human})
    assert res.stop_reason == "end_turn" and res.events_seen == 4
    assert calls["asked"] == 1 and [e["custom_tool_use_id"] for e in calls["sent"]] == ["e4"]
