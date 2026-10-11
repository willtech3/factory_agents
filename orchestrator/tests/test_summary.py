"""The run history: status and result derived from the event log (summary.py), the Console's cached Runs page,
the controller probe behind `stopped`, and `factory runs`."""
import json

import pytest
from conftest import EXAMPLES, make_config
from fastapi.testclient import TestClient

from factory.cli import run_history_lines
from factory.runner import controller_lock
from factory.store import RunStore
from factory.summary import controller_running, summarize, summarize_run
from factory.web.app import create_app
from support.scripted_run import ScriptedRun

ACTOR = {"type": "orchestrator", "id": "test"}
HUMAN = {"type": "human", "id": "priya"}


def _run(store, rid, title="Cut churn", *, design_only=False, budget=60.0):
    store.append(rid, "run.created", ACTOR, {"outcome_id": "out_1", "title": title, "forge": "github", "budget_usd": budget,
                                              "design_only": design_only}, stage="intake")
    store.put_artifact(rid, "intake", "outcome.json", json.dumps({"outcome_id": "out_1", "title": title, "statement": "s"}).encode())
    store.append(rid, "stage.entered", ACTOR, {"stage": "decomposing"})
    store.append(rid, "session.started", {"type": "platform"}, {"session_id": "s1", "agent_key": "decomposer", "agent_id": "a", "title": "dec"}, stage="decompose")
    store.append(rid, "session.finished", {"type": "platform"}, {"session_id": "s1", "agent_id": "a", "list_cost_usd": 1.5}, stage="decompose")
    store.append(rid, "artifact.produced", ACTOR, {"artifact_type": "decomposition", "revision": 1, "work_items": [
        {"work_item_id": "wi-a", "title": "Smart retries"}, {"work_item_id": "wi-b", "title": "Dunning emails"}]})
    store.append(rid, "stage.entered", ACTOR, {"stage": "gate_1_decomposition"})


def _through_gate_2(store, rid):
    store.append(rid, "gate.opened", ACTOR, {"gate_id": "g1", "kind": "gate_1_decomposition", "artifacts": [], "assignees": [], "summary": "plan"})
    store.append(rid, "gate.decided", HUMAN, {"gate_id": "g1", "kind": "gate_1_decomposition", "decision": "approve", "comments": "", "approved_subset": [], "artifact_shas": []})
    store.append(rid, "stage.entered", ACTOR, {"stage": "specifying"})
    for wid, sid in (("wi-a", "spec_a"), ("wi-b", "spec_b")):
        for stage in ("spec_drafting", "spec_judging"):
            store.append(rid, "work_item.stage", ACTOR, {"stage": stage, "spec_id": sid}, work_item_id=wid, spec_id=sid)
    store.append(rid, "stage.entered", ACTOR, {"stage": "gate_2_spec_readiness"})
    store.append(rid, "gate.opened", ACTOR, {"gate_id": "g2", "kind": "gate_2_spec_readiness", "artifacts": [], "assignees": [], "summary": "specs"})
    store.append(rid, "gate.decided", HUMAN, {"gate_id": "g2", "kind": "gate_2_spec_readiness", "decision": "approve_subset", "comments": "ship a first",
                                               "approved_subset": ["spec_a", "spec_b"], "artifact_shas": []})
    for wid, sid in (("wi-a", "spec_a"), ("wi-b", "spec_b")):
        store.append(rid, "work_item.stage", HUMAN, {"stage": "spec_approved", "spec_id": sid}, work_item_id=wid, spec_id=sid)


def _implement(store, rid, wid, sid, n, *, merged=False, deployed=False):
    for stage in ("implementing", "reviewing", "merge_judging", "integrating"):
        store.append(rid, "work_item.stage", ACTOR, {"stage": stage}, work_item_id=wid, spec_id=sid)
    store.append(rid, "session.finished", {"type": "platform"}, {"session_id": f"impl{n}", "agent_id": "a", "list_cost_usd": 10.0}, stage="implement")
    store.append(rid, "cr.opened", ACTOR, {"id": n, "url": f"https://github.com/x/app/pull/{n}", "branch": f"b{n}", "forge": "github"}, work_item_id=wid, spec_id=sid)
    store.append(rid, "work_item.stage", ACTOR, {"stage": "cr_open", "cr_id": n}, work_item_id=wid, spec_id=sid)
    if merged or deployed:
        store.append(rid, "cr.merged", HUMAN, {"id": n, "merge_commit_sha": "c" * 40, "approved": True, "forge": "github"}, work_item_id=wid, spec_id=sid)
        store.append(rid, "work_item.stage", ACTOR, {"stage": "merged", "merge_sha": "c" * 40}, work_item_id=wid, spec_id=sid)
    if deployed:
        store.append(rid, "work_item.stage", ACTOR, {"stage": "deploying_dev"}, work_item_id=wid, spec_id=sid)
        store.append(rid, "deploy.succeeded", ACTOR, {"pipeline_id": 9, "environment_url": "https://dev.example"}, work_item_id=wid, spec_id=sid)
        store.append(rid, "work_item.stage", ACTOR, {"stage": "deployed_dev", "environment_url": "https://dev.example"}, work_item_id=wid, spec_id=sid)


