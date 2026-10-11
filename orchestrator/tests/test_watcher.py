"""The change-request watcher (Gate 3 and Stage 5) against a scripted forge. No network, no platform.

Done means: a run with an open pull request that a human merges on the forge ends at DEPLOYED_DEV, or at a
DEPLOY_FAILURE gate, with no manual event edits; and running the watcher again never duplicates an event."""
import json

from conftest import make_config

from factory import checks
from factory.gates import Decision, GateDecision, InMemoryQueue, LogNotifier
from factory.stages import RunContext
from factory.state import Policy, WorkItemStage
from factory.store import RunStore
from factory.watcher import HEARTBEAT, Watcher, heartbeat, pending_work_items

RUN = "run_watch000001"
WID, SID, CR = "wi-smart-retries", "spec_smart-retries", 418
HEAD, MERGE = "a" * 40, "b" * 40
DEV_URL = "https://dev.ledgerline.internal"


class FakeForge:
    """The slice of `Forge` the watcher touches. Change-request states and pipeline statuses are scripted lists that
    are consumed one per call and repeat their last entry."""
    kind = "github"
    repo = "ledgerline/app"

    def __init__(self, cr_states=("opened",), pipeline=("success",), approved=True, env_url=DEV_URL,
                 merge_sha=MERGE, log="job deploy (failure)\nrails db:migrate failed"):
        self.cr_states, self.pipeline_statuses = list(cr_states), list(pipeline)
        self.approved, self.env_url, self.merge_sha, self.log = approved, env_url, merge_sha, log
        self.calls = {"change_request_state": 0, "latest_pipeline": 0, "pipeline": 0, "environment": 0, "approved": 0}

    def _take(self, seq, key):
        v = seq[min(self.calls[key], len(seq) - 1)]
        self.calls[key] += 1
        return v

    def change_request_state(self, cr_id):
        assert cr_id == CR
        state = self._take(self.cr_states, "change_request_state")
        return {"state": state, "merged_at": "2026-10-10T14:00:00Z" if state == "merged" else None, "sha": HEAD,
                "merge_commit_sha": self.merge_sha if state == "merged" else None, "labels": ["factory"], "detailed_merge_status": "clean"}

    def change_request_approved(self, cr_id):
        self.calls["approved"] += 1
        return self.approved

    def _pipe(self, key):
        return {"id": 4812, "status": self._take(self.pipeline_statuses, key), "ref": "main", "sha": MERGE,
                "web_url": "https://github.com/ledgerline/app/actions/runs/4812"}

    def latest_pipeline(self, ref, sha=None):
        assert ref == "main" and sha == MERGE
        return self._pipe("latest_pipeline")

    def pipeline(self, pipeline_id):
        return self._pipe("pipeline")

    def failed_job_log_excerpt(self, pipeline_id, max_chars=6000):
        return self.log

    def environment(self, name):
        self.calls["environment"] += 1
        return {"id": 1, "name": name, "external_url": self.env_url, "state": "success", "last_deployment": None}

    def authenticated_url(self, repo_url):
        return repo_url


def _ctx(tmp_path, root, forge, queue=None) -> RunContext:
    return RunContext(cfg=make_config(root, tmp_path), store=RunStore(tmp_path), driver=None, queue=queue or InMemoryQueue(),
                      notifier=LogNotifier(), forge=forge, policy=Policy(dev_pipeline_timeout_seconds=600), run_id=RUN)


