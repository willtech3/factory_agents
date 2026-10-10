import io
import json
import tarfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from conftest import make_config
from factory.onboarding import AppOnboarding, unpack_source, validate_plan
from factory.test_environments import fingerprint, validate_profile
from factory.sandbox_tools.environment_runner import check_evidence
from factory.web.app import create_app


@pytest.fixture
def manager(tmp_path, root):
    return AppOnboarding(make_config(root, tmp_path / 'runs', gate_surface='web'))


def create(manager, **kw):
    return manager.create(kind='existing', name='Example', repository_url='https://github.com/example/app',
                          description='Test app', budget_usd=10, **kw)


@pytest.fixture
def plan(root):
    profile = json.loads((root / 'examples/greenfield-browser/test_environment.json').read_text())
    return {'summary':'A test app', 'app_kind':'web', 'journeys':[{'id':'add','title':'Add task','capability':'browser'}],
            'capabilities':[{'id':'browser','label':'Browser journeys','available':True,'limitation':''}],
            'questions':[], 'profile_path':'test_environment.json',
            'files':[{'path':'test_environment.json','content':json.dumps(profile)}], 'limitations':[]}


def test_creation_has_no_execution_and_budget_and_host_validation(manager):
    r=create(manager)
    assert r['status']=='connected' and r['remaining_usd']==10 and r['sessions']==[]
    assert len(manager.list())==1
    with pytest.raises(HTTPException):
        manager.create(kind='greenfield', name='X', repository_url='https://evil.example/repo/x', description='x',budget_usd=10)
    for cap in (float('nan'),float('inf'),-1,61):
        with pytest.raises(HTTPException):
            manager.create(kind='greenfield',name='X',repository_url='https://github.com/example/x',description='x',budget_usd=cap)


def test_plan_scope_and_profile_validation(plan,tmp_path,root):
    got=validate_plan(plan,'existing',tmp_path,root/'schemas')
    assert got['profile']['runtime']=='linux'
    plan['files'].append({'path':'src/auth.py','content':'disabled'})
    with pytest.raises(ValueError,match='outside onboarding scope'):
        validate_plan(plan,'existing',tmp_path,root/'schemas')
    plan['files'][-1]['path']='.github/workflows/ci.yml'
    with pytest.raises(ValueError,match='CI'):
        validate_plan(plan,'greenfield',tmp_path,root/'schemas')
    plan['files'][-1]['path']='../outside'
    with pytest.raises(ValueError,match='path'):
        validate_plan(plan,'greenfield',tmp_path,root/'schemas')


def test_stale_approval_and_duplicate_operation_do_not_dispatch(manager,plan,monkeypatch):
    r=create(manager); aid=r['app_id']
    manager.update(aid,plan=plan,plan_sha256=fingerprint(plan),status='setup_ready')
    with pytest.raises(HTTPException) as err:
        manager.start(aid,'verify','stale')
    assert err.value.status_code==409
    manager.update(aid,status='verifying')
    with pytest.raises(HTTPException) as err:
        manager.start(aid,'discover')
    assert err.value.status_code==409


def test_answers_invalidate_review_and_never_become_credentials(manager,plan):
    r=create(manager); aid=r['app_id']; plan['questions']=[{'id':'auth','prompt':'Test login?','required':True}]
    manager.update(aid,plan=plan,plan_sha256=fingerprint(plan))
    manager.answers(aid,{'auth':'Use a synthetic test identity'})
    r=manager.get(aid)
    assert r['plan_sha256'] is None and r['credentials']==[]
    with pytest.raises(HTTPException):
        manager.answers(aid,{'unknown':'answer'})


def test_cost_counts_unknown_reservation_and_is_idempotent(manager):
    aid=create(manager)['app_id']
    manager.update(aid,reserved_usd=3)
    manager._started(aid,{'session_id':'s1','stage':'discovery'})
    manager._finished(aid,{'session_id':'s1','list_cost_usd':None})
    manager._finished(aid,{'session_id':'s1','list_cost_usd':None})
    r=manager.get(aid)
    assert r['spent_usd']==3 and r['remaining_usd']==7 and r['cost_warning']


def test_archive_refuses_links_traversal_and_runtime_env(tmp_path):
    for name, link in [('repo/../escape',False),('repo/.env.local',False),('repo/link',True)]:
        buf=io.BytesIO()
        with tarfile.open(fileobj=buf,mode='w') as t:
            m=tarfile.TarInfo(name); m.type=tarfile.SYMTYPE if link else tarfile.REGTYPE
            m.linkname='/etc/passwd' if link else '';m.size=0;t.addfile(m,io.BytesIO())
        with pytest.raises(ValueError):
            unpack_source(buf.getvalue(),tmp_path,strip_root=True)


def test_nonbrowser_recipe_requires_real_functional_results(tmp_path,plan,root):
    profile=json.loads(plan['files'][0]['content']); profile.pop('browser');profile['services']=[]
    profile['verification']={'kind':'cli','checks':[{'id':'real-output','description':'CLI processes fixture'}]}
    assert validate_profile(profile,root/'schemas')==[]
    assert not check_evidence(profile,tmp_path)[0]
    p=tmp_path/'verification-result.json'
    p.write_text(json.dumps({'status':'passed','checks':[]}))
    assert not check_evidence(profile,tmp_path)[0]
    p.write_text(json.dumps({'status':'passed','checks':[{'id':'real-output','status':'passed','observation':'Processed fixture and matched expected output'}]}))
    assert check_evidence(profile,tmp_path)[0]


def test_app_api_cross_origin_and_private_metadata(tmp_path,root):
    cfg=make_config(root,tmp_path/'runs',gate_surface='web')
    app=create_app(cfg,demo_thread=False)
    with TestClient(app) as c:
        body={'kind':'greenfield','name':'X','repository_url':'https://github.com/example/x','description':'demo','budget_usd':2}
        assert c.post('/api/apps',json=body,headers={'Origin':'https://evil.example'}).status_code==403
        assert c.post('/api/apps',content='x').status_code==415
        r=c.post('/api/apps',json=body).json();aid=r['app_id']
        app.state.onboarding.update(aid,vault_id='private-vault',credentials=[{'name':'TEST_KEY','id':'private-credential'}])
        data=c.get('/api/apps/'+aid).text
        assert 'private-vault' not in data and 'private-credential' not in data and 'configured' in data
        assert c.post('/api/apps/'+aid+'/credentials',json={'name':'UNDECLARED','value':'never-store-me'}).status_code==400
        assert 'never-store-me' not in (tmp_path/'runs'/'_apps'/aid/'app.json').read_text()
