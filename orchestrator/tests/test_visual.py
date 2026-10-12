import base64
import json
import subprocess

from fastapi.testclient import TestClient

from conftest import EXAMPLES, make_config
from factory import checks, visual
from factory.stages.implement import LocalVerifier
from factory.web.app import create_app
from support.scripted_run import ScriptedRun

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/k9sAAAAASUVORK5CYII=')


def entry():
    return {'command': 'factory:browser', 'target': 'preview', 'description': 'Select a model', 'covers_ac': ['AC-1'],
            'visual': {'persona': 'Chat member', 'viewport': {'width': 1280, 'height': 720},
                       'steps': ['Open picker', 'Select model'],
                       'environment': {'name': 'Isolated preview', 'data': 'synthetic', 'authentication': 'test-identity'}}}


def report(directory, request, **changes):
    (directory / 'screen.png').write_bytes(PNG)
    data = {'request_id': request['id'], 'head_sha': request['head_sha'], 'status': 'passed',
            'runner': 'fixture worker (unit test only)', 'summary': 'The model selected is visible',
            'steps': [{'status': 'passed', 'observation': 'Picker opened'}, {'status': 'passed', 'observation': 'Selection visible'}],
            'media': [{'file': 'screen.png', 'role': 'screenshot', 'caption': 'Selection'}], **changes}
    (directory / 'result.json').write_text(json.dumps(data))


def test_ui_coverage_cannot_be_replaced_by_curl():
    spec = {'acceptance_criteria': [{'ac_id': 'AC-1', 'user_visible': True, 'surface': 'ui'}],
            'test_plan': {'e2e': [{k: v for k, v in entry().items() if k != 'visual'}]}}
    assert not checks.e2e_coverage(spec).passed
    spec['test_plan']['e2e'] = [entry()]
    assert checks.e2e_coverage(spec).passed
    spec['test_plan']['e2e'][0]['target'] = 'local'
    assert not checks.e2e_coverage(spec).passed


def test_missing_stale_and_failed_visual_evidence_block_pass(tmp_path):
    directory, request = visual.new_request(tmp_path, tmp_path, 'a' * 40, 'http://localhost:9999', entry())
    assert visual.collect(directory, request, 0)['status'] == 'blocked'
    report(directory, request, head_sha='b' * 40)
    assert visual.collect(directory, request, 0)['status'] == 'blocked'
    report(directory, request)
    assert visual.collect(directory, request, 0)['status'] == 'passed'
    assert visual.collect(directory, request, 1)['status'] == 'blocked'
    report(directory, request, media=[])
    assert visual.collect(directory, request, 0)['status'] == 'blocked'
    report(directory, request, steps=[{'status': 'passed', 'observation': 'only one'}])
    assert visual.collect(directory, request, 0)['status'] == 'blocked'
    row = {'command': 'visual', 'target': 'preview', 'exit_code': 0, 'visual': {'status': 'blocked'}}
    assert not checks.e2e_passed([row]).passed


def test_visual_media_outside_the_evidence_directory_is_rejected(tmp_path):
    directory, request = visual.new_request(tmp_path, tmp_path, 'a' * 40, 'http://localhost:9999', entry())
    (directory.parent / 'outside.png').write_bytes(PNG)              # a real image, just not in the evidence directory
    for name in ('../outside.png', str(directory.parent / 'outside.png')):
        report(directory, request, media=[{'file': name, 'role': 'screenshot'}])
        assert visual.collect(directory, request, 0)['status'] == 'blocked', name


def test_visual_media_must_be_an_image_or_video_by_suffix_and_signature(tmp_path):
    directory, request = visual.new_request(tmp_path, tmp_path, 'a' * 40, 'http://localhost:9999', entry())
    (directory / 'screen.html').write_text('<script>alert(1)</script>')
    (directory / 'fake.png').write_bytes(b'<svg onload="alert(1)"></svg>')     # an image suffix, not an image
    for name in ('screen.html', 'fake.png'):
        report(directory, request, media=[{'file': name, 'role': 'screenshot'}])
        assert visual.collect(directory, request, 0)['status'] == 'blocked', name
    report(directory, request)                                         # the same report with a real PNG passes
    assert visual.collect(directory, request, 0)['status'] == 'passed'


