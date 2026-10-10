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
    assert r.status_code == 200 and "<title>Factory Console</title>" in r.text and "/static/app.js" in r.text
    assert client.get("/static/app.css").status_code == 200
    c = client.get("/api/config").json()
    assert c["forge"] == "github" and c["gate_surface"] == "web" and c["can_run_live"] is False and c["gaps"]


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
    assert t["_example"]["title"] and t["success_metrics"][0]["metric"] == ""
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
    assert client.post("/api/runs", json={"outcome_id": r["outcome_id"], "mode": "weird"}).status_code == 400
    started = client.post("/api/runs", json={"outcome_id": r["outcome_id"], "mode": "demo"}).json()
    assert started["mode"] == "demo" and started["run_id"].startswith("run_")
    assert client.get("/api/outcomes").json()["outcomes"][0]["runs"] == [started["run_id"]]
    assert client.get("/api/runs").json()["runs"][0]["title"] == "Try the console"
    assert client.post("/api/demo/tick").json()["ticked"] == [(started["run_id"], "awaiting_answer")] or \
        client.post("/api/demo/tick").json()["ticked"][0][1] == "awaiting_answer"
