import base64
import json
import os
import subprocess
import time

import pytest
from typer.testing import CliRunner

from factory import browser_worker as bw
from factory import checks, visual
from factory.cli import app

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/k9sAAAAASUVORK5CYII=')
STEPS = ['Open picker', 'Select model']


def entry(width=1280, height=720):
    return {'command': 'factory:browser', 'target': 'preview', 'description': 'Select a model', 'covers_ac': ['AC-1'],
            'visual': {'persona': 'Chat member', 'viewport': {'width': width, 'height': height}, 'steps': list(STEPS),
                       'environment': {'name': 'Loopback', 'data': 'synthetic', 'authentication': 'none-required'}}}


def git(repo, *args):
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=T', '-c', 'user.email=t@localhost', *args], check=True,
                   capture_output=True)


@pytest.fixture
def job(tmp_path):
    """A clean checkout (with ignored build/ output) and one open controller request for its HEAD."""
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q')
    (repo / 'app.js').write_text('render()\n')
    (repo / '.gitignore').write_text('build/\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'app')
    head = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
    runs = tmp_path / 'runs'
    directory, request = visual.new_request(runs / 'run_x' / 'verify', repo, head, 'http://127.0.0.1:5173/', entry())
    return {'repo': repo, 'runs': runs, 'dir': directory, 'request': request, 'shots': tmp_path / 'shots'}


def shot(job, name='s.png', data=PNG):
    job['shots'].mkdir(exist_ok=True)
    p = job['shots'] / name
    p.write_bytes(data)
    return str(p)


def draft(job, **changes):
    r = job['request']
    base = {'request_id': r['id'], 'head_sha': r['head_sha'], 'status': 'passed', 'summary': 'Model selected and visible',
            'viewport_observed': {'width': 1280, 'height': 720},
            'steps': [{'status': 'passed', 'observation': 'Picker open'}, {'status': 'passed', 'observation': 'Model shown'}],
            'media': [{'path': shot(job, 'one.png'), 'role': 'screenshot', 'step': 1, 'caption': 'Picker'},
                      {'path': shot(job, 'two.png'), 'role': 'screenshot', 'step': 2, 'caption': 'Selected'}]}
    return {**base, **changes}


def published(job):
    return sorted(p.name for p in job['dir'].iterdir())


def test_claim_then_submit_reaches_a_controller_pass(job):
    bw.claim(job['dir'])
    verdict = bw.submit(job['dir'], draft(job))
    assert verdict['status'] == 'passed'
    assert published(job) == ['01-screenshot.png', '02-screenshot.png', 'claim.json', 'request.json', 'result.json']
    outcome = visual.wait_for_browser(job['dir'], 1)
    evidence = visual.collect(job['dir'], job['request'], outcome['exit_code'])
    assert evidence['status'] == 'passed' and evidence['head_sha'] == job['request']['head_sha']
    assert [m['caption'] for m in evidence['media']] == ['Step 1: Picker', 'Step 2: Selected']
    row = {**entry(), 'exit_code': 0, 'visual': evidence}
    assert checks.visual_passed({'test_plan': {'e2e': [entry()]}}, [row]).passed


def test_one_claim_per_request_and_submit_needs_a_claim(job):
    with pytest.raises(bw.WorkerError, match='claim the request first'):
        bw.submit(job['dir'], draft(job))
    bw.claim(job['dir'])
    with pytest.raises(bw.WorkerError, match='already claimed'):
        bw.claim(job['dir'])


def test_uncommitted_or_untracked_source_is_refused_but_ignored_build_output_is_not(job):
    (job['repo'] / 'build').mkdir()
    (job['repo'] / 'build' / 'bundle.js').write_text('compiled')          # ignored output: fine
    bw.claim(job['dir'])
    (job['repo'] / 'app.js').write_text('render(changed)\n')               # tracked edit after the claim
    with pytest.raises(bw.WorkerError, match='uncommitted or untracked source.*app.js'):
        bw.submit(job['dir'], draft(job))
    git(job['repo'], 'checkout', '--', 'app.js')
    (job['repo'] / 'extra.js').write_text('new()\n')                       # untracked source
    with pytest.raises(bw.WorkerError, match='extra.js'):
        bw.submit(job['dir'], draft(job))
    assert 'result.json' not in published(job)
    (job['repo'] / 'extra.js').unlink()
    assert bw.submit(job['dir'], draft(job))['status'] == 'passed'


def test_dirty_checkout_cannot_be_claimed(job):
    (job['repo'] / 'app.js').write_text('render(changed)\n')
    with pytest.raises(bw.WorkerError, match='uncommitted'):
        bw.claim(job['dir'])
    assert not (job['dir'] / 'claim.json').exists()


def test_moved_head_makes_evidence_stale(job):
    bw.claim(job['dir'])
    git(job['repo'], 'commit', '-q', '--allow-empty', '-m', 'later')
    with pytest.raises(bw.WorkerError, match='evidence would be stale'):
        bw.submit(job['dir'], draft(job))
    assert 'result.json' not in published(job)


def test_closed_expired_submitted_and_remote_requests_are_refused(job, tmp_path):
    (job['dir'] / 'closed').touch()
    with pytest.raises(bw.WorkerError, match='closed'):
        bw.claim(job['dir'])
    (job['dir'] / 'closed').unlink()
    old = time.time() - visual.REQUEST_TTL - 5
    os.utime(job['dir'] / 'request.json', (old, old))
    with pytest.raises(bw.WorkerError, match='expired'):
        bw.claim(job['dir'])
    directory, _ = visual.new_request(tmp_path / 'other', job['repo'], job['request']['head_sha'], 'https://preview.example.com/', entry())
    with pytest.raises(bw.WorkerError, match='not a loopback'):
        bw.claim(directory)


def test_overall_status_must_follow_the_steps(job):
    bw.claim(job['dir'])
    mixed = [{'status': 'passed', 'observation': 'Picker open'}, {'status': 'failed', 'observation': 'Old model still shown'}]
    with pytest.raises(bw.WorkerError, match='overall status of failed'):
        bw.submit(job['dir'], draft(job, steps=mixed))
    with pytest.raises(bw.WorkerError, match='all 2 requested steps'):
        bw.submit(job['dir'], draft(job, steps=mixed[:1]))
    verdict = bw.submit(job['dir'], draft(job, steps=mixed, status='failed', summary='Selection did not stick'))
    assert verdict['status'] == 'failed'          # a real failure is published, truthfully


@pytest.mark.parametrize('requested, measured', [
    ((1024, 700), (1012, 700)),    # observed 2026-10-10: outer 1252 with a 240 px tab sidebar left the page 12 px short
    ((390, 844), (406, 844)),      # observed: Chrome clamps the window at outer 626, so the page is never under 406
])
def test_observed_near_miss_viewports_cannot_pass(tmp_path, requested, measured):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q')
    git(repo, 'commit', '-q', '--allow-empty', '-m', 'app')
    head = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
    directory, request = visual.new_request(tmp_path / 'verify', repo, head, 'http://localhost:5173/', entry(*requested))
    bw.claim(directory)
    job = {'request': request, 'shots': tmp_path / 'shots'}
    with pytest.raises(bw.WorkerError, match=f'requested {requested[0]}x{requested[1]} viewport'):
        bw.submit(directory, draft(job, viewport_observed=dict(zip(('width', 'height'), measured))))
    blocked = [{'status': 'blocked', 'observation': f'Seen only at {measured[0]}x{measured[1]}'}] * 2
    assert bw.submit(directory, draft(job, status='blocked', steps=blocked))['status'] == 'blocked'


def test_brief_names_the_request_and_how_to_report(job):
    """The brief is the worker's whole contract: which request at which head, every step, how to reach the requested
    viewport and get past a native Save dialog, and the two commands that end it."""
    text = bw.brief(job['request'], job['dir'], job['runs'])
    assert job['request']['id'] in text and job['request']['head_sha'] in text
    assert all(step in text for step in STEPS) and 'outer size' in text and 'Save dialog' in text
    assert f"submit {job['request']['id']} --draft" in text and f"block {job['request']['id']}" in text


def test_each_passed_step_needs_its_own_screenshot_at_the_requested_viewport(job):
    bw.claim(job['dir'])
    one_shot = draft(job)
    one_shot['media'] = one_shot['media'][:1]
    with pytest.raises(bw.WorkerError, match=r'passed steps \[2\]'):
        bw.submit(job['dir'], one_shot)
    with pytest.raises(bw.WorkerError, match='requested 1280x720 viewport'):
        bw.submit(job['dir'], draft(job, viewport_observed={'width': 1742, 'height': 1427}))
    with pytest.raises(bw.WorkerError, match='requested 1280x720 viewport'):
        bw.submit(job['dir'], draft(job, viewport_observed=None))
    blocked_steps = [{'status': 'blocked', 'observation': 'Seen only at 1742x1427'}] * 2
    verdict = bw.submit(job['dir'], draft(job, status='blocked', steps=blocked_steps,
                                          viewport_observed={'width': 1742, 'height': 1427}))
    assert verdict['status'] == 'blocked'          # the wrong viewport is reported, not hidden


def test_stale_fake_or_linked_media_is_refused_and_nothing_is_published(job, tmp_path):
    bw.claim(job['dir'])
    stale = draft(job)
    old = time.time() - 3600
    os.utime(stale['media'][1]['path'], (old, old))
    with pytest.raises(bw.WorkerError, match='predates the claim'):
        bw.submit(job['dir'], stale)
    with pytest.raises(bw.WorkerError, match='not a PNG'):
        fake = draft(job)
        fake['media'][1]['path'] = shot(job, 'page.png', b'<html>not an image</html>')
        bw.submit(job['dir'], fake)
    linked = draft(job)
    link = tmp_path / 'link.png'
    link.symlink_to(linked['media'][1]['path'])
    linked['media'][1]['path'] = str(link)
    with pytest.raises(bw.WorkerError, match='not a regular file'):
        bw.submit(job['dir'], linked)
    assert published(job) == ['claim.json', 'request.json']


@pytest.mark.parametrize('broken', [
    [],
    'passed',
    {'media': ['not-an-object']},
    {'media': [{'path': '/tmp/a\x00b.png', 'role': 'screenshot', 'step': 1}]},
    {'media': [{'path': ['a list'], 'role': 'screenshot', 'step': 1}]},
    {'media': 'screen.png'},
    {'steps': None},
    {'steps': ['passed', 'passed']},
    {'viewport_observed': 'wide'},
    {'summary': 42},
])
def test_malformed_drafts_are_controlled_refusals(job, broken):
    bw.claim(job['dir'])
    bad = broken if not isinstance(broken, dict) else draft(job, **broken)
    with pytest.raises(bw.WorkerError):
        bw.submit(job['dir'], bad)


def test_step_numbers_must_be_real_steps(job):
    bw.claim(job['dir'])
    bad = draft(job)
    bad['media'][0]['step'] = True
    with pytest.raises(bw.WorkerError, match='step number'):
        bw.submit(job['dir'], bad)


def test_cli_refuses_malformed_json_without_a_traceback(job, tmp_path):
    runner = CliRunner()
    rid, runs = job['request']['id'], str(job['runs'])
    assert runner.invoke(app, ['browser', 'claim', rid, '--runs-dir', runs]).exit_code == 0
    bad = tmp_path / 'draft.json'
    bad.write_text('{"media": [')
    result = runner.invoke(app, ['browser', 'submit', rid, '--draft', str(bad), '--runs-dir', runs])
    assert result.exit_code == 1 and 'refused' in result.output and isinstance(result.exception, SystemExit)
    bad.write_text(json.dumps({**draft(job), 'media': [7]}))
    result = runner.invoke(app, ['browser', 'submit', rid, '--draft', str(bad), '--runs-dir', runs])
    assert result.exit_code == 1 and 'media 1 must be an object' in result.output
    assert isinstance(result.exception, SystemExit)              # a refusal, not an AttributeError traceback


def test_block_tells_the_controller_why_and_never_passes(job):
    (job['repo'] / 'app.js').write_text('dirty\n')            # blocked reports claim nothing about the app
    verdict = bw.block(job['dir'], 'Claude in Chrome is not connected')
    assert verdict['status'] == 'blocked' and 'not connected' in verdict['summary']
    evidence = visual.collect(job['dir'], job['request'], 0)
    assert evidence['status'] == 'blocked' and all(s['status'] == 'blocked' for s in evidence['steps_observed'])
    row = {**entry(), 'exit_code': 0, 'visual': evidence}
    assert not checks.visual_passed({'test_plan': {'e2e': [entry()]}}, [row]).passed
    with pytest.raises(bw.WorkerError, match='already submitted'):
        bw.block(job['dir'], 'again')


def test_list_shows_open_requests_only(job):
    assert [j['id'] for j in bw.open_requests(job['runs'])] == [job['request']['id']]
    assert bw.locate(job['runs'], job['request']['id']) == job['dir'].resolve()
    (job['dir'] / 'closed').touch()
    assert bw.open_requests(job['runs']) == []
    with pytest.raises(bw.WorkerError):
        bw.locate(job['runs'], '../../etc')


def test_doctor_reports_unattended_unavailable_without_a_claude_ai_login(monkeypatch):
    monkeypatch.setattr(bw.shutil, 'which', lambda _: '/usr/local/bin/claude')
    status = json.dumps({'loggedIn': False, 'authMethod': 'none'})
    monkeypatch.setattr(bw.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 1, status, ''))
    report = bw.doctor()
    assert report['logged_in'] is False and report['unattended'].startswith('unavailable')
    monkeypatch.setattr(bw.shutil, 'which', lambda _: None)
    assert bw.doctor()['unattended'].startswith('unavailable')
