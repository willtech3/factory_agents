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


def test_recovery_gate_is_bound_and_retry_is_recorded_once(tmp_path, root, monkeypatch):
    from factory import build_recovery as br
    ctx = escalated(tmp_path, root, monkeypatch)
    rec = br.snapshot(ctx.store, tgr.RUN)
    gate = br.open_gate(ctx.store, ctx.queue, tgr.RUN, rec)
    assert br.open_gate(ctx.store, ctx.queue, tgr.RUN, rec) == gate
    with pytest.raises(ValueError, match='Decide'):
        br.apply_decision(ctx.store, ctx.queue, tgr.RUN)
    ctx.queue.record_decision(gate, 'approve', 'will', 'Environment repaired')
    first = br.apply_decision(ctx.store, ctx.queue, tgr.RUN)
    before = ctx.store.events(tgr.RUN)
    assert br.apply_decision(ctx.store, ctx.queue, tgr.RUN) == first
    assert ctx.store.events(tgr.RUN) == before and not ctx.store.state(tgr.RUN).open_gates


def test_recovery_api_rejects_stale_and_empty_instructions_then_closes(tmp_path, root, monkeypatch):
    ctx = escalated(tmp_path, root, monkeypatch)
    app = create_app(make_config(root,ctx.cfg.runs_dir)); client=TestClient(app)
    rec = client.get(f'/api/runs/{tgr.RUN}').json()['recovery']
    before=ctx.store.events(tgr.RUN)
    endpoint=f'/api/runs/{tgr.RUN}/recovery'
    assert client.post(endpoint,json={'revision':'old','decision':'approve'}).status_code == 409
    assert client.post(endpoint,json={'revision':rec['revision'],'decision':'request_changes'}).status_code == 422
    assert ctx.store.events(tgr.RUN) == before
    result=client.post(endpoint,json={'revision':rec['revision'],'decision':'reject'})
    assert result.status_code == 200 and result.json()['mode'] == 'closed'
    assert ctx.store.state(tgr.RUN).stage == RunStage.CLOSED
    assert not ctx.store.state(tgr.RUN).open_gates
    assert client.post(endpoint,json={'revision':rec['revision'],'decision':'approve'}).status_code == 409


def test_request_changes_runs_a_fresh_bounded_cycle_with_owner_notes(tmp_path, root, monkeypatch):
    from factory import build_recovery as br
    ctx=escalated(tmp_path,root,monkeypatch)
    rec=br.snapshot(ctx.store,tgr.RUN); gate=br.open_gate(ctx.store,ctx.queue,tgr.RUN,rec)
    ctx.queue.record_decision(gate,'request_changes','will','Fix the observed regression; keep the approved scope.')
    seen={}
    def recover(ctx,outcome,specs,spec_md,spec_verdict,resume=None,guidance=None):
        seen.update(points=resume,guidance=guidance)
        return RunStage.IMPLEMENTING
    monkeypatch.setattr(runner,'implement_and_integrate',recover)
    assert runner.resume(ctx) == RunStage.IMPLEMENTING
    assert seen['points']['spec_a'].round_no == 2 and seen['points']['spec_a'].report is None
    assert seen['points']['spec_a'].review_through == 2
    assert 'Fix the observed regression' in seen['guidance']['spec_a']


def test_recovery_refuses_tampered_gate_snapshot(tmp_path,root,monkeypatch):
    from factory import build_recovery as br
    ctx=escalated(tmp_path,root,monkeypatch);rec=br.snapshot(ctx.store,tgr.RUN)
    gate=br.open_gate(ctx.store,ctx.queue,tgr.RUN,rec)
    ctx.queue.record_decision(gate,'approve','will')
    path=ctx.store.artifact_path(tgr.RUN,'recovery',rec['event_id']+'-'+rec['revision'][:12]+'.json')
    path.write_text(path.read_text()+' ')
    with pytest.raises(ValueError,match='no longer matches'):
        br.apply_decision(ctx.store,ctx.queue,tgr.RUN)
    assert gate in ctx.store.state(tgr.RUN).open_gates


def test_corrective_round_can_restart_after_its_report_is_saved(tmp_path,root,monkeypatch):
    from factory import build_recovery as br
    ctx=escalated(tmp_path,root,monkeypatch);rec=br.snapshot(ctx.store,tgr.RUN)
    gate=br.open_gate(ctx.store,ctx.queue,tgr.RUN,rec);ctx.queue.record_decision(gate,'request_changes','will','Correct the bug')
    br.apply_decision(ctx.store,ctx.queue,tgr.RUN)
    ctx.store.put_artifact(tgr.RUN,'implement','spec_a-r2-implementation_report.json',b'{"head_sha":"new"}')
    ctx.store.put_artifact(tgr.RUN,'implement','spec_a-r2-review_response.json',b'{"responses":[]}')
    points=runner.resume_points(ctx,tgr.SPECS)
    assert points['spec_a'].report['head_sha']=='new'


