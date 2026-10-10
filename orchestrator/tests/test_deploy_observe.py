"""Stage 5 without a network: a scripted pipeline source and e2e runner drive `observe_deploy`."""
from pathlib import Path

import pytest

from factory.config import Config
from factory.gates import Decision, GateDecision, InMemoryQueue, LogNotifier
from factory.gitlab import parse_pipeline_webhook
from factory.stages import RunContext
from factory.stages.deploy_observe import handle_pipeline_webhook, observe_deploy, wait_for_pipeline
from factory.state import Policy, WorkItemStage
from factory.store import RunStore

RUN = "run_deploy0001"
SHA = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c"


class ScriptedSource:
    """Returns the scripted statuses in order, then repeats the last one."""

    def __init__(self, statuses, env_url="https://dev.ledgerline.internal", log="step_script: rails db:migrate failed"):
        self.statuses = list(statuses)
        self.env_url = env_url
        self.log = log
        self.calls = 0

    def _next(self):
        s = self.statuses[min(self.calls, len(self.statuses) - 1)]
        self.calls += 1
        return {"id": 501, "status": s, "ref": "main", "sha": SHA, "web_url": "https://gitlab.example.internal/p/-/pipelines/501"}

    def latest_pipeline(self, ref, sha=None):
        return self._next()

    def pipeline(self, pipeline_id):
        return self._next()

    def failed_job_log_excerpt(self, pipeline_id, max_chars=6000):
        return self.log

    def environment(self, name):
        return {"id": 1, "name": name, "external_url": self.env_url, "state": "available", "last_deployment": None}


class AutoDecide(InMemoryQueue):
    def __init__(self, decision: Decision, comments: str = ""):
        super().__init__()
        self.auto, self.comments, self.opened = decision, comments, []

    def open(self, gate):
        super().open(gate)
        self.opened.append(gate)
        self.decide(GateDecision(gate_id=gate.gate_id, decision=self.auto, decided_by="sam.k", comments=self.comments,
                                 artifact_shas=tuple(a.sha256 for a in gate.artifacts)))


def _ctx(tmp_path: Path, root: Path, queue) -> RunContext:
    cfg = Config(agents={}, environment_id="", vault_id=None, gitlab_url="https://gitlab.example.internal", gitlab_token=None,
                 gitlab_project="ledgerline/app", service_manifest_path="service_manifest.json", dev_environment_name="dev",
                 preview_environment_prefix="preview/", smoke_commands=(), console_workspace="w", runs_dir=tmp_path,
                 schemas_dir=root / "schemas", rubrics_dir=root / "rubrics", templates_dir=root / "templates",
                 pin_agent_versions=True)
    return RunContext(cfg=cfg, store=RunStore(tmp_path), driver=None, queue=queue, notifier=LogNotifier(), gitlab=None,
                      policy=Policy(), run_id=RUN)


def _seed_merged(ctx: RunContext, examples, wid="wi-smart-retries"):
    ctx.event("run.created", {"outcome_id": "out_ledger0001"})
    ctx.event("artifact.produced", {"artifact_type": "decomposition", "revision": 1,
                                    "work_items": examples["decomposition"]["work_items"]}, actor="agent")
    for stage in ("spec_drafting", "spec_judging", "spec_approved", "implementing", "reviewing", "merge_judging",
                  "integrating", "mr_open", "merged"):
        ctx.event("work_item.stage", {"stage": stage}, work_item_id=wid)


def _kinds(ctx):
    return [e.kind for e in ctx.store.events(ctx.run_id)]


def test_wait_for_pipeline_polls_to_terminal():
    src = ScriptedSource(["running", "running", "success"])
    obs = wait_for_pipeline(src, "main", SHA, poll_seconds=0, sleep=lambda s: None)
    assert obs.status == "succeeded" and obs.environment_url == "https://dev.ledgerline.internal"
    assert src.calls == 3
    obs = wait_for_pipeline(ScriptedSource(["failed"]), "main", SHA, poll_seconds=0, sleep=lambda s: None)
    assert obs.status == "failed" and "db:migrate" in obs.log_excerpt
    ticks = iter([0, 1, 2, 3, 100])
    obs = wait_for_pipeline(ScriptedSource(["running"]), "main", SHA, poll_seconds=0, timeout_seconds=10,
                            sleep=lambda s: None, clock=lambda: next(ticks))
    assert obs.status == "timed_out"


def test_observe_deploy_happy_path(tmp_path, root, examples):
    ctx = _ctx(tmp_path, root, AutoDecide(Decision.REJECT))
    _seed_merged(ctx, examples)
    spec = examples["spec_smart_retries"]
    ran = []

    def run_e2e(url, entries):
        ran.append(url)
        return [{"command": e["command"], "target": "preview", "exit_code": 0, "covers_ac": e["covers_ac"]} for e in entries]

    stage = observe_deploy(ctx, spec, SHA, "main", ScriptedSource(["running", "success"]), run_e2e,
                           smoke_commands=("curl -fsS $BASE_URL/up",), poll_seconds=0, sleep=lambda s: None)
    assert stage == WorkItemStage.DEPLOYED_DEV
    assert ran == ["https://dev.ledgerline.internal"]
    assert [k for k in _kinds(ctx) if k.startswith(("deploy", "e2e", "gate"))] == \
        ["deploy.started", "deploy.succeeded", "e2e.dev_passed"]
    st = ctx.store.state(RUN).work_items["wi-smart-retries"]
    assert st.stage == WorkItemStage.DEPLOYED_DEV and st.dev_environment_url == "https://dev.ledgerline.internal"
    passed = next(e for e in ctx.store.events(RUN) if e.kind == "e2e.dev_passed")
    # The spec's three preview entries plus the smoke command ran; the local entry did not.
    assert len(passed.payload["results"]) == 4
    assert not ctx.queue.opened  # no gate on success


