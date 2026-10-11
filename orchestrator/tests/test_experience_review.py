"""Regressions for the Factory experience review of October 11, 2026: what the Console says must match what the
factory can do and what actually happened. Each test names the finding it holds in place."""
import json
import re
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


class _Forge:
    def __init__(self, state="merged", fail=False):
        self.state, self.fail, self.calls = state, fail, 0
        self.http = SimpleNamespace(close=lambda: None)

    def change_request_state(self, cr_id):
        self.calls += 1
        if self.fail:
            raise OSError("forge unreachable")
        return {"state": self.state, "merged_at": "2026-10-11T01:00:00Z" if self.state == "merged" else None}


def test_setup_pull_request_state_follows_the_forge(tmp_path, root, monkeypatch):
    """P2: an app still said its setup pull request was ready for review after it had merged and deployed."""
    manager = AppOnboarding(make_config(root, tmp_path / "runs", gate_surface="web"))
    aid = manager.create(kind="existing", name="Evergreen", repository_url="https://github.com/example/evergreen",
                         description="Chat", budget_usd=10)["app_id"]
    manager.update(aid, status="ready_limited", pr_url="https://github.com/example/evergreen/pull/117", pr_state="open",
                   phase=onboarding_mod.PR_PHASE["open"])
    forge = _Forge(state="opened")
    monkeypatch.setattr(manager, "forge", lambda r: forge)
    still_open = manager.refresh_setup_pr(manager.get(aid))
    assert forge.calls == 1 and still_open["pr_state"] == "open"
    manager.refresh_setup_pr(manager.get(aid))
    assert forge.calls == 1                                            # re-read at most every PR_RECHECK_SECONDS

    manager._pr_checked.clear()
    forge.state = "merged"
    merged = manager.refresh_setup_pr(manager.get(aid))
    assert merged["pr_state"] == "merged" and merged["pr_merged_at"] == "2026-10-11T01:00:00Z"
    assert merged["phase"] == onboarding_mod.PR_PHASE["merged"] and merged["status"] == "ready_limited"
    manager._pr_checked.clear()
    manager.refresh_setup_pr(manager.get(aid))
    assert forge.calls == 2                                            # a settled pull request is never read again

    # an unreachable forge keeps the last known state rather than inventing one
    other = manager.create(kind="existing", name="Other", repository_url="https://github.com/example/other",
                           description="x", budget_usd=10)["app_id"]
    manager.update(other, status="ready", pr_url="https://github.com/example/other/pull/3", pr_state="open", phase="Setup pull request is ready for human review")
    monkeypatch.setattr(manager, "forge", lambda r: _Forge(fail=True))
    assert manager.refresh_setup_pr(manager.get(other))["pr_state"] == "open"


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