def test_symlink_and_oversized_media_rejected(tmp_path, monkeypatch):
    directory, request = visual.new_request(tmp_path, tmp_path, 'a' * 40, 'http://localhost:9999', entry())
    report(directory, request)
    monkeypatch.setattr(visual, 'MAX_FILE', 3)
    assert visual.collect(directory, request, 0)['status'] == 'blocked'
    monkeypatch.setattr(visual, 'MAX_FILE', 9999)
    (directory / 'screen.png').unlink()
    (tmp_path / 'outside.png').write_bytes(PNG)
    (directory / 'screen.png').symlink_to(tmp_path / 'outside.png')
    assert visual.collect(directory, request, 0)['status'] == 'blocked'


def test_visual_command_gets_unique_controller_request_and_missing_environment_blocks(tmp_path):
    repo = tmp_path / 'repo'; repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '--allow-empty', '-qm', 'init'], cwd=repo, check=True)
    e = entry(); e['command'] = 'exit 0'
    e['visual']['environment']['required_env'] = ['FACTORY_TEST_MISSING_ENV_123']
    v = LocalVerifier('unused', tmp_path / 'verify')
    rows = v.run_e2e('http://localhost:9999', [e], repo)
    assert rows[0]['exit_code'] is None and rows[0]['visual']['status'] == 'blocked'
    assert 'FACTORY_TEST_MISSING_ENV_123' in rows[0]['visual']['summary']
    e['visual']['environment'].pop('required_env')
    rows = v.run_e2e('http://localhost:9999', [e], repo)
    assert rows[0]['exit_code'] == 0 and rows[0]['visual']['status'] == 'blocked'
    assert len(list((v.workdir / 'visual').glob('*/request.json'))) == 2


def test_external_worker_timeout_is_not_passed(tmp_path):
    result = visual.wait_for_browser(tmp_path, 0)
    assert result['exit_code'] is None and (tmp_path / 'closed').exists()


def test_binary_artifacts_support_range_and_block_symlinks(tmp_path, root):
    cfg = make_config(root, tmp_path / 'runs')
    app = create_app(cfg)
    client = TestClient(app)
    store = app.state.console.store
    media = b'\x00\x00\x00\x18ftypmp42' + b'0' * 50
    store.put_artifact('run_test', 'visual', 'demo.mp4', media)
    r = client.get('/api/artifacts/run_test/visual/demo.mp4', headers={'Range': 'bytes=0-9'})
    assert r.status_code == 206 and r.content == media[:10] and r.headers['content-type'] == 'video/mp4'
    p = store.artifact_path('run_test', 'visual', 'secret.png')
    (tmp_path / 'outside.png').write_bytes(PNG)
    p.symlink_to(tmp_path / 'outside.png')
    assert client.get('/api/artifacts/run_test/visual/secret.png').status_code == 404


def test_old_visual_evidence_becomes_stale_when_new_head_verification_starts(tmp_path, root):
    cfg = make_config(root, tmp_path / 'runs')
    app = create_app(cfg)
    run_id = ScriptedRun(cfg.runs_dir, cfg.schemas_dir, EXAMPLES).seed_example()
    store = app.state.console.store
    store.put_artifact(run_id, 'visual', 'evidence.json', json.dumps({'head_sha': 'old', 'status': 'passed', 'media': []}).encode())
    store.append(run_id, 'visual.verified', {'type': 'orchestrator'}, {'path': 'visual/evidence.json', 'head_sha': 'old'}, spec_id='spec_test')
    assert app.state.console.run_detail(run_id)['visual_evidence'][0]['current']
    store.append(run_id, 'verification.started', {'type': 'orchestrator'}, {'head_sha': 'new'}, spec_id='spec_test')
    assert not app.state.console.run_detail(run_id)['visual_evidence'][0]['current']


def test_omitted_visual_results_cannot_pass():
    spec = {'test_plan': {'e2e': [entry()]}}
    assert not checks.visual_passed(spec, []).passed
    row = {**entry(), 'exit_code': 0}
    assert not checks.visual_passed(spec, [row]).passed
    row['visual'] = {**entry()['visual'], 'status': 'passed'}
    assert checks.visual_passed(spec, [row]).passed


def test_desktop_evidence_cannot_satisfy_a_missing_mobile_check():
    desktop = entry(); mobile = entry()
    mobile['visual']['viewport'] = {'width': 390, 'height': 844}
    spec = {'test_plan': {'e2e': [desktop, mobile]}}
    row = {**desktop, 'exit_code': 0, 'visual': {**desktop['visual'], 'status': 'passed'}}
    assert not checks.visual_passed(spec, [row]).passed
    mobile_row = {**mobile, 'exit_code': 0, 'visual': {**mobile['visual'], 'status': 'passed'}}
    assert checks.visual_passed(spec, [row, mobile_row]).passed