def test_recovery_rejects_unknown_run_before_creating_directories(tmp_path,root):
    app=create_app(make_config(root,tmp_path/'runs')); client=TestClient(app)
    for run_id in ('not-a-run','run_missing0001'):
        result=client.post(f'/api/runs/{run_id}/recovery',json={'revision':'x','decision':'reject'})
        assert result.status_code == 404
        assert not (tmp_path/'runs'/run_id).exists()


def test_new_implementer_block_does_not_show_a_previous_judge(tmp_path,root,monkeypatch):
    from factory import build_recovery as br
    ctx=escalated(tmp_path,root,monkeypatch)
    ctx.store.put_artifact(tgr.RUN,'merge_judge','spec_a-r3-merge_readiness.json',json.dumps({'round':3,'summary_for_human':'old failure','_head_sha':'old','deterministic_checks':[{'check':'build','passed':False}]}).encode())
    ctx.event('run.resumed',{'stage':'implementing'})
    ctx.store.put_artifact(tgr.RUN,'implement','spec_a-r4-implementation_report.json',b'{"head_commit":"new"}')
    ctx.store.put_artifact(tgr.RUN,'implement','spec_a-r4-blocked.md',b'New history conflict')
    ctx.event('run.escalated',{'reason':'blocked','summary':'New history conflict'},spec_id='spec_a')
    rec=br.snapshot(ctx.store,tgr.RUN)
    assert rec['summary']=='New history conflict' and rec['round']==4 and rec['head_sha']=='new'
    assert rec['checks']==[] and rec['artifact']=='implement/spec_a-r4-blocked.md'


def test_recheck_keeps_the_authorized_corrective_review_cycle(tmp_path,root,monkeypatch):
    from factory import build_recovery as br
    ctx=escalated(tmp_path,root,monkeypatch)
    rec=br.snapshot(ctx.store,tgr.RUN);gate=br.open_gate(ctx.store,ctx.queue,tgr.RUN,rec)
    ctx.queue.record_decision(gate,'request_changes','will','Fix it');br.apply_decision(ctx.store,ctx.queue,tgr.RUN)
    ctx.event('run.resumed',{'stage':'implementing'})
    ctx.store.put_artifact(tgr.RUN,'implement','spec_a-r2-implementation_report.json',b'{"head_sha":"new"}')
    ctx.store.put_artifact(tgr.RUN,'implement','spec_a-r2-review_response.json',b'{"responses":[]}')
    ctx.event('run.escalated',{'reason':'history conflict'},spec_id='spec_a')
    rec=br.snapshot(ctx.store,tgr.RUN);gate=br.open_gate(ctx.store,ctx.queue,tgr.RUN,rec)
    ctx.queue.record_decision(gate,'approve','will','Verify the compatible merge candidate')
    seen={}
    def recover(ctx,outcome,specs,spec_md,spec_verdict,resume=None,guidance=None):
        seen.update(points=resume);return RunStage.IMPLEMENTING
    monkeypatch.setattr(runner,'implement_and_integrate',recover)
    assert runner.resume(ctx)==RunStage.IMPLEMENTING
    assert seen['points']['spec_a'].review_through==2
    assert seen['points']['spec_a'].report['head_sha']=='new'


def test_changed_recovery_evidence_supersedes_the_old_gate(tmp_path,root,monkeypatch):
    from factory import build_recovery as br
    ctx=escalated(tmp_path,root,monkeypatch);rec=br.snapshot(ctx.store,tgr.RUN)
    old=br.open_gate(ctx.store,ctx.queue,tgr.RUN,rec)
    ctx.store.put_artifact(tgr.RUN,'implement','spec_a-r2-implementation_report.json',b'{"head_commit":"new"}')
    fresh=br.snapshot(ctx.store,tgr.RUN);new=br.open_gate(ctx.store,ctx.queue,tgr.RUN,fresh)
    assert old != new and set(ctx.store.state(tgr.RUN).open_gates)=={new}
    ctx.queue.record_decision(new,'approve','will');br.apply_decision(ctx.store,ctx.queue,tgr.RUN)
    assert not ctx.store.state(tgr.RUN).open_gates