@pytest.fixture
def store(tmp_path):
    return RunStore(tmp_path / "runs")


def summary_of(store, rid, alive=None):
    return summarize(store.events(rid), store.state(rid), alive)


def test_completed_run_counts_pull_requests_merges_and_dev_verification(store):
    rid = "run_done00000001"
    _run(store, rid)
    _through_gate_2(store, rid)
    store.append(rid, "stage.entered", ACTOR, {"stage": "implementing"})
    _implement(store, rid, "wi-a", "spec_a", 1, deployed=True)
    _implement(store, rid, "wi-b", "spec_b", 2)
    store.append(rid, "run.completed", ACTOR, {"specs_with_change_requests": ["spec_a", "spec_b"]})
    s = summary_of(store, rid)
    assert s["status"] == "completed" and s["status_label"] == "Done" and s["terminal"] is True and s["mode"] == "live"
    assert s["headline"] == "2 pull requests opened: 1 verified in dev, 1 open."
    assert s["counts"] == {"work_items": 2, "specs_approved": 2, "pull_requests": 2, "merged": 1, "verified_in_dev": 1, "closed": 0,
                           "decisions": 2, "answers": 0, "sessions": 3}
    prs = {p["work_item_id"]: p for p in s["pull_requests"]}
    assert prs["wi-a"] == {"work_item_id": "wi-a", "spec_id": "spec_a", "title": "Smart retries", "cr_id": 1, "url": "https://github.com/x/app/pull/1",
                           "stage": "deployed_dev", "state": "verified in dev", "merge_sha": "c" * 40, "dev_url": "https://dev.example"}
    assert prs["wi-b"]["state"] == "open" and prs["wi-b"]["dev_url"] is None
    assert [d["decision"] for d in s["decisions"]] == ["approve", "approve_subset"] and s["decisions"][1]["comments"] == "ship a first"
    assert s["decisions"][1]["by"] == "priya" and s["decisions"][1]["approved_subset"] == ["spec_a", "spec_b"]
    assert s["cost_usd"] == 21.5 and s["budget_usd"] == 60.0 and s["ended_at"] == store.events(rid)[-1].at
    assert s["work_items"] == {"total": 2, "by_stage": {"deployed_dev": 1, "cr_open": 1}}


def test_design_only_run_reports_approved_specs(store):
    rid = "run_design000001"
    _run(store, rid, design_only=True)
    _through_gate_2(store, rid)
    store.append(rid, "run.completed", ACTOR, {"design_only": True, "approved_specs": ["spec_a", "spec_b"]})
    s = summary_of(store, rid)
    assert s["mode"] == "design" and s["status"] == "completed" and s["pull_requests"] == []
    assert s["headline"] == "Design run: 2 specs approved, nothing built." and s["specs_approved"] == ["spec_a", "spec_b"]


def test_closed_at_a_gate_quotes_the_rejection(store):
    rid = "run_closed000001"
    _run(store, rid)
    store.append(rid, "gate.opened", ACTOR, {"gate_id": "g1", "kind": "gate_1_decomposition", "artifacts": [], "assignees": [], "summary": "plan"})
    store.append(rid, "gate.decided", HUMAN, {"gate_id": "g1", "kind": "gate_1_decomposition", "decision": "reject", "comments": "Not this quarter.",
                                               "approved_subset": [], "artifact_shas": []})
    store.append(rid, "stage.entered", HUMAN, {"stage": "closed"}, stage="gate_1")
    s = summary_of(store, rid)
    assert s["status"] == "closed" and s["headline"] == "Closed at Gate 1: “Not this quarter.”" and s["ended_at"] == store.events(rid)[-1].at


def test_failed_run_names_the_stage_and_the_reason(store):
    rid = "run_failed000001"
    _run(store, rid)
    _through_gate_2(store, rid)
    store.append(rid, "stage.entered", ACTOR, {"stage": "implementing"})
    store.append(rid, "session.started", {"type": "platform"}, {"session_id": "s9", "agent_key": "implementer", "agent_id": "a", "title": "impl"}, stage="implement")
    store.append(rid, "run.failed", ACTOR, {"reason": "RuntimeError: session did not produce review_response.json", "interrupted_sessions": ["s9"]})
    s = summary_of(store, rid)
    assert s["status"] == "failed" and s["failed_in"] == "implementing" and s["working"] == []
    assert s["headline"] == "Failed while building: RuntimeError: session did not produce review_response.json"


