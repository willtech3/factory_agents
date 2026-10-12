"""A dropped event stream is reopened; every event is handled once. No network: a fake client."""
from types import SimpleNamespace

import httpx

from conftest import make_config


class Page:
    def __init__(self, data): self.data = data; self.has_more = False


class FakeStream:
    """Yields the events given, then raises if told to (the peer closing the connection mid-stream)."""
    def __init__(self, events, then_raise=None): self.events, self.then_raise = events, then_raise
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __iter__(self):
        yield from self.events
        if self.then_raise:
            raise self.then_raise


def ev(id_, type_, **kw):
    return SimpleNamespace(id=id_, type=type_, **kw)


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
                environment_id="env_test", vault_ids=["vault_test"], system_override="Run the immutable test harness",
                model_override={"id": "claude-haiku-5-5", "effort": "low"})
    assert calls["create"]["agent"] == {"type": "agent_with_overrides", "id": "agent_i", "version": 1,
                                        "model": {"id": "claude-haiku-5-5", "effort": "low"},
                                        "system": "Run the immutable test harness"}
    assert res.stop_reason == "end_turn" and res.list_cost_usd == 1.23 and res.events_seen == 3
    assert res.last_message == "I stopped: a write was denied."
    assert calls["stream"] == 2 and calls["list"] == 2 and calls["custom"] == 0
    assert calls["create"]["environment_id"] == "env_test"
    assert calls["create"]["vault_ids"] == ["vault_test"]
    assert calls["create"]["agent"]["system"] == "Run the immutable test harness"
    assert calls["create"]["budget"]["max_list_cost"]["amount"] == "100"
