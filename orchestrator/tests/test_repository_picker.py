import dataclasses
import json

import httpx
import pytest
from conftest import make_config
from fastapi.testclient import TestClient

from factory.web.app import create_app
from factory.web import repositories


def connect(monkeypatch, handler):
    original = repositories.GitHubForge
    monkeypatch.setattr(repositories, 'GitHubForge', lambda *a, **kw: original(*a, **kw, transport=httpx.MockTransport(handler)))


def test_repository_pages_are_private_bounded_and_mark_unwritable(root, tmp_path, monkeypatch):
    cfg = make_config(root, tmp_path, forge_token='never-return-this')
    seen = []
    def handler(req):
        seen.append(req)
        assert req.headers['Authorization'] == 'Bearer never-return-this'
        assert req.url.path == '/user/repos' and req.url.params['per_page'] == '100'
        assert req.url.params['page'] == '2'
        rows = [dict(full_name='team/app', html_url='https://github.com/team/app', default_branch='develop', private=True, permissions={'push':True}, temp_clone_token='secret', owner={'login':'team','type':'Organization'}),
                dict(full_name='team/archive', html_url='https://github.com/team/archive', archived=True, permissions={'push':True}),
                dict(full_name='team/read', html_url='https://github.com/team/read', permissions={'push':False})]
        return httpx.Response(200,json=rows,headers={'Link':'<https://api.github.com/user/repos?page=3>; rel="next"'})
    connect(monkeypatch, handler)
    with TestClient(create_app(cfg)) as c:
        response = c.get('/api/repositories?page=2')
        assert response.status_code == 200
        data=response.json()
        assert data['next_page'] == 3
        assert data['repositories'][0]['default_branch'] == 'develop'
        assert [r['selectable'] for r in data['repositories']] == [True,False,False]
        # who owns it and whether the connection administers it: where the add-app form may offer a new repository
        assert data['repositories'][0]['owner_type'] == 'Organization' and data['repositories'][0]['admin'] is False
        assert 'token' not in response.text and 'secret' not in response.text
        assert c.get('/api/repositories?page=0').status_code == 422
        assert len(seen)==1


@pytest.mark.parametrize('status,expected',[(401,401),(403,503),(429,503),(500,502)])
def test_repository_errors_do_not_leak_provider_body(root,tmp_path,monkeypatch,status,expected):
    connect(monkeypatch,lambda req:httpx.Response(status,json={'message':'secret-provider-body'}))
    with TestClient(create_app(make_config(root,tmp_path,forge_token='secret'))) as c:
        r=c.get('/api/repositories')
        assert r.status_code==expected and 'secret' not in r.text


def test_repository_discovery_without_credentials(root,tmp_path):
    with TestClient(create_app(make_config(root,tmp_path))) as c:
        assert c.get('/api/repositories').status_code==409


def test_live_start_binds_selected_repository_and_preserves_branch(root,tmp_path,monkeypatch):
    from factory.web import app as webapp
    cfg=make_config(root,tmp_path,forge_token='test',forge_repo='old/repo')
    app=create_app(cfg)
    monkeypatch.setattr(webapp.cfgmod,'missing',lambda *a,**kw: [])
    calls=[]
    class Proc: pid=12345
    monkeypatch.setattr(webapp.subprocess,'Popen',lambda args,**kw: calls.append((args,kw)) or Proc())
    outcome=json.loads((root/'examples/sample-app/outcome.json').read_text())
    outcome['target_repositories']=[{'url':'https://github.com/team/selected','default_branch':'develop','paths_in_scope':['src/**']}]
    with TestClient(app) as c:
        saved=c.post('/api/outcomes',json={'outcome':outcome}).json()
        assert saved['ok']
        r=c.post('/api/runs',json={'outcome_id':saved['outcome_id'],'mode':'live'})
        assert r.status_code==200
        assert calls[0][1]['env']['FORGE_REPO']=='team/selected'
        path=calls[0][0][-1]
        assert json.loads(open(path).read())['target_repositories'][0]['default_branch']=='develop'


def test_existing_repository_binding_does_not_seed_default_branch(root,tmp_path,monkeypatch):
    from factory.cli import _bind_forge
    from factory import forge
    cfg=make_config(root,tmp_path,forge_token='test',forge_repo='team/app')
    class ReadOnlyForge:
        kind='github'
        def __getattr__(self,name):raise AssertionError(f'unexpected repository mutation or lookup: {name}')
    monkeypatch.setattr(forge,'make_forge',lambda cfg:ReadOnlyForge())
    _,_,created=_bind_forge(cfg,{'target_repositories':[{'url':'https://github.com/team/app','default_branch':'main'}]},True)
    assert created is None


@pytest.mark.parametrize('url',['https://evil.test/team/repo','https://github.com/team/repo/pulls','https://user:secret@github.com/team/repo','https://github.com/../repo'])
def test_repository_target_rejects_foreign_hosts_and_non_repository_urls(root,tmp_path,url):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):repositories.repository_name(url,make_config(root,tmp_path))