def _seed_cr_open(ctx: RunContext, examples) -> dict:
    """A run that reached a pull request: the shape `run_integrate` leaves behind, plus the approved spec in the store."""
    outcome = examples["outcome"]
    ctx.event("run.created", {"policy": {"preview_mode": "local"}, "budget_usd": 50, "outcome_id": outcome["outcome_id"], "title": outcome["title"]}, stage="intake")
    ctx.store.put_artifact(RUN, "intake", "outcome.json", json.dumps(outcome).encode())
    ctx.event("stage.entered", {"stage": "decomposing"})
    ctx.event("artifact.produced", {"artifact_type": "decomposition", "revision": 1,
                                    "work_items": examples["decomposition"]["work_items"]}, actor="agent")
    for stage in ("gate_1_decomposition", "specifying", "gate_2_spec_readiness", "implementing"):
        ctx.event("stage.entered", {"stage": stage})
    spec = examples["spec_smart_retries"]
    ctx.store.put_artifact(RUN, "specify", f"final-{SID}.json", json.dumps(spec).encode())
    for stage in ("spec_drafting", "spec_judging", "spec_approved", "implementing", "reviewing", "merge_judging", "integrating"):
        ctx.event("work_item.stage", {"stage": stage, "spec_id": SID}, work_item_id=WID, spec_id=SID)
    ctx.event("cr.opened", {"id": CR, "url": f"https://github.com/ledgerline/app/pull/{CR}", "branch": f"factory/{RUN}/{SID}", "forge": "github"},
              work_item_id=WID, spec_id=SID)
    ctx.event("work_item.stage", {"stage": "cr_open", "cr_id": CR}, work_item_id=WID, spec_id=SID)
    ctx.event("run.completed", {"specs_with_change_requests": [SID]})
    return outcome


def _kinds(ctx, since=0):
    return [e.kind for e in ctx.store.events(RUN) if e.seq >= since]


def _ok_e2e(url, entries):
    assert url == DEV_URL
    return [{"command": e["command"], "target": "preview", "exit_code": 0, "covers_ac": e.get("covers_ac", [])} for e in entries]


def _assert_schema_valid(ctx, schemas_dir):
    for ev in ctx.store.events(RUN):
        d = ev.to_dict()
        d["actor"] = {k: v for k, v in d["actor"].items() if k in ("type", "id")}
        assert checks.schema_valid(d, schemas_dir / "run_event.schema.json", schemas_dir).passed, (ev.kind, d)


# ---------------------------------------------------------------- the happy path

def test_merged_pull_request_ends_at_deployed_dev(tmp_path, root, examples, schemas_dir):
    forge = FakeForge(cr_states=("opened", "opened", "merged"), pipeline=("running", "running", "success"))
    ctx = _ctx(tmp_path, root, forge)
    outcome = _seed_cr_open(ctx, examples)
    assert [w.work_item_id for w in pending_work_items(ctx.store.state(RUN))] == [WID]
    w = Watcher(ctx, outcome, run_e2e=_ok_e2e, sleep=lambda s: None, heartbeat=False)

    r1 = w.tick()
    assert r1.polled == [{"work_item_id": WID, "cr_id": CR, "state": "opened"}] and r1.pending == [WID] and r1.events_appended == 0
    before = ctx.store.state(RUN).last_seq + 1
    r2 = w.tick()                                   # still open: nothing appended
    assert r2.events_appended == 0 and _kinds(ctx, before) == []

    r3 = w.tick()                                   # the human merged on the forge
    assert r3.merged == [WID] and r3.deployed == [WID] and r3.pending == [] and not r3.gates_opened and not r3.errors
    assert _kinds(ctx, before) == ["cr.merged", "work_item.stage", "work_item.stage", "deploy.started", "deploy.succeeded",
                                   "e2e.dev_passed", "work_item.stage"]
    st = ctx.store.state(RUN)
    wi = st.work_items[WID]
    assert wi.stage == WorkItemStage.DEPLOYED_DEV and wi.merge_sha == MERGE and wi.dev_environment_url == DEV_URL and wi.cr_id == CR
    merged = next(e for e in ctx.store.events(RUN) if e.kind == "cr.merged")
    assert merged.payload["merge_commit_sha"] == MERGE and merged.payload["approved"] is True and merged.payload["id"] == CR
    assert merged.payload["url"].endswith(f"/pull/{CR}") and merged.actor["type"] == "human" and merged.spec_id == SID
    started = next(e for e in ctx.store.events(RUN) if e.kind == "deploy.started")
    assert started.payload["sha"] == MERGE and started.payload["attempt"] == 0
    passed = next(e for e in ctx.store.events(RUN) if e.kind == "e2e.dev_passed")
    assert len(passed.payload["results"]) == 3           # the spec's three preview entries; the local one is not run against dev
    assert forge.calls["change_request_state"] == 3 and forge.calls["approved"] == 1
    _assert_schema_valid(ctx, schemas_dir)

    # idempotent: nothing left to watch, nothing appended, the forge is not asked again
    last = ctx.store.state(RUN).last_seq
    r4 = w.tick()
    assert r4.pending == [] and r4.events_appended == 0 and ctx.store.state(RUN).last_seq == last
    assert forge.calls["change_request_state"] == 3
    assert Watcher(ctx, outcome, run_e2e=_ok_e2e, heartbeat=False).pending() == []