def test_pipeline_failure_opens_gate_and_routes_back_to_implementing(tmp_path, root, examples):
    queue = AutoDecide(Decision.REQUEST_CHANGES, comments="Migration needs the pgcrypto extension; add it additively.")
    ctx = _ctx(tmp_path, root, queue)
    _seed_merged(ctx, examples)
    stage = observe_deploy(ctx, examples["spec_smart_retries"], SHA, "main", ScriptedSource(["pending", "failed"]),
                           lambda url, entries: [], poll_seconds=0, sleep=lambda s: None)
    assert stage == WorkItemStage.IMPLEMENTING
    assert ctx.store.state(RUN).work_items["wi-smart-retries"].stage == WorkItemStage.IMPLEMENTING
    gate = queue.opened[0]
    assert gate.kind.value == "deploy_failure" and "db:migrate" in gate.summary
    assert {Decision.REQUEST_CHANGES, Decision.APPROVE, Decision.REJECT} == set(gate.options)
    kinds = [k for k in _kinds(ctx) if k.startswith(("deploy", "e2e", "gate"))]
    assert kinds == ["deploy.started", "deploy.failed", "gate.opened", "gate.decided"]
    failed = next(e for e in ctx.store.events(RUN) if e.kind == "deploy.failed")
    assert "db:migrate" in failed.payload["log_excerpt"]
    feedback = ctx.store.get_artifact(RUN, "deploy", "spec_smart-retries-deploy_feedback.md").decode()
    assert "pgcrypto" in feedback and "db:migrate" in feedback


def test_dev_e2e_failure_opens_gate_and_reject_closes(tmp_path, root, examples):
    queue = AutoDecide(Decision.REJECT)
    ctx = _ctx(tmp_path, root, queue)
    _seed_merged(ctx, examples)

    def run_e2e(url, entries):
        return [{"command": e["command"], "target": "preview", "exit_code": 0 if i else 1, "covers_ac": e["covers_ac"],
                 "stderr_tail": "expected 200, got 502"} for i, e in enumerate(entries)]

    stage = observe_deploy(ctx, examples["spec_smart_retries"], SHA, "main", ScriptedSource(["success"]), run_e2e,
                           poll_seconds=0, sleep=lambda s: None)
    assert stage == WorkItemStage.CLOSED
    kinds = [k for k in _kinds(ctx) if k.startswith(("deploy", "e2e", "gate"))]
    assert kinds == ["deploy.started", "deploy.succeeded", "e2e.dev_failed", "gate.opened", "gate.decided"]
    assert "502" in queue.opened[0].summary


def test_approve_reobserves_once_then_escalates(tmp_path, root, examples):
    queue = AutoDecide(Decision.APPROVE)
    ctx = _ctx(tmp_path, root, queue)
    _seed_merged(ctx, examples)
    src = ScriptedSource(["failed"])
    stage = observe_deploy(ctx, examples["spec_smart_retries"], SHA, "main", src, lambda u, e: [],
                           poll_seconds=0, sleep=lambda s: None, max_reobserve=1)
    assert stage == WorkItemStage.ESCALATED and len(queue.opened) == 2
    assert ctx.store.state(RUN).work_items["wi-smart-retries"].stage == WorkItemStage.ESCALATED


def test_webhook_stub_records_deploy_started_once(tmp_path, root, examples):
    ctx = _ctx(tmp_path, root, AutoDecide(Decision.REJECT))
    _seed_merged(ctx, examples)
    payload = {"object_kind": "pipeline", "object_attributes": {"id": 9, "status": "running", "ref": "main", "sha": SHA,
                                                                "url": "https://gitlab.example.internal/p/-/pipelines/9"},
               "project": {"path_with_namespace": "ledgerline/app"},
               "builds": [{"name": "deploy-dev", "environment": {"name": "dev"}}]}
    assert parse_pipeline_webhook({"object_kind": "merge_request"}) is None
    mapping = {SHA: ("wi-smart-retries", "spec_smart-retries")}
    assert handle_pipeline_webhook(ctx, payload, mapping, "main")["environments"] == ["dev"]
    handle_pipeline_webhook(ctx, payload, mapping, "main")
    assert _kinds(ctx).count("deploy.started") == 1
    assert handle_pipeline_webhook(ctx, dict(payload, object_attributes={**payload["object_attributes"], "ref": "feature"}),
                                   mapping, "main") is None
    assert ctx.store.state(RUN).work_items["wi-smart-retries"].dev_pipeline_id == 9


def test_wait_for_preview_polls_until_ready(tmp_path, root, examples):
    from factory.stages.implement import wait_for_preview

    class FakeGitLab:
        def __init__(self):
            self.n = 0

        def latest_pipeline(self, ref, sha=None):
            self.n += 1
            return {"id": 1, "status": "running" if self.n < 3 else "success", "ref": ref, "sha": sha, "web_url": "u"}

        def environment_url_for_branch(self, branch, prefix="preview/"):
            return "https://factory-run-x-spec.preview.example.internal" if self.n >= 3 else None

    ctx = _ctx(tmp_path, root, AutoDecide(Decision.REJECT))
    ctx.gitlab = FakeGitLab()
    info = wait_for_preview(ctx, "factory/run_x/spec_y", SHA, poll_seconds=0, sleep=lambda s: None)
    assert info["status"] == "ready" and info["url"].startswith("https://") and ctx.gitlab.n == 3
    ctx.gitlab = None
    assert wait_for_preview(ctx, "b", SHA, sleep=lambda s: None)["status"] == "unavailable"
