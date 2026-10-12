"""Integration retains Factory artifacts outside the target repository.
Uses a local bare 'origin' and a working clone; no network."""
import json
import subprocess

import pytest
from pathlib import Path

from support.events import assert_log_valid


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


def _passthrough(kw, description=None):
    """An integrator that sends the orchestrator's draft (or `description`) with the facts it was given."""
    from support.sessions import stage_result
    facts = json.loads(kw['uploads']['pr_facts.json'])
    answer = kw['custom_tools']['open_change_request']({
        'source_branch': facts['source_branch'], 'target_branch': facts['target_branch'], 'title': facts['title'],
        'description_markdown': description if description is not None else kw['uploads']['pr_description.md'].decode(),
        'labels': facts['labels'], 'reviewers': facts['reviewers'], 'draft': facts['draft']}, None)
    return stage_result({'cr.json': answer.encode()})


def test_integration_keeps_artifacts_external_and_candidate_unchanged(tmp_path, root, examples):
    from types import SimpleNamespace
    from support.sessions import AutoQueue, run_after_gate_1
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
        uploads = kw['uploads']
        assert 'run_summary.json' not in uploads and 'findings.json' not in uploads   # the evidence goes to the run record
        assert json.loads(uploads['merge_readiness.json'])['_head_sha'] == judged
        assert json.loads(uploads['pr_lint.json']) == {'problems': []}
        return _passthrough(kw)
    ctx = run_after_gate_1(tmp_path, root, examples, SimpleNamespace(run=run), AutoQueue(Decision.APPROVE))
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
    record = ctx.store.get_json(ctx.run_id, 'integrate', f"{spec['spec_id']}-run_record.json")
    assert '/api/artifacts/' in record['artifact_links']['spec_json'] and record['head_sha'] == judged
    assert record['pull_request']['url'] == 'https://example.test/pull/42'
    assert record['size']['diff']['files'] == [{'path': 'retry.py', 'added': 1, 'removed': 0}]
    assert record['size']['measured']['hand_written'] == 1 and record['size']['flag'] is None
    opened = ctx.store.get_artifact(ctx.run_id, 'integrate', f"{spec['spec_id']}-pr_description.md").decode()
    assert '1 hand-written lines (app 1, tests 0); no generated files. Estimated 320 (M;' in opened and f"#record-{spec['spec_id']}" in opened
    assert_log_valid(ctx.store, ctx.run_id, root / "schemas")                       # cr.opened included


def test_moved_branch_must_be_reviewed_again(tmp_path, root, examples):
    from types import SimpleNamespace
    from support.sessions import AutoQueue, run_after_gate_1
    from factory.gates import Decision
    from factory.stages.integrate import run_integrate
    from factory.state import WorkItemStage
    _, work = _repo(tmp_path)
    judged = _git(work, 'rev-parse', 'HEAD')
    (work / 'app.py').write_text("print('changed after judgement')\n")
    _git(work, 'commit', '-am', 'unreviewed change')
    ctx = run_after_gate_1(tmp_path, root, examples, None, AutoQueue(Decision.APPROVE))
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
    from support.sessions import AutoQueue, SpecStageDriver, run_after_gate_1
    from factory.gates import Decision
    from factory.stages.integrate import make_open_change_request
    ctx = run_after_gate_1(tmp_path, root, examples, SpecStageDriver(examples), AutoQueue(Decision.APPROVE))
    calls = []
    forge = SimpleNamespace(create_change_request=lambda **kw: calls.append(kw))
    handler = make_open_change_request(ctx, forge, 'spec_test', 'wi-test', 'factory/branch', 'main')
    result = json.loads(handler({'source_branch': 'factory/branch', 'target_branch': 'production'}, None))
    assert result == {'error': 'target_branch must be main'} and calls == []


@pytest.mark.parametrize("opens_pr", [False, True])
def test_integrator_uses_forge_receipt_instead_of_agent_report(tmp_path, root, examples, opens_pr, monkeypatch):
    """When the draft needs rewording the integrator runs, and only the forge's receipt proves a PR exists."""
    import pytest
    from types import SimpleNamespace
    from support.sessions import AutoQueue, run_after_gate_1, stage_result
    from factory import pr_description
    from factory.gates import Decision
    from factory.stages.integrate import run_integrate
    from factory.state import WorkItemStage
    spec = examples['spec_smart_retries']
    wid = spec['work_item_id']
    lints = []
    real_lint = pr_description.lint
    def lint(*a, **kw):          # the rendered draft needs a rewording; what the integrator sends passes
        lints.append(1)
        return ["line 3 names a factory id"] if len(lints) == 1 else real_lint(*a, **kw)
    monkeypatch.setattr(pr_description, "lint", lint)
    def run(**kw):
        assert json.loads(kw['uploads']['pr_lint.json'])['problems'] == ["line 3 names a factory id"]
        if opens_pr:
            _passthrough(kw)
        return stage_result({'cr.json': b'{"id": 999, "url": "invented"}'})
    driver = SimpleNamespace(run=run)
    ctx = run_after_gate_1(tmp_path, root, examples, driver, AutoQueue(Decision.APPROVE))
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


