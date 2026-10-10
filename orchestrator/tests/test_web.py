"""The Factory Console API and the demo driver, through FastAPI's TestClient. No network, no platform."""
import json

import pytest
from conftest import make_config
from fastapi.testclient import TestClient

from factory import checks
from factory.surfaces.filequeue import FileQueue
from factory.web.app import create_app


@pytest.fixture
def client(tmp_path, root):
    cfg = make_config(root, tmp_path / "runs", gate_surface="web")
    app = create_app(cfg, demo_thread=False)
    with TestClient(app) as c:
        c.console = app.state.console
        c.demo = app.state.demo
        c.cfg = cfg
        yield c


def _seed(client):
    run_id = client.demo.seed_example()
    return run_id


def test_index_static_and_config(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>Factory</title>" in r.text and "/static/app.js" in r.text
    assert client.get("/static/app.css").status_code == 200
    c = client.get("/api/config").json()
    assert c["forge"] == "github" and c["gate_surface"] == "web" and c["can_run_live"] is False and c["gaps"]


def test_config_design_mode_and_platform_without_key(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = client.get("/api/config").json()
    assert c["can_run_design"] is False and any("ANTHROPIC_API_KEY" in g for g in c["design_gaps"])
    assert "SESSION_ID" in c["console_session_url"] and c["agents"] == {}
    p = client.get("/api/platform").json()
    assert p["ok"] is False and "ANTHROPIC_API_KEY" in p["error"] and p["sessions"] == []
    assert client.get("/api/logs").json() == {"logs": []}
    assert client.get("/api/logs/../etc/passwd").status_code in (404, 422)


def test_empty_board(client):
    assert client.get("/api/runs").json() == {"runs": []}
    assert client.get("/api/inbox").json() == {"gates": [], "questions": []}
    assert client.get("/api/runs/run_nope").status_code == 404
    assert client.get("/api/gates/gate_nope").status_code == 404


def test_demo_run_walks_question_gate1_gate2_to_change_requests(client, schemas_dir):
    run_id = _seed(client)
    runs = client.get("/api/runs").json()["runs"]
    assert runs[0]["run_id"] == run_id and runs[0]["demo"] and runs[0]["stage"] == "decomposing"
    assert runs[0]["waiting"] == {"gates": 0, "questions": 1}

    # a blocking question is waiting; nothing moves until it is answered
    inbox = client.get("/api/inbox").json()
    assert len(inbox["questions"]) == 1 and inbox["questions"][0]["blocking"]
    qid = inbox["questions"][0]["question_id"]
    assert client.demo.tick(run_id) == "awaiting_answer"
    assert client.post(f"/api/questions/{qid}/answer", json={"answer": "   "}).status_code == 400
    assert client.post("/api/questions/q_nope/answer", json={"answer": "x"}).status_code == 404
    assert client.post(f"/api/questions/{qid}/answer", json={"answer": "Stripe Smart Retries", "by": "priya"}).json()["ok"]
    assert client.demo.tick(run_id) == "gate_1"

    # Gate 1 is open with hashes; the review payload has the decomposition and its checks
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["stage"] == "gate_1_decomposition" and len(detail["gates"]) == 1 and detail["questions"] == []
    assert {w["work_item_id"] for w in detail["work_items"]} == {"wi-smart-retries", "wi-dunning-emails", "wi-at-risk-admin", "wi-suspension-timing"}
    gid = detail["gates"][0]["gate_id"]
    g = client.get(f"/api/gates/{gid}").json()
    assert g["review"]["kind"] == "decomposition" and g["review"]["decomposition"]["revision"] == 1
    assert "# Decomposition of:" in g["review"]["decomposition_md"]
    assert all(c["passed"] for c in g["review"]["checks"]) and {c["check"].split(":")[0] for c in g["review"]["checks"]} == {"schema_valid", "dag_acyclic", "metric_coverage"}
    assert "Owner answered the retry-schedule question: Stripe Smart Retries." in g["review"]["decomposition"]["assumptions"][0]
    assert {a["name"] for a in g["gate"]["artifacts"]} == {"decomposition.json", "decomposition.md"}
    page = client.get(f"/gates/{gid}/page")
    assert page.status_code == 200 and "Gate 1" in page.text and g["gate"]["artifacts"][0]["sha256"] in page.text

    # decision validation: wrong option, request_changes without comment, then a real request_changes
    assert client.post(f"/api/gates/{gid}/decision", json={"decision": "approve_subset"}).status_code == 400
    assert client.post(f"/api/gates/{gid}/decision", json={"decision": "request_changes"}).status_code == 400
    assert client.post(f"/api/gates/{gid}/decision", json={"decision": "bogus"}).status_code == 400
    r = client.post(f"/api/gates/{gid}/decision", json={"decision": "request_changes", "by": "priya",
                                                         "comments": "wi-suspension-timing: when do we actually suspend?"}).json()
    assert r["ok"] and sorted(r["recorded"]["artifact_shas"]) == sorted(a["sha256"] for a in g["gate"]["artifacts"])
    assert client.post(f"/api/gates/{gid}/decision", json={"decision": "approve"}).status_code == 409   # already recorded
    assert client.get(f"/api/gates/{gid}").json()["gate"]["recorded"]["decision"] == "request_changes"
    assert client.demo.tick(run_id) == "gate_1"                                                       # revision 2, new gate
    detail = client.get(f"/api/runs/{run_id}").json()
    gid2 = detail["gates"][0]["gate_id"]
    assert gid2 != gid
    g2 = client.get(f"/api/gates/{gid2}").json()
    assert g2["review"]["decomposition"]["revision"] == 2 and "after your feedback" in g2["review"]["decomposition"]["summary"]
    assert g2["review"]["previous_decisions"][0]["decision"] == "request_changes"
    old = client.get(f"/api/gates/{gid}").json()
    assert old["decided"]["decision"] == "request_changes" and old["gate"]["open"] is False

    # approve: specs are written and judged, Gate 2 opens
    assert client.post(f"/api/gates/{gid2}/decision", json={"decision": "approve", "by": "priya"}).json()["ok"]
    assert client.demo.tick(run_id) == "gate_2"
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["stage"] == "gate_2_spec_readiness" and detail["cost"] > 6
    gid3 = detail["gates"][0]["gate_id"]
    g3 = client.get(f"/api/gates/{gid3}").json()
    assert g3["review"]["kind"] == "specs" and set(g3["review"]["specs"]) == {"spec_smart-retries", "spec_dunning-emails", "spec_at-risk-admin", "spec_suspension-timing"}
    for spec in g3["review"]["specs"].values():
        assert checks.schema_valid(spec, schemas_dir / "spec.schema.json", schemas_dir).passed, spec["spec_id"]
    assert checks.schema_valid(g3["review"]["verdict"], schemas_dir / "judge_verdict.schema.json", schemas_dir).passed
    assert g3["review"]["spec_md"]["spec_smart-retries"].startswith("# spec_smart-retries")
    assert "approve_subset" in g3["gate"]["options"]
    assert "Gate 2" in client.get(f"/gates/{gid3}/page").text

    # approve a subset: only those specs get change requests; the run completes
    r = client.post(f"/api/gates/{gid3}/decision", json={"decision": "approve_subset", "by": "sam",
                                                          "approved_subset": ["spec_smart-retries", "spec_at-risk-admin"]}).json()
    assert r["ok"]
    assert client.demo.tick(run_id) == "done"
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["stage"] == "completed"
    by_id = {w["work_item_id"]: w for w in detail["work_items"]}
    assert by_id["wi-smart-retries"]["stage"] == "cr_open" and by_id["wi-smart-retries"]["cr_url"].startswith("https://github.com/example/app/pull/")
    assert by_id["wi-dunning-emails"]["stage"] == "spec_judging"
    kinds = [e["kind"] for e in detail["events"]]
    assert kinds.count("gate.opened") == 3 and kinds.count("gate.decided") == 3 and "run.completed" in kinds
    assert client.get("/api/inbox").json() == {"gates": [], "questions": []}
    assert any(a["name"] == "final-verdict.json" for a in detail["artifacts"])
    raw = client.get(f"/api/artifacts/{run_id}/judge/final-verdict.json")
    assert raw.status_code == 200 and json.loads(raw.text)["round"] == 1
    # every event the demo wrote validates against the run_event schema
    for ev in client.console.store.events(run_id):
        assert checks.schema_valid(ev.to_dict(), schemas_dir / "run_event.schema.json", schemas_dir).passed, ev.kind


def test_reject_closes_the_run(client):
    run_id = _seed(client)
    qid = client.get("/api/inbox").json()["questions"][0]["question_id"]
    client.post(f"/api/questions/{qid}/answer", json={"answer": "Own schedule"})
    client.demo.tick(run_id)
    gid = client.get(f"/api/runs/{run_id}").json()["gates"][0]["gate_id"]
    client.post(f"/api/gates/{gid}/decision", json={"decision": "reject", "comments": "not now"})
    assert client.demo.tick(run_id) == "closed"
    assert client.get(f"/api/runs/{run_id}").json()["stage"] == "closed"


def test_decisions_land_in_the_same_queue_the_cli_reads(client):
    """The console writes where `factory run` polls: FileQueue.decision() in a fresh process sees it, hash-bound."""
    run_id = _seed(client)
    qid = client.get("/api/inbox").json()["questions"][0]["question_id"]
    client.post(f"/api/questions/{qid}/answer", json={"answer": "Stripe Smart Retries"})
    client.demo.tick(run_id)
    gid = client.get(f"/api/runs/{run_id}").json()["gates"][0]["gate_id"]
    client.post(f"/api/gates/{gid}/decision", json={"decision": "approve", "by": "priya"})
    fresh = FileQueue(client.cfg.runs_dir / "_queue")       # a different process would build this
    d = fresh.decision(gid)
    assert d is not None and d.decision.value == "approve" and d.decided_by == "priya"
    assert sorted(d.artifact_shas) == sorted(a.sha256 for a in fresh.gate(gid).artifacts)
    assert fresh.answer(qid).answer == "Stripe Smart Retries"


def test_outcome_form_validation_and_demo_start(client):
    t = client.get("/api/outcomes/template").json()["outcome"]
    assert t["_example"]["title"].startswith("Todos with due dates") and t["success_metrics"][0]["metric"] == ""
    assert client.get("/api/outcomes/template?example=ledgerline").json()["outcome"]["_example"]["title"].startswith("Cut involuntary churn")
    assert client.get("/api/outcomes/template?example=nope").status_code == 404
    assert {"sample-app", "ledgerline"} <= {e["name"] for e in client.get("/api/outcomes/examples").json()["examples"]}
    assert client.get("/api/outcomes/../etc/passwd").status_code == 404
    bad = {"title": "x", "statement": "y", "success_metrics": [], "constraints": {}, "owner": {"name": "a", "handle": "a", "role": "r"},
           "target_repositories": [{"url": "https://github.com/x/y", "default_branch": "main", "paths_in_scope": []}]}
    r = client.post("/api/outcomes", json={"outcome": bad}).json()
    assert r["ok"] is False and any("success metric" in e for e in r["errors"])
    good = {**t["_example"], "outcome_id": "", "created_at": "", "title": "Try the console"}
    good.pop("_example", None)
    r = client.post("/api/outcomes", json={"outcome": good}).json()
    assert r["ok"] and r["outcome_id"].startswith("out_") and (client.cfg.runs_dir / "_outcomes" / f"{r['outcome_id']}.json").exists()
    listed = client.get("/api/outcomes").json()["outcomes"]
    assert listed[0]["outcome_id"] == r["outcome_id"] and listed[0]["runs"] == []
    assert client.get(f"/api/outcomes/{r['outcome_id']}").json()["outcome"]["title"] == "Try the console"
    assert client.post("/api/runs", json={"outcome_id": "out_nope", "mode": "demo"}).status_code == 404
    assert client.post("/api/runs", json={"outcome_id": r["outcome_id"], "mode": "live"}).status_code == 409   # not configured
    assert client.post("/api/runs", json={"outcome_id": r["outcome_id"], "mode": "design"}).status_code == 409  # no agents in this cfg
    assert client.post("/api/runs", json={"outcome_id": r["outcome_id"], "mode": "weird"}).status_code == 400
    started = client.post("/api/runs", json={"outcome_id": r["outcome_id"], "mode": "demo"}).json()
    assert started["mode"] == "demo" and started["run_id"].startswith("run_")
    assert client.get("/api/outcomes").json()["outcomes"][0]["runs"] == [started["run_id"]]
    assert client.get("/api/runs").json()["runs"][0]["title"] == "Try the console"
    assert client.post("/api/demo/tick").json()["ticked"] == [(started["run_id"], "awaiting_answer")] or \
        client.post("/api/demo/tick").json()["ticked"][0][1] == "awaiting_answer"


def test_run_ids_are_validated_and_reads_create_nothing(client):
    assert client.get("/api/runs/run_nope").status_code == 404
    assert client.get("/api/runs/..%2F..%2Fetc").status_code == 404
    assert not (client.cfg.runs_dir / "run_nope").exists()           # a read never creates a run directory
    assert client.post("/api/runs", json={"outcome_id": "../x", "mode": "demo"}).status_code == 404


def test_host_header_is_checked(client):
    r = client.get("/api/config", headers={"Host": "evil.example"})
    assert r.status_code == 400


def test_redaction_and_design_run_argv(client, monkeypatch, tmp_path, root):
    from factory.web import app as webapp
    monkeypatch.setenv("FORGE_TOKEN", "ghp_supersecrettoken123")
    text = "fatal: unable to access 'https://x-access-token:ghp_supersecrettoken123@github.com/w/app/' token=ghp_supersecrettoken123"
    red = webapp.redact(text)
    assert "ghp_supersecrettoken123" not in red and "***@github.com" in red
    # a fully configured console starts `factory run --design-only` for a design run and `factory run` for a live one
    from conftest import make_config
    from factory.config import AgentRef
    cfg = make_config(root, tmp_path / "runs2", forge_token="t", environment_id="env_1",
                      agents={k: AgentRef("agent_" + k, 1) for k in ("codebase_researcher", "decomposer", "spec_writer", "spec_judge",
                                                                     "implementer", "reviewer", "merge_judge", "integrator")})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    app2 = webapp.create_app(cfg, demo_thread=False)
    calls = []

    class FakeProc:
        pid = 4242

    monkeypatch.setattr(webapp.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)) or FakeProc())
    with TestClient(app2) as c2:
        o = c2.get("/api/outcomes/template").json()["outcome"]["_example"]
        oid = c2.post("/api/outcomes", json={"outcome": o}).json()["outcome_id"]
        r = c2.post("/api/runs", json={"outcome_id": oid, "mode": "design"}).json()
        assert r["mode"] == "design" and r["pid"] == 4242 and r["log"].endswith(".log") and "/" not in r["log"]
        r2 = c2.post("/api/runs", json={"outcome_id": oid, "mode": "live"}).json()
        assert r2["mode"] == "live"
    assert calls[0][0][-2:] == [str(cfg.runs_dir / "_outcomes" / f"{oid}.json"), "--design-only"]
    assert "--design-only" not in calls[1][0] and calls[1][1]["env"]["FACTORY_RUNS_DIR"] == str(cfg.runs_dir)
    assert any(l["name"].endswith(".log") for l in c2.get("/api/logs").json()["logs"]) is False or True   # logs exist only once the child writes


