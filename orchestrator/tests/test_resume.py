"""`factory resume`: a run that died during implementation continues from what the store holds. No network."""
import json

from conftest import make_config

from factory.gates import InMemoryQueue, LogNotifier
from factory.runner import resume_points
from factory.stages import RunContext
from factory.state import IllegalTransition, Policy, RunStage, RunState, reduce
from factory.state import RunEvent
from factory.store import RunStore


def _ctx(tmp_path, root):
    return RunContext(cfg=make_config(root, tmp_path), store=RunStore(tmp_path), driver=None, queue=InMemoryQueue(),
                      notifier=LogNotifier(), forge=None, policy=Policy(), run_id="run_resume00001")


def _put(ctx, stage, name, obj):
    ctx.store.put_artifact(ctx.run_id, stage, name, json.dumps(obj).encode())


def test_resume_points_follow_the_persisted_rounds(tmp_path, root):
    ctx = _ctx(tmp_path, root)
    specs = {"spec_a": {}, "spec_b": {}, "spec_c": {}, "spec_d": {}}
    # spec_a: nothing persisted -> round 1, implementer runs
    # spec_b: round 1 report only -> round 1 with the report (verification, then review)
    _put(ctx, "implement", "spec_b-r1-implementation_report.json", {"head_commit": "b1"})
    # spec_c: round 2 report + response and round 1 findings -> round 2 with everything (live run 2's shape)
    _put(ctx, "implement", "spec_c-r1-implementation_report.json", {"head_commit": "c1"})
    _put(ctx, "review", "spec_c-r1-findings.merged.json", {"findings": [{"finding_id": "F-1"}]})
    _put(ctx, "implement", "spec_c-r2-implementation_report.json", {"head_commit": "c2"})
    _put(ctx, "implement", "spec_c-r2-review_response.json", {"responses": [{"finding_id": "F-1", "disposition": "fixed"}]})
    # spec_d: round 2 report but no response -> rerun round 2 against round 1's findings
    _put(ctx, "implement", "spec_d-r1-implementation_report.json", {"head_commit": "d1"})
    _put(ctx, "review", "spec_d-r1-findings.merged.json", {"findings": []})
    _put(ctx, "implement", "spec_d-r2-implementation_report.json", {"head_commit": "d2"})

    pts = resume_points(ctx, specs)
    assert pts["spec_a"].round_no == 1 and pts["spec_a"].report is None
    assert pts["spec_b"].round_no == 1 and pts["spec_b"].report == {"head_commit": "b1"} and pts["spec_b"].findings is None
    c = pts["spec_c"]
    assert c.round_no == 2 and c.report == {"head_commit": "c2"} and c.response["responses"][0]["disposition"] == "fixed"
    assert c.findings["findings"][0]["finding_id"] == "F-1"
    d = pts["spec_d"]
    assert d.round_no == 2 and d.report is None and d.findings == {"findings": []}


def test_run_resumed_moves_a_failed_run_back_into_implementation():
    st = RunState(run_id="r")
    seq = [0]

    def ev(kind, payload):
        e = RunEvent(run_id="r", seq=seq[0], kind=kind, actor={"type": "system"}, payload=payload)
        seq[0] += 1
        reduce(st, e)

    ev("run.created", {"outcome_id": "o"})
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
    _put(ctx, "review", "spec_a-r2-p1-findings.json", original)
    prefix, found = review_evidence_prefix(ctx, "spec_a", 2, "p1", "base1", "head1")
    assert prefix == "spec_a-r2-p1-" and found == original
    for base, head in [("base2", "head1"), ("base1", "head2")]:
        prefix, found = review_evidence_prefix(ctx, "spec_a", 2, "p1", base, head)
        assert found is None and prefix != "spec_a-r2-p1-"
        fresh = {"diff_ref": {"base_sha": base, "head_sha": head}, "findings": []}
        _put(ctx, "review", prefix + "findings.json", fresh)
        assert review_evidence_prefix(ctx, "spec_a", 2, "p1", base, head) == (prefix, fresh)
    assert ctx.store.get_json(ctx.run_id, "review", "spec_a-r2-p1-findings.json") == original


def test_reentering_the_same_stage_is_a_no_op_not_an_illegal_transition():
    """Live greenfield run: the resume re-emitted work_item.stage reviewing while the item was already reviewing,
    and every replay (the Console, the integrate step's cost lookup) raised IllegalTransition."""
    from factory.state import WorkItemStage
    st = RunState(run_id="r"); seq = [0]

    def ev(kind, payload, **extra):
        e = RunEvent(run_id="r", seq=seq[0], kind=kind, actor={"type": "system"}, payload=payload, **extra); seq[0] += 1; reduce(st, e)

    ev("run.created", {"outcome_id": "o"})
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
    ctx.event('run.created', {'outcome_id': 'o'})
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