def test_watch_loop_stops_when_done_and_writes_a_heartbeat(tmp_path, root, examples):
    forge = FakeForge(cr_states=("opened", "merged"), pipeline=("success",))
    ctx = _ctx(tmp_path, root, forge)
    outcome = _seed_cr_open(ctx, examples)
    slept = []
    w = Watcher(ctx, outcome, run_e2e=_ok_e2e, sleep=slept.append, clock=lambda: 0.0)
    ticks = []
    final = w.watch(poll_seconds=30, timeout_seconds=3600, on_tick=ticks.append)
    assert len(ticks) == 2 and slept == [30] and final.deployed == [WID] and final.pending == []
    hb = heartbeat(tmp_path, RUN)
    assert (tmp_path / RUN / HEARTBEAT).is_file() and hb["pending"] == [] and hb["stopped_at"] and hb["alive"] is False
    assert hb["last_tick"]["deployed"] == [WID]
    assert heartbeat(tmp_path, "run_nothing0001") is None


def test_watch_loop_is_bounded_by_its_deadline(tmp_path, root, examples):
    forge = FakeForge(cr_states=("opened",))
    ctx = _ctx(tmp_path, root, forge)
    outcome = _seed_cr_open(ctx, examples)
    now = [0.0]

    def sleep(s):
        now[0] += s

    w = Watcher(ctx, outcome, run_e2e=_ok_e2e, sleep=sleep, clock=lambda: now[0])
    final = w.watch(poll_seconds=60, timeout_seconds=300)
    assert final.pending == [WID] and forge.calls["change_request_state"] == 6 and now[0] == 300
    assert ctx.store.state(RUN).work_items[WID].stage == WorkItemStage.CR_OPEN
    hb = heartbeat(tmp_path, RUN)
    assert hb["pending"] == [WID] and hb["stopped_at"]


# ---------------------------------------------------------------- closed without merging

def test_closed_pull_request_closes_the_item_once(tmp_path, root, examples, schemas_dir):
    forge = FakeForge(cr_states=("closed",))
    ctx = _ctx(tmp_path, root, forge)
    outcome = _seed_cr_open(ctx, examples)
    w = Watcher(ctx, outcome, run_e2e=_ok_e2e, heartbeat=False)
    before = ctx.store.state(RUN).last_seq + 1
    r = w.tick()
    assert r.closed == [WID] and r.pending == []
    assert _kinds(ctx, before) == ["cr.closed", "work_item.stage"]
    assert ctx.store.state(RUN).work_items[WID].stage == WorkItemStage.CLOSED
    assert w.tick().events_appended == 0 and forge.calls["change_request_state"] == 1
    _assert_schema_valid(ctx, schemas_dir)


# ---------------------------------------------------------------- failures and the gate, across restarts

