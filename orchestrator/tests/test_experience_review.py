"""Regressions for the Factory experience review of October 11, 2026: what the Console says must match what the
factory can do and what actually happened. Each test names the finding it holds in place."""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_config
from fastapi.testclient import TestClient

from factory import onboarding as onboarding_mod
from factory import visual
from factory.config import AgentRef
from factory.onboarding import AppOnboarding
from factory.runner import policy_snapshot
from factory.state import Policy, replay
from factory.store import RunStore
from factory.summary import summarize
from factory.surfaces.artifact_pages import _CSS
from factory.web.app import create_app

CREW = ("codebase_researcher", "decomposer", "spec_writer", "spec_judge", "implementer", "reviewer", "merge_judge", "integrator")


@pytest.fixture
def client(tmp_path, root):
    cfg = make_config(root, tmp_path / "runs", gate_surface="web")
    with TestClient(create_app(cfg)) as c:
        c.cfg = cfg
        yield c


def test_security_review_is_offered_only_when_its_agent_is_deployed(tmp_path, root):
    """P1: the Console advertised a security pass that the installation could not run."""
    without = make_config(root, tmp_path / "a", agents={k: AgentRef("agent_" + k, 1) for k in CREW})
    with_agent = make_config(root, tmp_path / "b", agents={k: AgentRef("agent_" + k, 1) for k in (*CREW, "security_reviewer")})
    with TestClient(create_app(without)) as c:
        assert c.get("/api/config").json()["security_review_available"] is False
    with TestClient(create_app(with_agent)) as c:
        assert c.get("/api/config").json()["security_review_available"] is True
    # a run records whether the pass could happen when it started, beside the policy that asked for it
    snap = policy_snapshot(SimpleNamespace(policy=Policy(), cfg=without))
    assert snap["security_review"] is True and snap["security_review_available"] is False
    assert policy_snapshot(SimpleNamespace(policy=Policy(), cfg=with_agent))["security_review_available"] is True


def test_outcome_errors_name_each_field_in_plain_words(client):
    """P1: invalid submission showed raw schema messages with nothing tied to the field."""
    bad = {"title": "x", "statement": "Too short", "success_metrics": [{"metric": "Recovered payments", "target": ""}],
           "constraints": {"budget_usd": 150}, "owner": {"name": "", "handle": ""},
           "target_repositories": [{"url": "", "default_branch": "main", "paths_in_scope": []}]}
    r = client.post("/api/outcomes", json={"outcome": bad}).json()
    assert r["ok"] is False
    by_field = {e["field"]: e["message"] for e in r["field_errors"]}
    assert set(by_field) == {"title", "statement", "success_metrics/0/target", "owner/name", "owner/handle", "target_repositories/0/url"}
    assert len(r["field_errors"]) == len(by_field)                     # each field once, though jsonschema reports per property
    assert by_field["statement"] == "The outcome statement needs at least 50 characters; it has 9."
    assert by_field["owner/handle"] == "The owner's code host handle is required."
    assert not any("is a required property" in m or "is too short" in m for m in r["errors"])
    assert r["errors"] == [e["message"] for e in r["field_errors"]]
    assert not list((client.cfg.runs_dir / "_outcomes").glob("*.json"))  # nothing saved

    none = client.post("/api/outcomes", json={"outcome": {**bad, "success_metrics": []}}).json()
    metrics = [e for e in none["field_errors"] if e["field"].startswith("success_metrics")]
    assert metrics == [{"field": "success_metrics", "message": "Add at least one success metric with a target."}]


def test_summary_separates_agent_activity_from_pipeline_events(tmp_path):
    """P2: a healthy agent looked idle because "last activity" tracked pipeline events only, and spend left out live work."""
    store, actor = RunStore(tmp_path), {"type": "system", "id": "test"}
    rid = "run_activity0001"
    store.append(rid, "run.created", actor, {"outcome_id": "o", "title": "Activity"})
    store.append(rid, "session.started", actor, {"session_id": "s1", "agent_key": "implementer", "title": "implement"}, stage="implement")
    started = store.events(rid)[-1].at
    s = summarize(store.events(rid), replay(rid, store.events(rid)))
    assert s["working"] == ["implementer"] and s["working_since"] == started and s["live_sessions"] == 1
    store.append(rid, "session.finished", actor, {"session_id": "s1", "list_cost_usd": 2.5}, stage="implement")
    s = summarize(store.events(rid), replay(rid, store.events(rid)))
    assert s["working"] == [] and s["working_since"] is None and s["live_sessions"] == 0 and s["cost_usd"] == 2.5


