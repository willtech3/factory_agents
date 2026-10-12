"""Stage 5 pieces without a network: a scripted pipeline source and e2e runner drive `wait_for_pipeline` and `observe_once`
(the watcher's full Stage 5 flow is in test_watcher.py)."""
from pathlib import Path

import pytest

from conftest import make_config
from factory.gates import InMemoryQueue, LogNotifier
from factory.stages import RunContext
from factory.stages.deploy_observe import observe_once, wait_for_pipeline
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


def _ctx(tmp_path: Path, root: Path, queue) -> RunContext:
    cfg = make_config(root, tmp_path)
    return RunContext(cfg=cfg, store=RunStore(tmp_path), driver=None, queue=queue, notifier=LogNotifier(), forge=None,
                      policy=Policy(), run_id=RUN)


def _seed_merged(ctx: RunContext, examples, wid="wi-smart-retries"):
    ctx.event("run.created", {"policy": {"preview_mode": "local"}, "budget_usd": 50, "outcome_id": "out_ledger0001"})
    ctx.event("artifact.produced", {"artifact_type": "decomposition", "revision": 1,
                                    "work_items": examples["decomposition"]["work_items"]}, actor="agent")
    for stage in ("spec_drafting", "spec_judging", "spec_approved", "implementing", "reviewing", "merge_judging",
                  "integrating", "cr_open", "merged"):
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
    no_pipe = ScriptedSource(["success"])
    no_pipe.latest_pipeline = lambda ref, sha=None: None
    ticks = iter([0, 1, 2, 100])
    obs = wait_for_pipeline(no_pipe, "main", SHA, poll_seconds=0, timeout_seconds=10, sleep=lambda s: None, clock=lambda: next(ticks))
    assert obs.status == "no_pipeline"
    obs = wait_for_pipeline(ScriptedSource(["success"], env_url=None), "main", SHA, poll_seconds=0, sleep=lambda s: None)
    assert obs.status == "failed" and "no external_url" in obs.log_excerpt     # a deploy with nowhere to test is not a pass


def test_wait_for_preview_polls_until_ready(tmp_path, root, examples):
    from factory.stages.implement import wait_for_preview

    class FakeForge:
        def __init__(self):
            self.n = 0

        def latest_pipeline(self, ref, sha=None):
            self.n += 1
            return {"id": 1, "status": "running" if self.n < 3 else "success", "ref": ref, "sha": sha, "web_url": "u"}

        def environment_url_for_branch(self, branch, prefix="preview/"):
            return "https://factory-run-x-spec.preview.example.internal" if self.n >= 3 else None

    ctx = _ctx(tmp_path, root, InMemoryQueue())
    ctx.forge = FakeForge()
    info = wait_for_preview(ctx, "factory/run_x/spec_y", SHA, poll_seconds=0, sleep=lambda s: None)
    assert info["status"] == "ready" and info["url"].startswith("https://") and ctx.forge.n == 3
    ctx.forge = None
    assert wait_for_preview(ctx, "b", SHA, sleep=lambda s: None)["status"] == "unavailable"


def test_managed_deploy_never_executes_local_callback(tmp_path,root,examples):
    from dataclasses import replace
    ctx=_ctx(tmp_path,root,InMemoryQueue());ctx.cfg=replace(ctx.cfg,preview_mode='managed')
    _seed_merged(ctx,examples)
    def forbidden(*args): raise AssertionError('local app execution attempted')
    obs=observe_once(ctx,examples['spec_smart_retries'],SHA,'main',ScriptedSource(['success']),forbidden,poll_seconds=0,sleep=lambda s:None)
    assert not obs.passed and obs.e2e_results[0]['status']=='blocked'


@pytest.mark.parametrize("watcher_mode", ["platform", "local"])
def test_deploy_preserves_hosted_mode_after_gate_recovery(tmp_path, root, examples, watcher_mode):
    """A fresh watcher must honor the hosted mode recorded by a recovered controller."""
    from dataclasses import replace

    ctx = _ctx(tmp_path, root, InMemoryQueue())
    ctx.cfg = replace(ctx.cfg, preview_mode=watcher_mode)
    _seed_merged(ctx, examples)
    ctx.event("run.failed", {"reason": "controller stopped at Gate 2"})
    ctx.event("run.resumed", {"stage": "gate_2_spec_readiness", "preview_mode": "managed"})

    def forbidden(*args):
        raise AssertionError("an upgraded hosted run executed checks on the watcher host")

    obs = observe_once(ctx, examples["spec_smart_retries"], SHA, "main", ScriptedSource(["success"]),
                       forbidden, poll_seconds=0, sleep=lambda s: None)
    assert not obs.passed
    assert all(row["status"] == "blocked" for row in obs.e2e_results)
    assert "e2e.dev_passed" not in _kinds(ctx)