def test_escalated_and_paused_runs(store):
    rid = "run_escal0000001"
    store.append(rid, "run.created", ACTOR, {"outcome_id": "out_1", "title": "Cut churn", "budget_usd": 60.0}, stage="intake")
    store.append(rid, "stage.entered", ACTOR, {"stage": "decomposing"})
    store.append(rid, "run.escalated", ACTOR, {"reason": "decomposition failed deterministic checks twice"}, stage="decompose")
    s = summary_of(store, rid)
    assert s["status"] == "escalated" and s["headline"] == "Escalated: decomposition failed deterministic checks twice"

    rid = "run_paused000001"
    _run(store, rid)
    store.append(rid, "gate.opened", ACTOR, {"gate_id": "g1", "kind": "gate_1_decomposition", "artifacts": [], "assignees": [], "summary": "plan"})
    store.append(rid, "gate.decided", HUMAN, {"gate_id": "g1", "kind": "gate_1_decomposition", "decision": "approve", "comments": "", "approved_subset": [], "artifact_shas": []})
    store.append(rid, "stage.entered", ACTOR, {"stage": "specifying"})
    store.append(rid, "budget.reached", ACTOR, {"spent_usd": 36.2, "budget_usd": 60.0, "needed_usd": 66.2,
                                                 "next": {"stage": "review", "label": "reviewer", "sessions": 2, "cap_usd": 30.0}})
    s = summary_of(store, rid)
    assert s["status"] == "paused" and s["status_label"] == "Paused at budget" and s["terminal"] is False
    assert s["headline"] == "Paused at $36.20 of $60.00: the next step (reviewer, 2 sessions) needs $66.20."


def test_needs_you_running_and_stopped(store):
    rid = "run_gate10000001"
    _run(store, rid)
    store.append(rid, "gate.opened", ACTOR, {"gate_id": "g1", "kind": "gate_1_decomposition", "artifacts": [], "assignees": [], "summary": "plan"})
    assert summary_of(store, rid)["headline"] == "Gate 1 waiting on you."
    assert summary_of(store, rid)["status"] == "needs_you"
    # the same log with the controller known to be gone: nothing will pick up the decision
    s = summary_of(store, rid, alive=False)
    assert s["status"] == "stopped" and s["headline"] == "The controller stopped without recording why, while at Gate 1; Gate 1 is open but nothing will pick up a decision."
    assert summary_of(store, rid, alive=True)["status"] == "needs_you"

    rid = "run_running00001"
    _run(store, rid)
    store.append(rid, "gate.opened", ACTOR, {"gate_id": "g1", "kind": "gate_1_decomposition", "artifacts": [], "assignees": [], "summary": "plan"})
    store.append(rid, "gate.decided", HUMAN, {"gate_id": "g1", "kind": "gate_1_decomposition", "decision": "approve", "comments": "", "approved_subset": [], "artifact_shas": []})
    store.append(rid, "stage.entered", ACTOR, {"stage": "specifying"})
    store.append(rid, "session.started", {"type": "platform"}, {"session_id": "w1", "agent_key": "spec_writer", "agent_id": "a", "title": "spec"}, stage="specify")
    store.append(rid, "session.started", {"type": "platform"}, {"session_id": "w2", "agent_key": "spec_writer", "agent_id": "a", "title": "spec"}, stage="specify")
    s = summary_of(store, rid, alive=True)
    assert s["status"] == "running" and s["headline"] == "Writing and judging the specs; spec writer working." and s["working"] == ["spec_writer"]
    assert summary_of(store, rid, alive=False)["headline"] == "The controller stopped without recording why, while writing and judging the specs."


def test_controller_probe_reads_the_lock_without_creating_it(tmp_path):
    runs_dir = tmp_path / "runs"
    rid = "run_lock00000001"
    assert controller_running(runs_dir, rid) is None and not (runs_dir / rid).exists()
    with controller_lock(runs_dir, rid):
        assert controller_running(runs_dir, rid) is True
    assert controller_running(runs_dir, rid) is False