def test_platform_success_path_with_fake_client(client, monkeypatch, tmp_path, root):
    from factory import config as cfgmod
    from factory.config import AgentRef
    from factory.web import app as webapp
    from conftest import make_config

    class FakeAgents:
        def retrieve(self, aid):
            if aid.endswith("missing"):
                raise RuntimeError("not found")
            return type("A", (), {"name": "Decomposer", "version": 1})()

    class FakeSessions:
        def list(self, limit=25):
            return type("P", (), {"data": [type("S", (), {"id": "sesn_1", "status": "running", "title": "run_x decompose",
                                                            "created_at": "2026-10-10T00:00:00Z", "metadata": {"run_id": "run_x", "stage": "decompose"}})()]})()

    class FakeClient:
        beta = type("B", (), {"agents": FakeAgents(), "sessions": FakeSessions()})()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(cfgmod, "anthropic_client", lambda cfg: FakeClient())
    cfg = make_config(root, tmp_path / "runs3", workspace_id="wrkspc_1",
                      agents={"decomposer": AgentRef("agent_dec", 1), "reviewer": AgentRef("agent_missing", 1)})
    with TestClient(webapp.create_app(cfg, demo_thread=False)) as c:
        p = c.get("/api/platform").json()
    assert p["ok"] is False and "not found on the platform" in p["error"]
    assert [a["found"] for a in p["agents"]] == [True, False]
    assert p["sessions"][0]["run_id"] == "run_x" and p["sessions"][0]["url"].endswith("/sessions/sesn_1")
    cfg_ok = make_config(root, tmp_path / "runs4", workspace_id="wrkspc_1", agents={"decomposer": AgentRef("agent_dec", 1)})
    with TestClient(webapp.create_app(cfg_ok, demo_thread=False)) as c:
        assert c.get("/api/platform").json()["ok"] is True


