"""`factory resume`: a run that died during implementation continues from what the store holds. No network."""
import json

import pytest

from conftest import make_config

from factory.gates import InMemoryQueue, LogNotifier
from factory import steps
from factory.stages import RunContext
from factory.stages.implement import adopt_rounds
from factory.state import IllegalTransition, Policy, RunStage, RunState, reduce
from factory.state import RunEvent
from factory.store import RunStore


def _ctx(tmp_path, root):
    return RunContext(cfg=make_config(root, tmp_path), store=RunStore(tmp_path), driver=None, queue=InMemoryQueue(),
                      notifier=LogNotifier(), forge=None, policy=Policy(), run_id="run_resume00001")


def _put(ctx, stage, name, obj):
    ctx.store.put_artifact(ctx.run_id, stage, name, json.dumps(obj).encode())


def test_an_older_controllers_rounds_are_adopted_where_their_artefacts_stop(tmp_path, root):
    """A run written before round steps existed: its persisted rounds become steps (ADR-0013), so the loop continues
    where they stop instead of running the implementer again."""
    ctx = _ctx(tmp_path, root)

    def spec(sid):
        return {"spec_id": sid, "test_plan": {"commands": [], "e2e": []}}

    def step(key):
        return ctx.store.step(ctx.run_id, key)
    # spec_a: nothing persisted -> nothing adopted, round 1 runs its implementer
    # spec_b: round 1 report only -> that report is kept (verification and review run again)
    _put(ctx, "implement", "spec_b-r1-implementation_report.json", {"head_commit": "b1"})
    # spec_c: round 2 report + response and round 1 findings -> round 1 answered, round 2's report kept (live run 2's shape)
    _put(ctx, "implement", "spec_c-r1-implementation_report.json", {"head_commit": "c1"})
    _put(ctx, "review", "spec_c-r1-findings.merged.json", {"findings": [{"finding_id": "F-1", "severity": "blocking", "status": "open"}]})
    _put(ctx, "implement", "spec_c-r2-implementation_report.json", {"head_commit": "c2"})
    _put(ctx, "implement", "spec_c-r2-review_response.json", {"responses": [{"finding_id": "F-1", "disposition": "fixed"}]})
    # spec_d: round 2 report but no response -> round 2's implementer runs again against round 1's findings
    _put(ctx, "implement", "spec_d-r1-implementation_report.json", {"head_commit": "d1"})
    _put(ctx, "review", "spec_d-r1-findings.merged.json", {"findings": []})
    _put(ctx, "implement", "spec_d-r2-implementation_report.json", {"head_commit": "d2"})
    for sid in ("spec_a", "spec_b", "spec_c", "spec_d"):
        adopt_rounds(ctx, spec(sid))

    assert not [s for s in ctx.store.steps(ctx.run_id) if "/spec_a/" in s.key]
    b = step("implement/spec_b/r1/author")
    assert b.status == "done" and steps.get_json(ctx, b.result["report"]) == {"head_commit": "b1"} and step("implement/spec_b/r1") is None
    c1, c2 = step("implement/spec_c/r1"), step("implement/spec_c/r2/author")
    assert c1.result["next"] == "respond" and steps.get_json(ctx, c1.result["findings"])["findings"][0]["finding_id"] == "F-1"
    assert steps.get_json(ctx, c2.result["report"]) == {"head_commit": "c2"}
    assert steps.get_json(ctx, c2.result["response"])["responses"][0]["disposition"] == "fixed"
    assert step("implement/spec_d/r1").result["next"] == "respond" and step("implement/spec_d/r2/author") is None
    adopt_rounds(ctx, spec("spec_c"))                              # once: a run with round steps adopts nothing more
    assert len([s for s in ctx.store.steps(ctx.run_id) if "/spec_c/" in s.key]) == 2
    # a round-2 report with no round-1 findings to have answered is a store the loop cannot continue from
    _put(ctx, "implement", "spec_e-r2-implementation_report.json", {"head_commit": "e2"})
    with pytest.raises(RuntimeError, match="round 1 has no merged findings"):
        adopt_rounds(ctx, spec("spec_e"))


def test_run_resumed_moves_a_failed_run_back_into_implementation():
    st = RunState(run_id="r")
    seq = [0]

    def ev(kind, payload):
        e = RunEvent(run_id="r", seq=seq[0], kind=kind, actor={"type": "system"}, payload=payload)
        seq[0] += 1
        reduce(st, e)

    ev("run.created", {"policy": {"preview_mode": "local"}, "budget_usd": 50, "outcome_id": "o"})
    ev("stage.entered", {"stage": "decomposing"})
    ev("stage.entered", {"stage": "gate_1_decomposition"})
    ev("stage.entered", {"stage": "specifying"})
    ev("stage.entered", {"stage": "gate_2_spec_readiness"})
    ev("stage.entered", {"stage": "implementing"})
    ev("run.failed", {"reason": "boom"})
    assert st.stage == RunStage.FAILED
    ev("run.resumed", {"stage": "implementing"})
    assert st.stage == RunStage.IMPLEMENTING
    ev("run.resumed", {"stage": "implementing"})        # killed before run.failed: a no-op, not an illegal self-transition
    assert st.stage == RunStage.IMPLEMENTING
    ev("run.completed", {})
    try:
        ev("run.resumed", {"stage": "implementing"})
    except IllegalTransition:
        pass
    else:
        raise AssertionError("a completed run must not resume")