def test_runs_page_is_the_history_with_a_cache(tmp_path, root, monkeypatch):
    cfg = make_config(root, tmp_path / "runs", gate_surface="web")
    app = create_app(cfg)
    store = app.state.console.store
    rid = "run_done00000002"
    _run(store, rid)
    _through_gate_2(store, rid)
    store.append(rid, "stage.entered", ACTOR, {"stage": "implementing"})
    _implement(store, rid, "wi-a", "spec_a", 4)
    store.append(rid, "run.completed", ACTOR, {"specs_with_change_requests": ["spec_a"]})
    scripted = ScriptedRun(cfg.runs_dir, cfg.schemas_dir, EXAMPLES)
    live = scripted.seed_example()
    with TestClient(app) as c:
        runs = c.get("/api/runs").json()["runs"]
        assert [r["run_id"] for r in runs] == [live, rid]                      # the unfinished run first
        assert runs[0]["status"] == "needs_you" and runs[0]["waiting"] == {"gates": 0, "questions": 1}
        assert runs[1]["status"] == "completed" and runs[1]["headline"] == "1 pull request opened." and runs[1]["waiting"] == {"gates": 0, "questions": 0}
        assert runs[1]["pull_requests"][0]["url"].endswith("/pull/4") and "open_gate_ids" not in runs[1]
        # a second read of an unchanged log is served from the cache; an append invalidates it
        replays = []
        real = store.events
        monkeypatch.setattr(store, "events", lambda run_id: replays.append(run_id) or real(run_id))
        c.get("/api/runs")
        assert replays == []
        store.append(rid, "cr.closed", HUMAN, {"id": 4, "forge": "github"}, work_item_id="wi-a", spec_id="spec_a")
        store.append(rid, "work_item.stage", ACTOR, {"stage": "closed", "reason": "change request closed without merging"}, work_item_id="wi-a", spec_id="spec_a")
        c.get("/api/runs")
        assert replays == [rid]
        # the run page carries the same record
        d = c.get(f"/api/runs/{rid}").json()
        assert d["status"] == "completed" and d["summary"]["headline"] == "1 pull request opened, closed without merging." and "demo" not in d
        # a corrupt log is reported, not fatal
        (cfg.runs_dir / "run_corrupt000001").mkdir()
        (cfg.runs_dir / "run_corrupt000001" / "events.jsonl").write_text("{not json\n")
        bad = [r for r in c.get("/api/runs").json()["runs"] if r["run_id"] == "run_corrupt000001"][0]
        assert bad["status"] == "unreadable" and bad["terminal"] is True and "could not be read" in bad["headline"]


def test_factory_runs_lists_the_history(tmp_path, root):
    cfg = make_config(root, tmp_path / "runs")
    store = RunStore(cfg.runs_dir)
    assert run_history_lines(store, cfg.runs_dir) == [f"no runs under {cfg.runs_dir}"]
    rid = "run_cli000000001"
    _run(store, rid, title="Cut churn")
    store.append(rid, "run.failed", ACTOR, {"reason": "stopped by signal 15"})
    rid2 = "run_cli000000002"
    _run(store, rid2, title="Due dates")
    store.append(rid2, "gate.opened", ACTOR, {"gate_id": "g1", "kind": "gate_1_decomposition", "artifacts": [], "assignees": [], "summary": "plan"})
    lines = run_history_lines(store, cfg.runs_dir)
    assert lines[0].startswith("run              status")
    assert lines[1].startswith("run_cli000000002 Needs you        $1.50 of $60.00") and lines[1].endswith("Due dates")
    assert lines[2].strip() == "Gate 1 waiting on you."
    assert lines[3].startswith("run_cli000000001 Failed") and lines[4].strip() == "Failed while at Gate 1: stopped by signal 15"
    assert len(run_history_lines(store, cfg.runs_dir, active=True)) == 3
    assert summarize_run(store, cfg.runs_dir, rid)["status"] == "failed"


def test_legacy_demo_is_hidden_without_deleting_its_history(tmp_path, root):
    cfg = make_config(root, tmp_path / 'runs')
    app = create_app(cfg)
    store = app.state.console.store
    _run(store, 'run_real000001', title='Real work')
    _run(store, 'run_demo000001', title='Fake payment demo')
    marker = cfg.runs_dir / 'run_demo000001' / 'demo.json'
    marker.write_text('{}')
    with TestClient(app) as client:
        assert [r['run_id'] for r in client.get('/api/runs').json()['runs']] == ['run_real000001']
        assert all(r['run_id'] != 'run_demo000001' for r in client.get('/api/board').json()['runs'])
        assert client.get('/api/runs/run_demo000001').status_code == 404
    assert marker.exists() and (marker.parent / 'events.jsonl').exists()


def test_failure_stage_is_the_stage_at_failure_not_after_recovery(store):
    rid = 'run_recover0001'
    _run(store, rid)
    store.append(rid, 'stage.entered', ACTOR, {'stage': 'specifying'})
    store.append(rid, 'run.failed', ACTOR, {'reason': 'writer failed'})
    store.append(rid, 'run.resumed', ACTOR, {'stage': 'implementing'})
    assert summary_of(store, rid)['failed_in'] == 'specifying'
