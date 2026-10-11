"""Integration retains Factory artifacts outside the target repository.
Uses a local bare 'origin' and a working clone; no network."""
import json
import subprocess

import pytest
from pathlib import Path


RUN = "run_rec0000001"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git(work, "config", "user.email", "dev@example.com"); _git(work, "config", "user.name", "dev")
    (work / "app.py").write_text("print('hi')\n")
    _git(work, "add", "."); _git(work, "commit", "-q", "-m", "init")
    _git(work, "push", "-q", "origin", "HEAD:main")
    _git(work, "checkout", "-q", "-b", "factory/run_rec0000001/spec_smart-retries")
    (work / "retry.py").write_text("x = 1\n")
    _git(work, "add", "."); _git(work, "commit", "-q", "-m", "implement")
    _git(work, "push", "-q", "-u", "origin", "factory/run_rec0000001/spec_smart-retries")
    return origin, work


def test_integration_keeps_artifacts_external_and_candidate_unchanged(tmp_path, root, examples):
    from types import SimpleNamespace
    from test_session_cutoff import _ctx, _result, AutoQueue
    from factory.gates import Decision
    from factory.stages.integrate import run_integrate
    from factory.state import WorkItemStage
    from factory.forge import ChangeRequest
    origin, work = _repo(tmp_path)
    judged = _git(work, 'rev-parse', 'HEAD')
    branch = _git(work, 'branch', '--show-current')
    spec = examples['spec_smart_retries']
    wid = spec['work_item_id']
    def run(**kw):
        summary = json.loads(kw['uploads']['run_summary.json'])
        assert '/api/artifacts/' in summary['artifact_links']['spec_json']
        assert 'run_record_paths' not in summary
        assert json.loads(kw['uploads']['merge_readiness.json'])['_head_sha'] == judged
        kw['custom_tools']['open_change_request']({'source_branch': branch, 'target_branch': 'main',
                                                  'title': 'Reviewed change', 'description_markdown': 'Evidence'}, None)
        return _result({'cr.json': b'{}'})
    ctx = _ctx(tmp_path, root, examples, SimpleNamespace(run=run), AutoQueue(Decision.APPROVE))
    for stage in [WorkItemStage.SPEC_DRAFTING, WorkItemStage.SPEC_JUDGING, WorkItemStage.SPEC_APPROVED,
                  WorkItemStage.IMPLEMENTING, WorkItemStage.REVIEWING, WorkItemStage.MERGE_JUDGING]:
        ctx.event('work_item.stage', {'stage': stage.value}, work_item_id=wid)
    ctx.forge = SimpleNamespace(kind='github', sandbox_git_instructions=lambda *a, **kw: 'read only',
                                session_repository_resource=lambda *a: None,
                                create_change_request=lambda **kw: ChangeRequest(42, 'https://example.test/pull/42', branch, 'main'))
    verifier = SimpleNamespace(checkout=lambda b: work)
    verdict = {**examples['merge_readiness'], '_head_sha': judged, '_branch': branch}
    run_integrate(ctx, spec, verdict, {}, {}, {}, {}, 'main', verifier=verifier)
    assert _git(work, 'rev-parse', 'HEAD') == judged
    assert _git(origin, 'rev-parse', f'refs/heads/{branch}') == judged
    assert _git(work, 'status', '--porcelain') == ''
    assert not (work / '.factory').exists()
    assert ctx.store.get_json(ctx.run_id, 'integrate', f"{spec['spec_id']}-approved-merge_readiness.json") == verdict


def test_moved_branch_must_be_reviewed_again(tmp_path, root, examples):
    from types import SimpleNamespace
    from test_session_cutoff import _ctx, AutoQueue
    from factory.gates import Decision
    from factory.stages.integrate import run_integrate
    from factory.state import WorkItemStage
    _, work = _repo(tmp_path)
    judged = _git(work, 'rev-parse', 'HEAD')
    (work / 'app.py').write_text("print('changed after judgement')\n")
    _git(work, 'commit', '-am', 'unreviewed change')
    ctx = _ctx(tmp_path, root, examples, None, AutoQueue(Decision.APPROVE))
    for stage in [WorkItemStage.SPEC_DRAFTING, WorkItemStage.SPEC_JUDGING, WorkItemStage.SPEC_APPROVED,
                  WorkItemStage.IMPLEMENTING, WorkItemStage.REVIEWING, WorkItemStage.MERGE_JUDGING]:
        ctx.event('work_item.stage', {'stage': stage.value}, work_item_id=examples['spec_smart_retries']['work_item_id'])
    ctx.forge = SimpleNamespace()
    with pytest.raises(RuntimeError, match='changed after'):
        run_integrate(ctx, examples['spec_smart_retries'], {'_branch': 'factory/branch', '_head_sha': judged},
                      {}, {}, {}, {}, 'main', verifier=SimpleNamespace(checkout=lambda b: work))
    assert not (work / '.factory').exists()