def test_resume_review_cache_is_bound_to_both_base_and_head(tmp_path, root):
    from factory.stages.implement import review_evidence_prefix
    ctx = _ctx(tmp_path, root)
    original = {"diff_ref": {"base_sha": "base1", "head_sha": "head1"}, "findings": []}
    prefix, found = review_evidence_prefix(ctx, "spec_a", 2, "p1", "base1", "head1")
    assert prefix == "spec_a-r2-basebase1-headhead1-p1-" and found is None
    _put(ctx, "review", prefix + "findings.json", original)
    assert review_evidence_prefix(ctx, "spec_a", 2, "p1", "base1", "head1") == (prefix, original)
    for base, head in [("base2", "head1"), ("base1", "head2")]:
        other, found = review_evidence_prefix(ctx, "spec_a", 2, "p1", base, head)
        assert found is None and other != prefix
    # a pass saved under this diff's name that reports reading another head is not reused
    _put(ctx, "review", "spec_a-r3-basebase1-headhead1-p1-findings.json", {"diff_ref": {"base_sha": "base1", "head_sha": "moved"}})
    assert review_evidence_prefix(ctx, "spec_a", 3, "p1", "base1", "head1")[1] is None
    assert ctx.store.get_json(ctx.run_id, "review", prefix + "findings.json") == original


def test_reentering_the_same_stage_is_a_no_op_not_an_illegal_transition():
    """Live greenfield run: the resume re-emitted work_item.stage reviewing while the item was already reviewing,
    and every replay (the Console, the integrate step's cost lookup) raised IllegalTransition."""
    from factory.state import WorkItemStage
    st = RunState(run_id="r"); seq = [0]

    def ev(kind, payload, **extra):
        e = RunEvent(run_id="r", seq=seq[0], kind=kind, actor={"type": "system"}, payload=payload, **extra); seq[0] += 1; reduce(st, e)

    ev("run.created", {"policy": {"preview_mode": "local"}, "budget_usd": 50, "outcome_id": "o"})
    ev("stage.entered", {"stage": "decomposing"})
    ev("artifact.produced", {"artifact_type": "decomposition", "revision": 1, "work_items": [{"work_item_id": "wi-a"}]})
    for s_ in ("spec_drafting", "spec_judging", "spec_approved", "implementing", "reviewing"):
        ev("work_item.stage", {"stage": s_}, work_item_id="wi-a")
    ev("work_item.stage", {"stage": "reviewing"}, work_item_id="wi-a")      # again: no-op
    assert st.work_items["wi-a"].stage == WorkItemStage.REVIEWING
    ev("stage.entered", {"stage": "decomposing"})                           # run-level same stage: no-op too
    assert st.stage == RunStage.DECOMPOSING


def test_evaluation_target_is_pinned_and_survives_resume(tmp_path, root):
    import subprocess
    import pytest
    from types import SimpleNamespace
    from factory.runner import comparison_branch
    ctx = _ctx(tmp_path, root)
    ctx.event('run.created', {'outcome_id': 'o', 'budget_usd': 50})
    work = tmp_path / 'repo'
    work.mkdir()
    def git(*args):
        return subprocess.run(['git', *args], cwd=work, check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-q', '-b', 'evaluation')
    git('config', 'user.email', 'test@example.test')
    git('config', 'user.name', 'test')
    (work / 'app.txt').write_text('baseline')
    git('add', '.')
    git('commit', '-qm', 'baseline')
    original = git('rev-parse', 'HEAD')
    verifier = SimpleNamespace(checkout=lambda branch: work)
    ctx.evaluation_target = ('evaluation', original)
    assert comparison_branch(ctx, {'default_branch': 'main'}, verifier) == 'evaluation'
    ctx.evaluation_target = None  # next controller inherits the recorded instruction
    assert comparison_branch(ctx, {'default_branch': 'main'}, verifier) == 'evaluation'
    (work / 'app.txt').write_text('moved')
    git('commit', '-qam', 'moved')
    with pytest.raises(RuntimeError, match='pinned'):
        comparison_branch(ctx, {'default_branch': 'main'}, verifier)
    ctx.evaluation_target = ('main', original)
    with pytest.raises(RuntimeError, match='differs'):
        comparison_branch(ctx, {'default_branch': 'main'}, verifier)