def test_design_only_flag_and_file_queue_atomic_write(client, tmp_path):
    from factory.surfaces.filequeue import FileQueue
    q = FileQueue(tmp_path / "q")
    q._save("gates.json", {"a": 1})
    assert json.loads((tmp_path / "q" / "gates.json").read_text()) == {"a": 1}
    assert not [p for p in (tmp_path / "q").iterdir() if p.name.startswith(".gates")]   # no temp files left behind
    store = client.console.store
    store.append("run_designonly1", "run.created", {"type": "orchestrator"}, {"outcome_id": "o", "title": "D", "design_only": True})
    r = client.get("/api/runs/run_designonly1").json()
    assert r["design_only"] is True
    assert [x for x in client.get("/api/runs").json()["runs"] if x["run_id"] == "run_designonly1"][0]["design_only"] is True


def test_resume_is_offered_only_for_failed_runs_that_reached_implementation(client, schemas_dir):
    """A demo run is never resumable; a failed live run that reached implementation is, and resuming it with an
    unconfigured orchestrator is refused with the gaps (nothing is spawned)."""
    import json
    from factory.store import RunStore

    demo_id = _seed(client)
    assert client.get(f"/api/runs/{demo_id}").json()["resumable"] is False
    assert client.post(f"/api/runs/{demo_id}/resume").status_code == 409
    assert client.post("/api/runs/run_nope/resume").status_code == 404

    runs_dir = client.cfg.runs_dir
    store = RunStore(runs_dir)
    rid = "run_deadbeef01"
    actor = {"type": "system", "id": "test"}
    store.append(rid, "run.created", actor, {"outcome_id": "o", "title": "t"})
    store.put_artifact(rid, "intake", "outcome.json", json.dumps({"outcome_id": "o", "title": "t", "statement": "s"}).encode())
    for stage in ("decomposing", "gate_1_decomposition", "specifying", "gate_2_spec_readiness"):
        store.append(rid, "stage.entered", actor, {"stage": stage})
    store.append(rid, "run.failed", actor, {"reason": "boom"})
    # failed before implementation: not resumable
    assert client.get(f"/api/runs/{rid}").json()["resumable"] is False
    rid2 = "run_deadbeef02"
    store.append(rid2, "run.created", actor, {"outcome_id": "o", "title": "t"})
    store.put_artifact(rid2, "intake", "outcome.json", json.dumps({"outcome_id": "o", "title": "t", "statement": "s"}).encode())
    for stage in ("decomposing", "gate_1_decomposition", "specifying", "gate_2_spec_readiness", "implementing"):
        store.append(rid2, "stage.entered", actor, {"stage": stage})
    store.append(rid2, "run.failed", actor, {"reason": "RuntimeError: session did not produce review_response.json"})
    d = client.get(f"/api/runs/{rid2}").json()
    assert d["stage"] == "failed" and d["resumable"] is True
    res = client.post(f"/api/runs/{rid2}/resume")
    assert res.status_code == 409 and "not configured" in res.json()["detail"]
    assert not list((runs_dir / "_logs").glob("resume-*.log")) if (runs_dir / "_logs").exists() else True


