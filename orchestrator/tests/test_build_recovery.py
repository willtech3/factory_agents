"""Recover settled build escalations without replacing contracts or re-running completed authors."""
import json

import pytest
import test_gate_recovery as tgr
from conftest import make_config
from fastapi.testclient import TestClient

from factory import runner
from factory.state import RunStage
from factory.web.app import create_app


def escalated(tmp_path, root, monkeypatch):
    gate, _ = tgr.park(tmp_path, root, design_only=False)
    ctx = tgr.context(tmp_path, root, design_only=False)
    ctx.queue.record_decision(gate.gate_id, "approve", "will")

    def build(ctx, outcome, specs, spec_md, spec_verdict, **kwargs):
        ctx.event("stage.entered", {"stage": "implementing"})
        for sid, spec in specs.items():
            for stage in ("implementing", "reviewing", "merge_judging"):
                ctx.event("work_item.stage", {"stage": stage}, work_item_id=spec["work_item_id"], spec_id=sid)
        ctx.event("run.escalated", {"reason": "spec_a: merge judge said escalate"}, spec_id="spec_a")
        ctx.event("run.completed", {"specs_with_change_requests": []})  # old controller bug
        return RunStage.ESCALATED

    monkeypatch.setattr(runner, "implement_and_integrate", build)
    assert runner.resume(ctx) == RunStage.ESCALATED
    return ctx


def test_approved_build_escalation_is_recoverable_in_console_and_runner(tmp_path, root, monkeypatch):
    ctx = escalated(tmp_path, root, monkeypatch)
    client = TestClient(create_app(make_config(root, ctx.cfg.runs_dir)))
    detail = client.get(f"/api/runs/{tgr.RUN}").json()
    assert detail["resumable"] and detail["status"] == "escalated"
    assert all(ev["kind"] != "run.completed" for ev in detail["events"])
    originals = {sid: ctx.store.get_artifact(tgr.RUN, "specify", f"final-{sid}.json") for sid in tgr.SPECS}
    seen = {}

    def recover(ctx, outcome, specs, spec_md, spec_verdict, resume=None, guidance=None):
        seen.update(specs=specs, points=resume)
        assert ctx.store.state(tgr.RUN).stage == RunStage.IMPLEMENTING
        return RunStage.IMPLEMENTING

    monkeypatch.setattr(runner, "implement_and_integrate", recover)
    assert runner.resume(ctx) == RunStage.IMPLEMENTING
    assert set(seen["specs"]) == set(tgr.SPECS) and all(pt.round_no == 1 for pt in seen["points"].values())
    assert originals == {sid: ctx.store.get_artifact(tgr.RUN, "specify", f"final-{sid}.json") for sid in tgr.SPECS}


def test_recovery_refuses_changed_approved_contract_before_writing_resume(tmp_path, root, monkeypatch):
    ctx = escalated(tmp_path, root, monkeypatch)
    ctx.store.put_artifact(tgr.RUN, "specify", "final-spec_a.json", json.dumps({**tgr.SPECS["spec_a"], "title": "changed"}).encode(), replace=True)
    before = ctx.store.events(tgr.RUN)
    with pytest.raises(RuntimeError, match="no longer matches"):
        runner.resume(ctx)
    assert ctx.store.events(tgr.RUN) == before


def test_spec_escalations_and_open_gates_are_not_build_recovery(tmp_path, root, monkeypatch):
    ctx = escalated(tmp_path, root, monkeypatch)
    ctx.event("gate.opened", {"gate_id": "human", "kind": "escalation"})
    assert not runner.resumable_build_escalation(ctx.store.state(tgr.RUN), ctx.store.events(tgr.RUN))
    other = tgr.context(tmp_path / "spec", root, design_only=False)
    other.event("run.created", {"outcome_id": "o", "budget_usd": 50, "policy": {"preview_mode": "local"}})
    other.store.put_artifact(tgr.RUN, "intake", "outcome.json", json.dumps(tgr.OUTCOME).encode())
    other.event("stage.entered", {"stage": "decomposing"})
    other.event("run.escalated", {"reason": "specification needs revision"})
    assert not runner.resumable_build_escalation(other.store.state(tgr.RUN), other.store.events(tgr.RUN))
    with pytest.raises(RuntimeError, match="can be resumed"):
        runner.resume(other)


def test_console_recovery_keeps_recorded_managed_recipe_and_target(tmp_path, root, monkeypatch):
    ctx = escalated(tmp_path, root, monkeypatch)
    ctx.event("run.resumed", {"stage": "implementing", "preview_mode": "managed"})
    ctx.store.put_derived_artifact(tgr.RUN, "implement", "spec_a-r3-hosted_verification.json", json.dumps({
        "app_id": "app_verified", "approved_profile_sha256": "a" * 64, "vaults_attached": 0}).encode())
    ctx.event("verification.hosted", {"round": 3}, spec_id="spec_a")
    console = create_app(make_config(root, ctx.cfg.runs_dir, forge_repo="wrong/repo")).state.console
    env = console.resume_environment(tgr.RUN, console.run_detail(tgr.RUN))
    assert env["FACTORY_PREVIEW"] == "managed"
    assert env["FACTORY_TEST_PROFILE_SHA256"] == "a" * 64 and env["FACTORY_APP_ID"] == "app_verified"
    assert env["FACTORY_TEST_VAULT_IDS"] == "" and env["FORGE_REPO"] == "x/evergreen"