def test_sessions_left_by_a_dead_controller_are_not_live_work(tmp_path):
    """Review of the fix above: a SIGKILLed controller leaves session.started without a finish, forever."""
    from factory.web.app import crew_roster
    store, actor = RunStore(tmp_path), {"type": "system", "id": "test"}
    rid = "run_orphaned0001"
    store.append(rid, "run.created", actor, {"outcome_id": "o", "title": "Orphan"})
    for stage in ("decomposing", "gate_1_decomposition", "specifying", "gate_2_spec_readiness", "implementing"):
        store.append(rid, "stage.entered", actor, {"stage": stage})
    store.append(rid, "session.started", actor, {"session_id": "old", "agent_key": "implementer", "title": "implement"}, stage="implement")
    events = store.events(rid)
    # the controller is gone (no lock holder): the run reads Stopped, and nothing in it is working
    stopped = summarize(events, replay(rid, events), controller_alive=False)
    assert stopped["status"] == "stopped" and stopped["live_sessions"] == 0 and stopped["working_since"] is None
    # a resume starts new sessions; the one its predecessor left open is no longer counted
    store.append(rid, "run.resumed", actor, {"stage": "implementing", "from": "implementing", "points": {}})
    store.append(rid, "session.started", actor, {"session_id": "new", "agent_key": "reviewer", "title": "review"}, stage="review")
    events = store.events(rid)
    resumed = summarize(events, replay(rid, events), controller_alive=True)
    assert resumed["working"] == ["reviewer"] and resumed["live_sessions"] == 1 and resumed["working_since"] == events[-1].at
    roster = {r["role"]: r for r in crew_roster(events)}
    assert roster["implementer"]["state"] == "done" and roster["reviewer"]["state"] == "working"


class _Forge:
    def __init__(self, state="merged", fail=False):
        self.state, self.fail, self.calls = state, fail, 0
        self.http = SimpleNamespace(close=lambda: None)

    def change_request_state(self, cr_id):
        self.calls += 1
        if self.fail:
            raise OSError("forge unreachable")
        return {"state": self.state, "merged_at": "2026-10-11T01:00:00Z" if self.state == "merged" else None}


def _published(manager, name="Evergreen"):
    aid = manager.create(kind="existing", name=name, repository_url=f"https://github.com/example/{name.lower()}",
                         description="Chat", budget_usd=10)["app_id"]
    manager.update(aid, status="ready_limited", pr_url=f"https://github.com/example/{name.lower()}/pull/117", pr_state="open",
                   phase=onboarding_mod.PR_PHASE["open"])
    return aid


def test_setup_pull_request_state_follows_the_forge(tmp_path, root, monkeypatch):
    """P2: an app still said its setup pull request was ready for review after it had merged and deployed."""
    manager = AppOnboarding(make_config(root, tmp_path / "runs", gate_surface="web"))
    aid = _published(manager)
    forge = _Forge(state="opened")
    monkeypatch.setattr(manager, "forge", lambda r, timeout=30.0: forge)
    assert manager.read_setup_pr(aid)["pr_state"] == "open" and forge.calls == 1

    # closed is not final: a pull request closed by mistake can be reopened, and then merged
    forge.state = "closed"
    closed = manager.read_setup_pr(aid)
    assert closed["pr_state"] == "closed" and closed["phase"] == onboarding_mod.PR_PHASE["closed"]
    forge.state = "opened"
    assert manager.read_setup_pr(aid)["pr_state"] == "open"
    forge.state = "merged"
    merged = manager.read_setup_pr(aid)
    assert merged["pr_state"] == "merged" and merged["pr_merged_at"] == "2026-10-11T01:00:00Z" and merged["status"] == "ready_limited"
    # merged says only that: whether a change run can use the setup is checked when the run starts
    assert merged["phase"] == "Setup pull request merged"

    # an unreachable forge records nothing and keeps the last known state
    other = _published(manager, "Other")
    monkeypatch.setattr(manager, "forge", lambda r, timeout=30.0: _Forge(fail=True))
    assert manager.read_setup_pr(other) is None and manager.get(other)["pr_state"] == "open"


def test_setup_pull_request_is_read_off_the_request_path(tmp_path, root, monkeypatch):
    """Review of the fix above: a synchronous forge read per app held up GET /api/apps for up to its timeout."""
    manager = AppOnboarding(make_config(root, tmp_path / "runs", gate_surface="web"))
    aid = _published(manager)
    started = []

    class Thread:            # records what would run in the background instead of running it
        def __init__(self, target, args, **kw):
            self.call = (target, args)

        def start(self):
            started.append(self.call)

    monkeypatch.setattr(onboarding_mod.threading, "Thread", Thread)
    record = manager.get(aid)
    assert manager.refresh_setup_pr(record) is record                # answers at once with the record as it is
    assert started == [(manager.read_setup_pr, (aid,))]
    manager.refresh_setup_pr(manager.get(aid))
    assert len(started) == 1                                         # at most one read per PR_RECHECK_SECONDS
    manager._pr_checked.clear()
    manager.update(aid, pr_state="merged")
    manager.refresh_setup_pr(manager.get(aid))
    assert len(started) == 1                                         # a merged pull request is never read again