def test_pipeline_failure_opens_a_gate_that_survives_a_restart(tmp_path, root, examples, schemas_dir):
    forge = FakeForge(cr_states=("merged",), pipeline=("pending", "failed"))
    queue = InMemoryQueue()
    ctx = _ctx(tmp_path, root, forge, queue)
    outcome = _seed_cr_open(ctx, examples)
    w = Watcher(ctx, outcome, run_e2e=_ok_e2e, sleep=lambda s: None, heartbeat=False)

    r = w.tick()
    assert r.merged == [WID] and r.gates_opened == [WID] and r.pending == [WID] and not r.deployed
    kinds = [k for k in _kinds(ctx) if k.startswith(("cr.", "deploy", "e2e", "gate"))]
    assert kinds == ["cr.opened", "cr.merged", "deploy.started", "deploy.failed", "gate.opened"]
    st = ctx.store.state(RUN)
    assert st.work_items[WID].stage == WorkItemStage.DEPLOYING_DEV and len(st.open_gates) == 1
    gate_id, gate = next(iter(st.open_gates.items()))
    assert gate["kind"] == "deploy_failure" and gate["work_item_id"] == WID and "db:migrate" in gate["summary"]
    assert gate["assignees"] == [outcome["owner"]["handle"]]
    opened = next(e for e in ctx.store.events(RUN) if e.kind == "gate.opened")
    assert opened.work_item_id == WID

    # no decision yet: ticks wait, appending nothing and not re-observing
    last = ctx.store.state(RUN).last_seq
    r = w.tick()
    assert r.waiting_on_gate == [WID] and r.events_appended == 0 and ctx.store.state(RUN).last_seq == last
    assert forge.calls["latest_pipeline"] == 2        # one observation: once for deploy.started, once to begin the wait

    # the watcher is restarted (new process: new context over the same store and queue files)
    ctx2 = _ctx(tmp_path, root, forge, queue)
    w2 = Watcher(ctx2, outcome, run_e2e=_ok_e2e, sleep=lambda s: None, heartbeat=False)
    r = w2.tick()
    assert r.waiting_on_gate == [WID] and r.events_appended == 0 and not r.gates_opened     # picked up, not re-opened

    # the human rejects from another process: the next tick records and applies it
    queue.decide(GateDecision(gate_id=gate_id, decision=Decision.REJECT, decided_by="sam.k",
                              artifact_shas=tuple(a["sha256"] for a in gate["artifacts"])))
    r = w2.tick()
    assert r.decisions_applied == [WID] and r.pending == []
    assert _kinds(ctx2, last + 1) == ["gate.decided", "work_item.stage", "work_item.stage"]
    st = ctx2.store.state(RUN)
    assert st.work_items[WID].stage == WorkItemStage.CLOSED and not st.open_gates
    assert w2.tick().events_appended == 0
    _assert_schema_valid(ctx2, schemas_dir)


def test_approve_reobserves_the_next_pipeline_then_escalates(tmp_path, root, examples):
    forge = FakeForge(cr_states=("merged",), pipeline=("failed",))

    class AutoApprove(InMemoryQueue):
        def open(self, gate):
            super().open(gate)
            self.decide(GateDecision(gate_id=gate.gate_id, decision=Decision.APPROVE, decided_by="sam.k",
                                     artifact_shas=tuple(a.sha256 for a in gate.artifacts)))

    ctx = _ctx(tmp_path, root, forge, AutoApprove())
    outcome = _seed_cr_open(ctx, examples)
    w = Watcher(ctx, outcome, run_e2e=_ok_e2e, sleep=lambda s: None, heartbeat=False)
    r = w.tick()                                      # merge, fail, gate 1 (opened this tick; decided by the queue already)
    assert r.merged == [WID] and r.gates_opened == [WID]
    r = w.tick()                                      # approve -> second observation -> fails -> gate 2
    assert r.decisions_applied == [WID] and r.gates_opened == [WID]
    starts = [e.payload["attempt"] for e in ctx.store.events(RUN) if e.kind == "deploy.started"]
    assert starts == [0, 1]
    r = w.tick()                                      # approve again: re-observations exhausted, a human takes over
    assert r.decisions_applied == [WID] and r.pending == [] and not r.gates_opened
    assert ctx.store.state(RUN).work_items[WID].stage == WorkItemStage.ESCALATED
    assert forge.calls["latest_pipeline"] == 4        # two observations, two lookups each


def test_dev_e2e_failure_then_request_changes_routes_back_to_implementing(tmp_path, root, examples):
    forge = FakeForge(cr_states=("merged",), pipeline=("success",))
    queue = InMemoryQueue()
    ctx = _ctx(tmp_path, root, forge, queue)
    outcome = _seed_cr_open(ctx, examples)

    def flaky_e2e(url, entries):
        return [{"command": e["command"], "target": "preview", "exit_code": 0 if i else 7, "covers_ac": e.get("covers_ac", []),
                 "stderr_tail": "expected 200, got 502"} for i, e in enumerate(entries)]

    w = Watcher(ctx, outcome, run_e2e=flaky_e2e, sleep=lambda s: None, heartbeat=False)
    r = w.tick()
    assert r.gates_opened == [WID]
    kinds = [k for k in _kinds(ctx) if k.startswith(("deploy", "e2e", "gate"))]
    assert kinds == ["deploy.started", "deploy.succeeded", "e2e.dev_failed", "gate.opened"]
    gate_id, gate = next(iter(ctx.store.state(RUN).open_gates.items()))
    assert "502" in gate["summary"]
    queue.decide(GateDecision(gate_id=gate_id, decision=Decision.REQUEST_CHANGES, decided_by="sam.k",
                              comments="The dev database lacks the pgcrypto extension; add the migration additively.",
                              artifact_shas=tuple(a["sha256"] for a in gate["artifacts"])))
    r = w.tick()
    assert r.decisions_applied == [WID] and r.pending == []
    assert ctx.store.state(RUN).work_items[WID].stage == WorkItemStage.IMPLEMENTING
    fb = ctx.store.get_artifact(RUN, "deploy", f"{SID}-deploy_feedback.md").decode()
    assert "pgcrypto" in fb and "502" in fb