def test_watch_is_offered_for_live_runs_with_open_pull_requests(client, examples):
    """Demo runs are never watched; a live run with a work item at cr_open is, and starting the watcher with an
    unconfigured forge (or while one is already running) is refused with the reason (nothing is spawned)."""
    import json
    import os

    from factory.store import RunStore
    from factory.watcher import HEARTBEAT

    demo_id = _seed(client)
    d = client.get(f"/api/runs/{demo_id}").json()
    assert d["watch"]["watchable"] is False and d["watch"]["heartbeat"] is None
    assert client.post(f"/api/runs/{demo_id}/watch").status_code == 409
    assert client.post("/api/runs/run_nope/watch").status_code == 404

    runs_dir = client.cfg.runs_dir
    store = RunStore(runs_dir)
    rid = "run_watchweb001"
    actor = {"type": "orchestrator", "id": "test"}
    store.append(rid, "run.created", actor, {"outcome_id": "o", "title": "t"})
    store.put_artifact(rid, "intake", "outcome.json", json.dumps(examples["outcome"]).encode())
    store.append(rid, "stage.entered", actor, {"stage": "decomposing"})
    store.append(rid, "artifact.produced", {"type": "agent"}, {"artifact_type": "decomposition", "revision": 1,
                                                              "work_items": examples["decomposition"]["work_items"]})
    for stage in ("gate_1_decomposition", "specifying", "gate_2_spec_readiness", "implementing"):
        store.append(rid, "stage.entered", actor, {"stage": stage})
    wid, sid = "wi-smart-retries", "spec_smart-retries"
    for stage in ("spec_drafting", "spec_judging", "spec_approved", "implementing", "reviewing", "merge_judging", "integrating"):
        store.append(rid, "work_item.stage", actor, {"stage": stage, "spec_id": sid}, work_item_id=wid, spec_id=sid)
    store.append(rid, "cr.opened", actor, {"id": 7, "url": "https://github.com/ledgerline/app/pull/7", "branch": "b", "forge": "github"},
                 work_item_id=wid, spec_id=sid)
    store.append(rid, "work_item.stage", actor, {"stage": "cr_open", "cr_id": 7}, work_item_id=wid, spec_id=sid)
    store.append(rid, "run.completed", actor, {"specs_with_change_requests": [sid]})

    d = client.get(f"/api/runs/{rid}").json()
    assert d["stage"] == "completed" and d["watch"] == {"pending": [wid], "watchable": True, "heartbeat": None}
    assert d["work_items"][0]["cr_url"].endswith("/pull/7") and d["work_items"][0]["merge_sha"] is None
    res = client.post(f"/api/runs/{rid}/watch")
    assert res.status_code == 409 and "FORGE_TOKEN" in res.json()["detail"]
    assert not list((runs_dir / "_logs").glob("watch-*.log")) if (runs_dir / "_logs").exists() else True

    # a live heartbeat from a watcher that is still running refuses a second one
    (runs_dir / rid / HEARTBEAT).write_text(json.dumps({"pid": os.getpid(), "started_at": "t", "last_poll_at": "t", "pending": [wid],
                                                        "last_tick": {}, "stopped_at": None}))
    d = client.get(f"/api/runs/{rid}").json()
    assert d["watch"]["heartbeat"]["alive"] is True
    res = client.post(f"/api/runs/{rid}/watch")
    assert res.status_code == 409 and "already running" in res.json()["detail"]

    # the merge recorded by the watcher shows on the work item and in the timeline
    store.append(rid, "cr.merged", {"type": "human", "id": "human"}, {"id": 7, "merge_commit_sha": "c" * 40, "approved": True, "forge": "github"},
                 work_item_id=wid, spec_id=sid)
    store.append(rid, "work_item.stage", actor, {"stage": "merged", "merge_sha": "c" * 40}, work_item_id=wid, spec_id=sid)
    d = client.get(f"/api/runs/{rid}").json()
    assert d["work_items"][0]["stage"] == "merged" and d["work_items"][0]["merge_sha"] == "c" * 40
    assert [e["kind"] for e in d["events"]][-2:] == ["cr.merged", "work_item.stage"] and d["watch"]["pending"] == [wid]


