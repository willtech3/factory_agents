"""The run record committed on the implementation branch before the change request is opened.
Uses a local bare 'origin' and a working clone; no network."""
import json
import subprocess

import pytest
from pathlib import Path

from factory.stages.integrate import commit_run_record, run_record_paths

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


def test_record_commit_is_deterministic_and_pushed(tmp_path, examples):
    origin, work = _repo(tmp_path)
    spec = examples["spec_smart_retries"]
    judged = _git(work, "rev-parse", "HEAD")
    merge_verdict = {**examples["merge_readiness"], "_head_sha": judged, "_branch": "factory/run_rec0000001/spec_smart-retries", "_base_sha": "x"}
    sha = commit_run_record(work, RUN, spec, "# Spec\n", examples["verdict_round2"], merge_verdict)
    assert sha != judged and _git(work, "rev-parse", "HEAD") == sha
    paths = run_record_paths(RUN, spec["spec_id"])
    for key, rel in paths.items():
        assert (work / rel).exists(), key
    assert json.loads((work / paths["spec_json"]).read_text())["spec_id"] == spec["spec_id"]
    recorded = json.loads((work / paths["merge_readiness"]).read_text())
    assert "_head_sha" not in recorded and recorded["result"] == examples["merge_readiness"]["result"]
    assert json.loads((work / paths["verdict"]).read_text())["round"] == examples["verdict_round2"]["round"]
    # pushed: origin's branch head equals the new sha
    assert _git(origin, "rev-parse", "refs/heads/factory/run_rec0000001/spec_smart-retries") == sha
    # only .factory/ changed, and the commit identity and date are fixed
    assert _git(work, "diff", "--name-only", f"{judged}..{sha}").splitlines() == sorted(paths.values())
    log = _git(work, "log", "-1", "--format=%an|%ae|%cn|%ci", sha)
    assert log.startswith("factory|factory@localhost|factory|")
    assert judged in _git(work, "log", "-1", "--format=%B", sha)
    # idempotent: the same inputs produce no second commit
    assert commit_run_record(work, RUN, spec, "# Spec\n", examples["verdict_round2"], merge_verdict) == sha
    # deterministic: a fresh clone at the judged head produces the same sha for the same inputs
    work2 = tmp_path / "work2"
    subprocess.run(["git", "clone", "-q", "-b", "factory/run_rec0000001/spec_smart-retries", str(origin), str(work2)], check=True)
    _git(work2, "reset", "-q", "--hard", judged)
    sha2 = commit_run_record(work2, RUN, spec, "# Spec\n", examples["verdict_round2"], merge_verdict, push=False)
    assert sha2 == sha


def test_record_does_not_commit_unrelated_staged_files(tmp_path, examples):
    _, work = _repo(tmp_path)
    judged = _git(work, 'rev-parse', 'HEAD')
    (work / 'unreviewed.py').write_text('x = 99\n')
    _git(work, 'add', 'unreviewed.py')
    verdict = {**examples['merge_readiness'], '_head_sha': judged}
    sha = commit_run_record(work, RUN, examples['spec_smart_retries'], '# Spec\n', None, verdict, push=False)
    assert 'unreviewed.py' not in _git(work, 'diff', '--name-only', judged, sha).splitlines()
    assert _git(work, 'diff', '--cached', '--name-only') == 'unreviewed.py'


def test_moved_branch_must_be_reviewed_again(tmp_path, examples):
    import pytest
    _, work = _repo(tmp_path)
    judged = _git(work, 'rev-parse', 'HEAD')
    (work / 'app.py').write_text("print('changed after judgement')\n")
    _git(work, 'commit', '-am', 'unreviewed change')
    with pytest.raises(RuntimeError, match='changed after'):
        commit_run_record(work, RUN, examples['spec_smart_retries'], '# Spec\n', None,
                          {**examples['merge_readiness'], '_head_sha': judged}, push=False)
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