def _luminance(hex_colour):
    h = hex_colour[1:] if len(hex_colour) == 7 else "".join(c * 2 for c in hex_colour[1:])     # #fff -> ffffff
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_exported_gate_pages_keep_links_and_status_readable_in_both_themes():
    """P1: the exported gate document's links were about 2.0:1 against its dark background."""
    colour = r"--([a-z]+):(#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3})\b"
    light = dict(re.findall(colour, _CSS.split("@media")[0]))
    dark = {**light, **dict(re.findall(colour, _CSS.split("prefers-color-scheme:dark")[1].split("}}")[0]))}
    assert "a{color:var(--link)}" in _CSS and "a:visited{color:var(--visited)}" in _CSS
    for theme in (light, dark):
        for name in ("link", "visited", "ok", "warn", "bad", "muted", "fg"):
            assert _contrast(theme[name], theme["bg"]) >= 4.5, (name, theme[name], theme["bg"])


def test_console_status_text_is_readable_in_both_themes():
    """Review of the fixes: inline errors and warning tags used --bad/--warn, which the dark theme never redefined."""
    css = (Path(__file__).resolve().parents[1] / "factory" / "web" / "static" / "app.css").read_text()
    colour = r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\b"
    light = dict(re.findall(colour, css.split(":root {")[1].split("}")[0]))
    dark = {**light, **dict(re.findall(colour, css.split('html[data-theme="dark"] {')[1].split("}")[0]))}
    for theme in (light, dark):
        for name in ("ok-text", "warn-text", "bad-text"):
            for ground in ("surface", "surface-2", "bg", name.replace("-text", "-soft")):
                assert _contrast(theme[name], theme[ground]) >= 4.5, (name, ground, theme[name], theme[ground])
    assert not re.search(r"(?<![-\w])color: var\(--(ok|warn|bad)\)", css)    # fills may use them; text may not


def test_console_script_parses_on_older_safari():
    """Review of the fixes: a regex lookbehind is an early SyntaxError before Safari 16.4, blanking every page."""
    js = (Path(__file__).resolve().parents[1] / "factory" / "web" / "static" / "app.js").read_text()
    assert "(?<=" not in js and "(?<!" not in js


def test_outcome_list_without_runs_reads_no_run_log(client, monkeypatch):
    """Review of the fixes: prefilling the owner read every run's event log through /api/outcomes."""
    good = {"title": "Recover payments", "statement": "Customers whose card fails are retried and told, so fewer of them churn.",
            "success_metrics": [{"metric": "Recovered payments", "target": "60%"}], "constraints": {"budget_usd": 60},
            "owner": {"name": "Will", "handle": "willtech3"}, "target_repositories": [{"url": "https://github.com/x/y", "default_branch": "main"}]}
    assert client.post("/api/outcomes", json={"outcome": good}).json()["ok"]
    RunStore(client.cfg.runs_dir).append("run_reads00000001", "run.created", {"type": "system", "id": "t"}, {"outcome_id": "o", "title": "t"})
    monkeypatch.setattr(RunStore, "events", lambda *a: pytest.fail("read a run log"))
    listed = client.get("/api/outcomes?runs=false").json()["outcomes"]
    assert listed[0]["owner"] == "willtech3" and listed[0]["owner_name"] == "Will" and listed[0]["runs"] == []


def test_browser_evidence_says_who_wrote_its_summary(tmp_path):
    """P1: a worker's guess (the provider key) headed a check whose recording showed an origin refusal."""
    request = {"id": "req1", "head_sha": "abc", "steps": ["Send a message"]}
    missing = tmp_path / "missing"
    missing.mkdir()
    refused = visual.collect(missing, request, None)
    assert refused["status"] == "blocked" and refused["summary_source"] == "controller"

    reported = tmp_path / "reported"
    reported.mkdir()
    (reported / "result.json").write_text(json.dumps({
        "request_id": "req1", "head_sha": "abc", "status": "failed", "summary": "The provider key is probably missing.",
        "steps": [{"status": "failed", "observation": "Invalid request origin (403) from /api/chat"}]}))
    worker = visual.collect(reported, request, 1)
    assert worker["status"] == "failed" and worker["summary_source"] == "worker"
    assert worker["steps_observed"][0]["observation"].startswith("Invalid request origin")