def test_change_request_target_is_controller_owned(tmp_path, root, examples):
    from types import SimpleNamespace
    from test_session_cutoff import _ctx, Driver, AutoQueue
    from factory.gates import Decision
    from factory.stages.integrate import make_open_change_request
    ctx = _ctx(tmp_path, root, examples, Driver(examples), AutoQueue(Decision.APPROVE))
    calls = []
    forge = SimpleNamespace(create_change_request=lambda **kw: calls.append(kw))
    handler = make_open_change_request(ctx, forge, 'spec_test', 'wi-test', 'factory/branch', 'main')
    result = json.loads(handler({'source_branch': 'factory/branch', 'target_branch': 'production'}, None))
    assert result == {'error': 'target_branch must be main'} and calls == []


@pytest.mark.parametrize("opens_pr", [False, True])
def test_integrator_uses_forge_receipt_instead_of_agent_report(tmp_path, root, examples, opens_pr):
    import pytest
    from types import SimpleNamespace
    from test_session_cutoff import _ctx, _result, AutoQueue
    from factory.gates import Decision
    from factory.stages.integrate import run_integrate
    from factory.state import WorkItemStage
    spec = examples['spec_smart_retries']
    sid, wid = spec['spec_id'], spec['work_item_id']
    def run(**kw):
        if opens_pr:
            kw['custom_tools']['open_change_request']({'source_branch': 'factory/branch', 'target_branch': 'main',
                                                       'title': 'A reviewed change', 'description_markdown': 'Evidence'}, None)
        return _result({'cr.json': b'{"id": 999, "url": "invented"}'})
    driver = SimpleNamespace(run=run)
    ctx = _ctx(tmp_path, root, examples, driver, AutoQueue(Decision.APPROVE))
    for stage in [WorkItemStage.SPEC_DRAFTING, WorkItemStage.SPEC_JUDGING, WorkItemStage.SPEC_APPROVED,
                  WorkItemStage.IMPLEMENTING, WorkItemStage.REVIEWING, WorkItemStage.MERGE_JUDGING]:
        ctx.event('work_item.stage', {'stage': stage.value}, work_item_id=wid)
    from factory.forge import ChangeRequest
    ctx.forge = SimpleNamespace(kind='github', sandbox_git_instructions=lambda *a, **kw: 'read only',
                                session_repository_resource=lambda *a: None,
                                create_change_request=lambda **kw: ChangeRequest(42, 'https://example.test/pull/42', 'factory/branch', 'main'))
    if opens_pr:
        cr = run_integrate(ctx, spec, {'_branch': 'factory/branch', '_head_sha': 'a'*40}, {}, {}, {}, {}, 'main')
        assert cr['id'] == 42 and cr['url'] == 'https://example.test/pull/42'
    else:
        with pytest.raises(RuntimeError, match='did not open a change request'):
            run_integrate(ctx, spec, {'_branch': 'factory/branch', '_head_sha': 'a'*40}, {}, {}, {}, {}, 'main')
    state = ctx.store.state(ctx.run_id)
    assert state.work_items[wid].cr_id == (42 if opens_pr else None)
    assert state.list_cost_usd == .5


def test_evaluation_pr_is_forced_to_draft_and_labeled(tmp_path, root, examples):
    from types import SimpleNamespace
    from test_session_cutoff import _ctx, Driver, AutoQueue
    from factory.gates import Decision
    from factory.stages.integrate import make_open_change_request
    from factory.forge import ChangeRequest
    ctx = _ctx(tmp_path, root, examples, Driver(examples), AutoQueue(Decision.APPROVE))
    ctx.evaluation_target = ('evaluation', 'a' * 40)
    calls = []
    def create(**kw):
        calls.append(kw)
        return ChangeRequest(42, 'https://example.test/pull/42', 'factory/branch', 'evaluation')
    handler = make_open_change_request(ctx, SimpleNamespace(kind='github', create_change_request=create),
                                      'spec_test', 'wi-test', 'factory/branch', 'evaluation')
    handler({'source_branch': 'factory/branch', 'target_branch': 'evaluation', 'title': 'Change',
             'description_markdown': 'Evidence', 'draft': False}, None)
    assert calls[0]['draft'] is True
    assert calls[0]['title'].startswith('[Factory evaluation]')
    assert 'do not merge' in calls[0]['description']
    assert 'a' * 40 in calls[0]['description']