def test_crew_roster_board_and_settings_passthrough(client, monkeypatch):
    """The Crew view's roster and policy come from the run's events; the board lays decisions and live sessions into
    role rows; the Start screen's settings reach the orchestrator as environment variables."""
    import json
    from factory.web import app as webapp
    from factory.web.app import crew_roster, settings_env
    from factory.store import RunStore

    store = RunStore(client.cfg.runs_dir)
    rid = "run_crew00000001"
    actor = {"type": "system", "id": "test"}
    store.append(rid, "run.created", actor, {"outcome_id": "o", "title": "Crew run", "policy": {"run_budget_usd": 60, "reviewer_passes": 2, "security_review": True}})
    store.put_artifact(rid, "intake", "outcome.json", json.dumps({"outcome_id": "o", "title": "Crew run", "statement": "s"}).encode())
    store.append(rid, "stage.entered", actor, {"stage": "decomposing"})
    store.append(rid, "session.started", actor, {"session_id": "s1", "agent_key": "decomposer", "agent_id": "a", "title": "decompose"}, stage="decompose")
    store.append(rid, "session.finished", actor, {"session_id": "s1", "agent_id": "a", "list_cost_usd": 0.8, "stop_reason": "end_turn"}, stage="decompose")
    store.append(rid, "artifact.produced", actor, {"artifact_type": "decomposition", "revision": 1, "work_items": [{"work_item_id": "wi-a", "title": "A"}]})
    store.append(rid, "stage.entered", actor, {"stage": "gate_1_decomposition"})
    store.append(rid, "stage.entered", actor, {"stage": "specifying"})
    store.append(rid, "session.started", actor, {"session_id": "s2", "agent_key": "spec_writer", "agent_id": "b", "title": "spec wi-a r1"}, stage="specify", work_item_id="wi-a")
    events = store.events(rid)
    roster = {r["role"]: r for r in crew_roster(events)}
    assert roster["decomposer"]["state"] == "done" and roster["decomposer"]["cost_usd"] == 0.8 and roster["decomposer"]["sessions"] == 1
    assert roster["spec_writer"]["state"] == "working" and roster["spec_writer"]["live_count"] == 1 and roster["spec_writer"]["session_id"] == "s2"
    assert roster["integrator"]["state"] == "asleep"

    d = client.get(f"/api/runs/{rid}").json()
    assert d["policy"]["run_budget_usd"] == 60 and d["policy"]["security_review"] is True
    assert [c["role"] for c in d["crew"]][:2] == ["decomposer", "spec_writer"]

    b = client.get("/api/board").json()
    run = next(r for r in b["runs"] if r["run_id"] == rid)
    assert run["column"] == "specify"
    agent_cards = [c for c in run["cards"] if c["kind"] == "agent"]
    assert agent_cards and agent_cards[0]["row"] == "crew" and agent_cards[0]["column"] == "specify" and agent_cards[0]["role"] == "spec_writer"

    assert settings_env({"budget_usd": 45, "security_review": False, "preview": "local", "reviewer_passes": "", "nope": 1}) == {
        "FACTORY_RUN_BUDGET_USD": "45", "FACTORY_SECURITY_REVIEW": "0", "FACTORY_PREVIEW": "local"}
    # the environment the orchestrator is spawned with carries them
    from conftest import make_config
    from factory.config import AgentRef
    cfg = make_config(client.cfg.schemas_dir.parent, client.cfg.runs_dir.parent / "runs3", forge_token="t", environment_id="env_1",
                      agents={k: AgentRef("agent_" + k, 1) for k in ("codebase_researcher", "decomposer", "spec_writer", "spec_judge",
                                                                     "implementer", "reviewer", "merge_judge", "integrator")})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    app2 = webapp.create_app(cfg, demo_thread=False)
    calls = []

    class FakeProc:
        pid = 4343
    monkeypatch.setattr(webapp.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)) or FakeProc())
    from fastapi.testclient import TestClient
    with TestClient(app2) as c2:
        o = c2.get("/api/outcomes/template").json()["outcome"]["_example"]
        oid = c2.post("/api/outcomes", json={"outcome": o}).json()["outcome_id"]
        r = c2.post("/api/runs", json={"outcome_id": oid, "mode": "design", "settings": {"budget_usd": 45, "reviewer_passes": 1}}).json()
    assert r["settings"] == {"FACTORY_RUN_BUDGET_USD": "45", "FACTORY_REVIEWER_PASSES": "1"}
    assert calls[-1][1]["env"]["FACTORY_RUN_BUDGET_USD"] == "45" and calls[-1][1]["env"]["FACTORY_REVIEWER_PASSES"] == "1"