def test_decided_but_unapplied_gate_is_applied_after_a_restart(tmp_path, root, examples):
    """The watcher recorded gate.decided and died before acting: the next watcher acts on the recorded decision."""
    forge = FakeForge(cr_states=("merged",), pipeline=("failed",))
    queue = InMemoryQueue()
    ctx = _ctx(tmp_path, root, forge, queue)
    outcome = _seed_cr_open(ctx, examples)
    Watcher(ctx, outcome, run_e2e=_ok_e2e, sleep=lambda s: None, heartbeat=False).tick()
    gate_id, gate = next(iter(ctx.store.state(RUN).open_gates.items()))
    ctx.event("gate.decided", {"gate_id": gate_id, "kind": "deploy_failure", "decision": "reject", "comments": "", "approved_subset": [],
                               "artifact_shas": [a["sha256"] for a in gate["artifacts"]]}, stage="deploy_failure", actor="human",
              work_item_id=WID)
    r = Watcher(_ctx(tmp_path, root, forge, queue), outcome, run_e2e=_ok_e2e, sleep=lambda s: None, heartbeat=False).tick()
    assert r.decisions_applied == [WID] and r.pending == []
    assert ctx.store.state(RUN).work_items[WID].stage == WorkItemStage.CLOSED
    assert _kinds(ctx).count("gate.decided") == 1 and _kinds(ctx).count("gate.opened") == 1


def test_interrupted_observation_is_observed_again_without_moving_the_item_twice(tmp_path, root, examples):
    """A watcher killed while the pipeline was running left the item at DEPLOYING_DEV with deploy.started and no outcome."""
    forge = FakeForge(cr_states=("merged",), pipeline=("success",))
    ctx = _ctx(tmp_path, root, forge)
    outcome = _seed_cr_open(ctx, examples)
    ctx.event("cr.merged", {"id": CR, "merge_commit_sha": MERGE, "approved": True, "forge": "github"}, work_item_id=WID, spec_id=SID, actor="human")
    ctx.event("work_item.stage", {"stage": "merged", "merge_sha": MERGE}, work_item_id=WID, spec_id=SID)
    ctx.event("work_item.stage", {"stage": "deploying_dev"}, work_item_id=WID, spec_id=SID)
    ctx.event("deploy.started", {"pipeline_id": 4812, "sha": MERGE, "attempt": 0}, work_item_id=WID, spec_id=SID, actor="platform")
    before = ctx.store.state(RUN).last_seq + 1
    r = Watcher(ctx, outcome, run_e2e=_ok_e2e, sleep=lambda s: None, heartbeat=False).tick()
    assert r.deployed == [WID] and forge.calls["change_request_state"] == 0
    assert _kinds(ctx, before) == ["deploy.started", "deploy.succeeded", "e2e.dev_passed", "work_item.stage"]
    assert ctx.store.state(RUN).work_items[WID].stage == WorkItemStage.DEPLOYED_DEV


def test_forge_errors_are_reported_and_retried_not_fatal(tmp_path, root, examples):
    class Broken(FakeForge):
        def change_request_state(self, cr_id):
            self.calls["change_request_state"] += 1
            if self.calls["change_request_state"] == 1:
                raise ConnectionError("api.github.com: connection reset")
            return {"state": "opened", "sha": HEAD}

    forge = Broken()
    ctx = _ctx(tmp_path, root, forge)
    outcome = _seed_cr_open(ctx, examples)
    w = Watcher(ctx, outcome, run_e2e=_ok_e2e, heartbeat=False)
    r = w.tick()
    assert r.errors == [{"work_item_id": WID, "error": "ConnectionError: api.github.com: connection reset"}] and r.pending == [WID]
    assert "connection reset" in r.summary()
    r = w.tick()
    assert not r.errors and r.polled[0]["state"] == "opened"


