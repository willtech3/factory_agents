"""Automated acceptance criteria may cite an actual controller-run e2e journey, without fake unit tests."""
from copy import deepcopy

import pytest
from factory import checks
import support.hosted as hosted
from support import artifacts
from factory.stages.implement import LocalVerifier, run_implement_one


def evidence():
    visual = {'persona': 'Owner', 'viewport': {'width': 390, 'height': 844}, 'steps': ['Open picker'],
              'environment': {'name': 'sandbox', 'data': 'synthetic', 'authentication': 'test-identity'}}
    spec = {'acceptance_criteria': [{'ac_id': 'AC-1', 'verification': 'automated_test'}],
            'test_plan': {'e2e': [{'command': 'python browser.py', 'target': 'preview', 'covers_ac': ['AC-1'], 'visual': visual}]}}
    rows = [{'command': 'python browser.py', 'target': 'preview', 'covers_ac': ['AC-1'], 'exit_code': 0,
             'visual': {**deepcopy(visual), 'status': 'passed'}}]
    return spec, rows


def mapped(spec, rows, ids=None, **kw):
    return checks.ac_test_mapping(spec, {'criteria': [{'ac_id': 'AC-1', 'tests': ids or ['e2e:0']}]},
                                  None, None, require_verified=True, e2e_results=rows, **kw)


def test_the_controller_run_browser_journey_itself_proves_its_criterion():
    spec, rows = evidence()
    result = mapped(spec, rows)
    assert result.passed and result.data['verified']


@pytest.mark.parametrize('change', ['missing', 'failed', 'not-run', 'target', 'command', 'coverage', 'viewport', 'steps', 'visual-blocked', 'no-visual'])
def test_missing_failed_or_mismatched_browser_evidence_cannot_prove_a_criterion(change):
    spec, rows = evidence()
    if change == 'missing': rows = []
    elif change == 'failed': rows[0]['exit_code'] = 1
    elif change == 'not-run': rows[0]['exit_code'] = None
    elif change == 'target': rows[0]['target'] = 'local'
    elif change == 'command': rows[0]['command'] = 'other.py'
    elif change == 'coverage': rows[0]['covers_ac'] = ['AC-2']
    elif change == 'viewport': rows[0]['visual']['viewport']['width'] = 1280
    elif change == 'steps': rows[0]['visual']['steps'] = ['Unrelated baseline']
    elif change == 'visual-blocked': rows[0]['visual']['status'] = 'blocked'
    elif change == 'no-visual': del rows[0]['visual']
    assert not mapped(spec, rows).passed


@pytest.mark.parametrize('test_id', ['e2e:1', 'e2e:-1', 'e2e:00', 'e2e:fake'])
def test_an_invented_or_ambiguous_entry_index_is_rejected(test_id):
    spec, rows = evidence()
    assert not mapped(spec, rows, [test_id]).passed


def test_an_entry_proving_another_criterion_cannot_be_borrowed():
    spec, rows = evidence()
    spec['test_plan']['e2e'][0]['covers_ac'] = ['AC-2']
    rows[0]['covers_ac'] = ['AC-2']
    assert not mapped(spec, rows).passed


def test_a_passing_journey_does_not_hide_a_missing_additional_unit_test():
    spec, rows = evidence()
    assert not mapped(spec, rows, ['e2e:0', 'tests/test_missing.py::test_missing']).passed


def test_a_nonvisual_service_journey_needs_its_actual_command_result():
    spec, rows = evidence()
    del spec['test_plan']['e2e'][0]['visual']
    del rows[0]['visual']
    assert mapped(spec, rows).passed
    rows[0]['exit_code'] = 2
    assert not mapped(spec, rows).passed


sandbox_double = hosted.sandbox_double
no_host_execution = hosted.no_host_execution


def test_automated_ui_mapping_survives_the_full_hosted_controller_round(tmp_path, root, monkeypatch, no_host_execution):
    spec = hosted.spec()
    spec['acceptance_criteria'][1]['verification'] = 'automated_test'
    monkeypatch.setattr(hosted, 'REPORT', artifacts.report('spec_hosted', [
        {'ac_id': 'AC-1', 'tests': ['tests/test_a.py::test_a']},
        {'ac_id': 'AC-2', 'tests': ['e2e:0']}]))
    origin = hosted.remote(tmp_path)
    ctx = hosted.context(tmp_path, root, hosted.Driver(origin, tmp_path))
    verdict = run_implement_one(ctx, spec, str(origin), 'main', LocalVerifier(str(origin), tmp_path/'verify'), {})
    det = {r['check']: r for r in hosted.artifact(ctx, 'implement', 'spec_hosted-r1-deterministic_checks.json')}
    assert verdict['result'] == 'ready'
    assert det['ac_test_mapping']['passed'] and det['visual_e2e']['passed']
