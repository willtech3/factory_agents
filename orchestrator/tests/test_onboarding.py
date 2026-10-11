import io
import json
import tarfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from conftest import make_config
from factory.onboarding import AppOnboarding, unpack_source, validate_plan, secret_scope
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
    app=create_app(cfg)
    with TestClient(app) as c:
        body={'kind':'greenfield','name':'X','repository_url':'https://github.com/example/x','description':'demo','budget_usd':2}
        assert c.post('/api/apps',json=body,headers={'Origin':'https://evil.example'}).status_code==403
        assert c.post('/api/apps',content='x').status_code==415
        r=c.post('/api/apps',json=body).json();aid=r['app_id']
        secret={'name':'TEST_KEY','allowed_hosts':['example.com'],'injection':'header'}
        app.state.onboarding.update(aid,vault_id='private-vault',plan={'profile':{'secrets':[secret]}},credentials=[{'name':'TEST_KEY','id':'private-credential','vault_id':'private-vault','scope_sha256':secret_scope(secret)}])
        data=c.get('/api/apps/'+aid).text
        assert 'private-vault' not in data and 'private-credential' not in data and 'configured' in data
        assert c.post('/api/apps/'+aid+'/credentials',json={'name':'UNDECLARED','value':'never-store-me'}).status_code==400
        assert 'never-store-me' not in (tmp_path/'runs'/'_apps'/aid/'app.json').read_text()


def test_changed_secret_scope_is_not_attached_or_claimed_configured(manager,plan):
    aid=create(manager)['app_id']
    secret={'name':'TEST_KEY','required':True,'allowed_hosts':['example.com'],'injection':'header'}
    plan['profile']={'secrets':[secret]}
    manager.update(aid,plan=plan,credentials=[{'name':'TEST_KEY','id':'c1','vault_id':'v1','scope_sha256':secret_scope(secret)}])
    assert manager.get(aid)['credentials']==[{'name':'TEST_KEY','status':'configured'}]
    secret['allowed_hosts']=['different.example']
    manager.update(aid,plan=plan)
    assert manager.get(aid)['credentials']==[{'name':'TEST_KEY','status':'needs_reconnect'}]
    plan['profile']['secrets']=[]
    manager.update(aid,plan=plan)
    assert manager.get(aid)['credentials']==[]


def test_recovery_stops_orphans_and_conservatively_accounts(manager,monkeypatch):
    monkeypatch.setattr('factory.onboarding.owner_alive',lambda r:False)
    aid=create(manager)['app_id']
    manager.update(aid,status='verifying',owner_pid=999999,reserved_usd=4,sessions=[{'session_id':'s1','cost_usd':None}])
    calls=[]
    manager._client=SimpleNamespace(beta=SimpleNamespace(sessions=SimpleNamespace(events=SimpleNamespace(send=lambda **kw:calls.append(kw)))))
    assert manager.get(aid)['recoverable']
    r=manager.recover(aid)
    assert calls[0]['events']==[{'type':'user.interrupt'}]
    assert r['status']=='failed' and r['spent_usd']==4 and r['reserved_usd']==0
    with pytest.raises(HTTPException): manager.recover(aid)


def test_empty_setup_does_not_create_branch_or_pr(manager,plan):
    aid=create(manager)['app_id'];plan['files']=[]
    sha=fingerprint(plan)
    manager.update(aid,plan=plan,plan_sha256=sha,source_sha='abc',verification={'status':'passed','plan_sha256':sha,'source_sha':'abc'})
    manager._publish(aid,0)
    assert manager.get(aid)['phase']=='Existing setup reused; no setup changes to publish'
    manager.update(aid,plan_sha256='changed')
    with pytest.raises(ValueError,match='changed'): manager._publish(aid,0)


def test_run_binding_uses_only_verified_recipe_and_explicit_test_vaults(manager,plan,monkeypatch):
    import base64
    aid=create(manager)['app_id']
    secret={'name':'TEST_KEY','required':True,'allowed_hosts':['example.com'],'injection':'header'}
    profile=json.loads(plan['files'][0]['content']);profile['secrets']=[secret];profile['network']['allowed_hosts']=['example.com']
    plan['profile']=profile
    sha=fingerprint(plan)
    manager.update(aid,status='ready',plan=plan,plan_sha256=sha,source_sha='abc',verification={'status':'passed','plan_sha256':sha,'source_sha':'abc','profile_sha256':fingerprint(profile)},
                   credentials=[{'name':'TEST_KEY','id':'c1','vault_id':'test-only-vault','scope_sha256':secret_scope(secret)}, {'name':'OLD_KEY','id':'c2','vault_id':'old-unused-vault'}],vault_id='legacy-vault')
    def get(path):
        payload={'test_environment':{'profile':'test_environment.json'}} if 'service_manifest.json' in path else profile
        return {'encoding':'base64','size':100,'content':base64.b64encode(json.dumps(payload).encode()).decode()}
    fake=SimpleNamespace(_get=get,file_exists=lambda *args:True,http=SimpleNamespace(close=lambda:None))
    monkeypatch.setattr(manager,'forge',lambda r:fake)
    target={'url':'https://github.com/example/app','default_branch':'main'}
    env=manager.run_settings(aid,target)
    assert env['FACTORY_PREVIEW']=='managed' and env['FACTORY_TEST_VAULT_IDS']=='test-only-vault'
    assert env['FACTORY_TEST_PROFILE_SHA256']==fingerprint(profile)
    with pytest.raises(HTTPException): manager.run_settings(aid,{**target,'url':'https://github.com/example/other'})
    profile['name']='changed'
    with pytest.raises(HTTPException) as exc: manager.run_settings(aid,target)
    assert exc.value.status_code==409 and 'base branch' in exc.value.detail