def test_items_without_a_number_are_not_watched(tmp_path, root, examples):
    ctx = _ctx(tmp_path, root, FakeForge())
    outcome = examples["outcome"]
    ctx.event("run.created", {"policy": {"preview_mode": "local"}, "budget_usd": 50, "outcome_id": outcome["outcome_id"]})
    ctx.event("artifact.produced", {"artifact_type": "decomposition", "revision": 1, "work_items": examples["decomposition"]["work_items"]}, actor="agent")
    for stage in ("spec_drafting", "spec_judging", "spec_approved", "implementing", "reviewing", "merge_judging", "integrating", "cr_open"):
        ctx.event("work_item.stage", {"stage": stage}, work_item_id=WID)       # cr_open without a cr_id: nothing to poll
    assert pending_work_items(ctx.store.state(RUN)) == []
    assert Watcher(ctx, outcome, heartbeat=False).tick().pending == []


def test_watcher_without_a_forge_reports_the_gap(tmp_path, root, examples):
    ctx = _ctx(tmp_path, root, None)
    outcome = _seed_cr_open(ctx, examples)
    r = Watcher(ctx, outcome, heartbeat=False).tick()
    assert r.errors and "FORGE_TOKEN" in r.errors[0]["error"] and r.pending == [WID]


# ---------------------------------------------------------------- the blocking Stage 5 entry point still composes the same pieces

def test_observe_deploy_blocking_path_matches_the_watcher(tmp_path, root, examples):
    from factory.stages.deploy_observe import observe_deploy

    forge = FakeForge(pipeline=("running", "success"))
    ctx = _ctx(tmp_path, root, forge)
    _seed_cr_open(ctx, examples)
    ctx.event("cr.merged", {"id": CR, "merge_commit_sha": MERGE}, work_item_id=WID, spec_id=SID, actor="human")
    ctx.event("work_item.stage", {"stage": "merged", "merge_sha": MERGE}, work_item_id=WID, spec_id=SID)
    stage = observe_deploy(ctx, examples["spec_smart_retries"], MERGE, "main", forge, _ok_e2e, poll_seconds=0, sleep=lambda s: None)
    assert stage == WorkItemStage.DEPLOYED_DEV
    assert ctx.store.state(RUN).work_items[WID].stage == WorkItemStage.DEPLOYED_DEV


# ---------------------------------------------------------------- the CLI

def test_cli_watch_once_runs_a_tick(tmp_path, root, examples, monkeypatch):
    from typer.testing import CliRunner

    from factory import cli

    runs = tmp_path / "runs"
    cfg = make_config(root, runs, forge_token="ghp_test")
    forge = FakeForge(cr_states=("merged",), pipeline=("success",))
    ctx = _ctx(runs, root, forge)
    _seed_cr_open(ctx, examples)
    monkeypatch.setattr(cli.cfgmod, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(cli, "_bind_forge", lambda c, outcome, want: (c, forge, None))
    monkeypatch.setattr("factory.watcher.local_e2e_runner", lambda ctx, outcome, branch: _ok_e2e)
    res = CliRunner().invoke(cli.app, ["watch", RUN, "--once"])
    assert res.exit_code == 0, res.output
    assert f"run {RUN} watching 1 work item(s) on github ledgerline/app" in res.output and "verified in dev wi-smart-retries" in res.output
    assert "wi-smart-retries=deployed_dev" in res.output
    assert RunStore(runs).state(RUN).work_items[WID].stage == WorkItemStage.DEPLOYED_DEV
    # second time: nothing left
    res = CliRunner().invoke(cli.app, ["watch", RUN, "--once"])
    assert res.exit_code == 0 and "nothing to watch" in res.output
    # unknown run, and an unconfigured forge
    assert CliRunner().invoke(cli.app, ["watch", "run_nothing0001", "--once"]).exit_code == 1
    monkeypatch.setattr(cli.cfgmod, "load", lambda *a, **k: make_config(root, runs))
    res = CliRunner().invoke(cli.app, ["watch", RUN, "--once"])
    assert res.exit_code == 1 and "FORGE_TOKEN" in res.output