def test_a_clean_description_opens_the_change_request_without_an_agent(tmp_path, root, examples):
    from types import SimpleNamespace
    from test_session_cutoff import _ctx, AutoQueue
    from factory.forge import ChangeRequest
    from factory.gates import Decision
    from factory.stages.integrate import run_integrate
    from factory.state import WorkItemStage
    spec = examples['spec_smart_retries']
    sid, wid = spec['spec_id'], spec['work_item_id']
    def run(**kw):
        raise AssertionError("no integrator session when the rendered description passes its lint")
    ctx = _ctx(tmp_path, root, examples, SimpleNamespace(run=run), AutoQueue(Decision.APPROVE))
    for stage in [WorkItemStage.SPEC_DRAFTING, WorkItemStage.SPEC_JUDGING, WorkItemStage.SPEC_APPROVED,
                  WorkItemStage.IMPLEMENTING, WorkItemStage.REVIEWING, WorkItemStage.MERGE_JUDGING]:
        ctx.event('work_item.stage', {'stage': stage.value}, work_item_id=wid)
    opened = []
    ctx.forge = SimpleNamespace(kind='github', create_change_request=lambda **kw: opened.append(kw) or
                                ChangeRequest(42, 'https://example.test/pull/42', 'factory/branch', 'main'))
    cr = run_integrate(ctx, spec, {'_branch': 'factory/branch', '_head_sha': 'a'*40}, {}, {}, {}, {"reviewers": ["dana"]}, 'main')
    assert cr['id'] == 42 and len(opened) == 1
    assert opened[0]['source_branch'] == 'factory/branch' and opened[0]['target_branch'] == 'main'
    assert opened[0]['reviewers'] == ['dana'] and 'factory' in opened[0]['labels']
    assert opened[0]['description'] == ctx.store.get_artifact(ctx.run_id, 'integrate', f'{sid}-pr_description.md').decode()
    state = ctx.store.state(ctx.run_id)
    assert state.work_items[wid].cr_id == 42 and state.list_cost_usd == 0 and state.sessions_finished == 0
    assert json.loads(ctx.store.get_artifact(ctx.run_id, 'integrate', f'{sid}-cr.json'))['url'] == 'https://example.test/pull/42'


def test_evaluation_pr_is_forced_to_draft_and_labeled(tmp_path, root, examples):
    from types import SimpleNamespace
    from support.sessions import AutoQueue, SpecStageDriver, run_after_gate_1
    from factory.gates import Decision
    from factory.stages.integrate import evaluation_banner, make_open_change_request
    from factory.forge import ChangeRequest
    ctx = run_after_gate_1(tmp_path, root, examples, SpecStageDriver(examples), AutoQueue(Decision.APPROVE))
    ctx.evaluation_target = ('evaluation', 'a' * 40)
    calls = []
    def create(**kw):
        calls.append(kw)
        return ChangeRequest(42, 'https://example.test/pull/42', 'factory/branch', 'evaluation')
    beyond = {'files': [{'path': 'verification/x.py', 'added': 12, 'removed': 3}], 'added': 12, 'removed': 3}
    banner = evaluation_banner('a' * 40, 'main', beyond)
    handler = make_open_change_request(ctx, SimpleNamespace(kind='github', create_change_request=create),
                                      'spec_test', 'wi-test', 'factory/branch', 'evaluation', banner=banner)
    handler({'source_branch': 'factory/branch', 'target_branch': 'evaluation', 'title': 'Change',
             'description_markdown': 'Evidence', 'draft': False, 'labels': ['ui']}, None)
    assert calls[0]['draft'] is True
    assert calls[0]['title'].startswith('[Factory evaluation]')
    # the controller's labels go on whatever the integrator asked for, so the watcher and people can find factory PRs
    assert calls[0]['labels'] == sorted({'ui', 'factory', 'spec:spec_test', f'run:{ctx.run_id}'})
    first, second = calls[0]['description'].splitlines()[:2]
    assert 'do not merge' in first and 'a' * 10 in first and first.count('. ') == 0      # one sentence
    assert second == "> Beyond the current `main` it holds 1 file, +12 / −3: `verification/x.py` (+12 / −3)."
    assert calls[0]['description'].endswith('Evidence')


def test_open_change_request_refuses_a_description_written_for_the_factory(tmp_path, root, examples):
    """Post-mortem 2026-10-11 §4: ids, factory vocabulary, costs, first-person judge text, local links and tables are
    refused with the reasons, and nothing reaches the forge until the integrator sends a description that passes."""
    from types import SimpleNamespace
    from support.sessions import AutoQueue, SpecStageDriver, run_after_gate_1
    from factory.gates import Decision
    from factory.stages.integrate import make_open_change_request
    from factory.forge import ChangeRequest
    ctx = run_after_gate_1(tmp_path, root, examples, SpecStageDriver(examples), AutoQueue(Decision.APPROVE))
    calls = []
    forge = SimpleNamespace(kind='github', create_change_request=lambda **kw: calls.append(kw) or ChangeRequest(7, 'u', 'b', 'main'))
    url = 'http://127.0.0.1:8772/#/runs/run_budget000001#record-spec_test'
    handler = make_open_change_request(ctx, forge, 'spec_test', 'wi-test', 'b', 'main', record_url=url)
    bad = ("## Summary\nI re-ran AC-9 and the merge judge settled F-7 in round 2 for $2.08.\n"
           "| AC | Judge |\n|---|---|\n[results](http://127.0.0.1:8772/api/artifacts/run_budget000001/implement/x.json)\n")
    answer = json.loads(handler({'source_branch': 'b', 'target_branch': 'main', 'title': 'spec_test: Change',
                                 'description_markdown': bad}, None))
    joined = ' '.join(answer['problems'])
    for expected in ('AC-9', 'F-7', 'merge judge', 'round', '$2', 'first-person', 'tables', '127.0.0.1', 'run record exactly once',
                     '`## Needs your decision`', 'spec id'):
        assert expected in joined, expected
    assert calls == [] and not any(ev.kind == 'cr.opened' for ev in ctx.store.events(ctx.run_id))