def test_setup_feedback_invalidates_review_and_preserves_previous_proposal(manager,plan):
    aid=create(manager)['app_id']
    manager.update(aid,plan=plan,plan_sha256=fingerprint(plan),status='failed')
    r=manager.feedback(aid,'Fix header lookup without removing the assertion')
    assert r['plan_sha256'] is None and r['plan']==plan and r['status']=='needs_input'
    manager.update(aid,status='verifying')
    with pytest.raises(HTTPException): manager.feedback(aid,'Different instructions')


def test_an_unused_recipe_is_rejected_before_cloud_verification(plan,tmp_path,root):
    plan['profile_path']='verification/factory-onboarding/unused.json'
    plan['files'][0]['path']=plan['profile_path']
    with pytest.raises(ValueError,match='change runner selects test_environment.json'):
        validate_plan(plan,'existing',tmp_path,root/'schemas')


def test_an_existing_manifest_selected_recipe_can_be_reused(plan,tmp_path,root):
    path='verification/current/profile.json'
    (tmp_path/'verification/current').mkdir(parents=True)
    (tmp_path/path).write_text(plan['files'][0]['content'])
    (tmp_path/'service_manifest.json').write_text(json.dumps({'test_environment':{'profile':path}}))
    plan['profile_path']=path; plan['files']=[]
    assert validate_plan(plan,'existing',tmp_path,root/'schemas')['profile_path']==path


def test_a_root_recipe_cannot_silently_override_an_existing_manifest_pointer(plan,tmp_path,root):
    (tmp_path/'service_manifest.json').write_text(json.dumps({'test_environment':{'profile':'ops/active.json'}}))
    with pytest.raises(ValueError,match='change runner selects ops/active.json'):
        validate_plan(plan,'existing',tmp_path,root/'schemas')


def test_greenfield_recipe_and_proposed_manifest_must_agree(plan,tmp_path,root):
    path='verification/profile.json'
    plan['profile_path']=path; plan['files'][0]['path']=path
    plan['files'].append({'path':'service_manifest.json','content':json.dumps({'test_environment':{'profile':path}})})
    assert validate_plan(plan,'greenfield',tmp_path,root/'schemas')['profile_path']==path


def test_custom_manifest_filename_is_used_for_selection(plan,tmp_path,root):
    (tmp_path/'factory-service.json').write_text(json.dumps({'test_environment':{'profile':'ops/active.json'}}))
    with pytest.raises(ValueError,match='change runner selects ops/active.json'):
        validate_plan(plan,'existing',tmp_path,root/'schemas','factory-service.json')


def test_a_reonboarded_plan_publishes_after_the_last_setup_pr(manager,plan,monkeypatch):
    """Live emoji-smith: a re-verified plan returned the merged setup PR instead of publishing its fix."""
    from factory import onboarding as ob
    started=[]
    monkeypatch.setattr(ob.threading,'Thread',lambda target,args,**kw:SimpleNamespace(start=lambda:started.append(args[1])))
    aid=create(manager)['app_id'];sha=fingerprint(plan)
    passed={'status':'passed','plan_sha256':sha,'source_sha':'abc'}
    manager.update(aid,plan=plan,plan_sha256=sha,source_sha='abc',verification=passed,status='ready',
                   pr_url='https://github.com/example/app/pull/1',pr_state='open',pr_plan_sha256=sha)
    manager.start(aid,'publish',sha)
    assert started==[]                                   # the same plan's open PR is returned, not published twice
    manager.update(aid,status='ready',pr_plan_sha256='an-earlier-plan')
    manager.start(aid,'publish',sha)
    assert started==['publish']
    manager.update(aid,status='ready',pr_plan_sha256=None,pr_state='merged')   # a record from before pr_plan_sha256
    manager.start(aid,'publish',sha)
    assert started==['publish','publish']
