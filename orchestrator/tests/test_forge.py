"""Forge selection and the GitHub client's request shapes, through httpx's MockTransport. No network."""
import base64
import json

import httpx
import pytest
from conftest import make_config

from factory import config as cfgmod
from factory.forge import PIPELINE_TERMINAL, make_forge
from factory.forge.github import GitHubForge
from factory.forge.gitlab import GitLabForge


class Seq(list):
    """Route value: one (status, payload) per call in order; the last one repeats. A plain list is a JSON array."""


class Recorder:
    """Scripted forge API: records every request; answers from a route table (method + decoded path -> json/status)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.raw_paths = []                              # as sent, so a missing percent-encoding shows

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.raw_paths.append(request.url.raw_path.decode().split("?")[0])
        self.calls.append((request.method, request.url.path, dict(request.url.params), body))
        key = f"{request.method} {request.url.path}"
        resp = self.routes.get(key)
        if resp is None:
            return httpx.Response(404, json={"message": f"no route {key}"})
        if isinstance(resp, Seq):                        # one response per call, the last one repeats
            idx = self.calls.count((request.method, request.url.path, dict(request.url.params), body)) - 1
            resp = resp[min(idx, len(resp) - 1)]
        status, payload = (resp if isinstance(resp, tuple) else (200, resp))
        if callable(payload):
            payload = payload(body, dict(request.url.params))
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload)


def _forge(routes, **kw) -> tuple[GitHubForge, Recorder]:
    rec = Recorder(routes)
    f = GitHubForge("https://github.com", "ghp_test", "will/ledgerline", transport=httpx.MockTransport(rec), **kw)
    return f, rec


# ---------------------------------------------------------------- selection and config aliases

def test_make_forge_selects_by_config(tmp_path, root):
    assert make_forge(make_config(root, tmp_path)) is None                     # no token: local mode
    gh = make_forge(make_config(root, tmp_path, forge_token="t"))
    assert isinstance(gh, GitHubForge) and gh.kind == "github" and gh.api == "https://api.github.com"
    gl = make_forge(make_config(root, tmp_path, forge="gitlab", forge_url="https://gitlab.example.internal",
                                forge_token="t", forge_repo="group/app"))
    assert isinstance(gl, GitLabForge) and gl.kind == "gitlab" and gl.base.startswith("https://gitlab.example.internal/api/v4")
    ghes = make_forge(make_config(root, tmp_path, forge_url="https://ghe.example.com", forge_token="t"))
    assert ghes.api == "https://ghe.example.com/api/v3"
    with pytest.raises(ValueError):
        make_forge(make_config(root, tmp_path, forge="bitbucket", forge_token="t"))


def test_config_env_and_defaults(monkeypatch, tmp_path):
    for k in ("FACTORY_FORGE", "FORGE_URL", "FORGE_TOKEN", "FORGE_REPO", "LINEAR_API_KEY", "FACTORY_GATE_SURFACE",
              "FACTORY_VAULT_ID", "ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID"):
        monkeypatch.delenv(k, raising=False)
    nodotenv = tmp_path / "no.env"
    monkeypatch.setenv("FORGE_TOKEN", "ghp_x")
    monkeypatch.setenv("FORGE_REPO", "will/ledgerline")
    cfg = cfgmod.load(tmp_path / "no-lock.json", dotenv_path=nodotenv)
    assert cfg.forge == "github" and cfg.forge_url == "https://github.com" and cfg.forge_token == "ghp_x"
    assert cfg.forge_repo == "will/ledgerline" and cfg.repo_url == "https://github.com/will/ledgerline"
    assert cfg.gate_surface == "web"     # no design PR in the target repository (ADR-0011)
    gaps = cfgmod.missing(cfg)
    assert not any("FORGE_TOKEN" in g or "FACTORY_VAULT_ID" in g for g in gaps)   # github needs no vault

    monkeypatch.setenv("LINEAR_API_KEY", "lin_x")
    assert cfgmod.load(tmp_path / "no-lock.json", dotenv_path=nodotenv).gate_surface == "tracker"
    assert any("LINEAR_TEAM_ID" in g for g in cfgmod.missing(cfgmod.load(tmp_path / "no-lock.json", dotenv_path=nodotenv)))

    monkeypatch.setenv("FACTORY_FORGE", "gitlab")
    monkeypatch.setenv("FORGE_URL", "https://gitlab.example.internal")
    monkeypatch.setenv("FORGE_TOKEN", "glpat")
    monkeypatch.setenv("FORGE_REPO", "group/app")
    cfg = cfgmod.load(tmp_path / "no-lock.json", dotenv_path=nodotenv)
    assert (cfg.forge, cfg.forge_url, cfg.forge_token, cfg.forge_repo) == ("gitlab", "https://gitlab.example.internal", "glpat", "group/app")
    assert any("FACTORY_VAULT_ID" in g for g in cfgmod.missing(cfg))


# ---------------------------------------------------------------- GitHub request shapes

def test_github_commit_files_uses_git_data_api():
    routes = {
        "GET /repos/will/ledgerline/git/ref/heads/main": {"object": {"sha": "head1"}},
        "GET /repos/will/ledgerline/git/commits/head1": {"tree": {"sha": "tree0"}},
        "POST /repos/will/ledgerline/git/blobs": (201, lambda b, p: {"sha": "blob-" + b["content"][:4]}),
        "POST /repos/will/ledgerline/git/trees": (201, {"sha": "tree1"}),
        "POST /repos/will/ledgerline/git/commits": (201, {"sha": "commit1"}),
        "PATCH /repos/will/ledgerline/git/refs/heads/main": {"object": {"sha": "commit1"}},
    }
    f, rec = _forge(routes)
    sha = f.commit_files("main", "factory: platform contract skill",
                         {".claude/skills/platform-contract/SKILL.md": b"# skill", ".github/workflows/preview.yml": b"on: push"})
    assert sha == "commit1"
    methods = [(m, p) for m, p, _, _ in rec.calls]
    assert methods[0] == ("GET", "/repos/will/ledgerline/git/ref/heads/main")
    assert methods.count(("POST", "/repos/will/ledgerline/git/blobs")) == 2
    tree_body = next(b for m, p, _, b in rec.calls if p.endswith("/git/trees"))
    assert tree_body["base_tree"] == "tree0"
    assert {e["path"] for e in tree_body["tree"]} == {".claude/skills/platform-contract/SKILL.md", ".github/workflows/preview.yml"}
    assert all(e["mode"] == "100644" and e["type"] == "blob" for e in tree_body["tree"])
    blob = next(b for m, p, _, b in rec.calls if p.endswith("/git/blobs"))
    assert blob["encoding"] == "base64" and base64.b64decode(blob["content"]) in (b"# skill", b"on: push")
    commit_body = next(b for m, p, _, b in rec.calls if p.endswith("/git/commits") and m == "POST")
    assert commit_body == {"message": "factory: platform contract skill", "tree": "tree1", "parents": ["head1"]}
    assert rec.calls[-1][0] == "PATCH" and rec.calls[-1][3]["sha"] == "commit1"


def test_github_pull_request_flow():
    routes = {
        "POST /repos/will/ledgerline/pulls": (201, {"number": 7, "html_url": "https://github.com/will/ledgerline/pull/7"}),
        "POST /repos/will/ledgerline/issues/7/labels": {},
        "POST /repos/will/ledgerline/pulls/7/requested_reviewers": (422, {"message": "Review cannot be requested from pull request author."}),
        "GET /repos/will/ledgerline/pulls/7": {"state": "closed", "merged": True, "merged_at": "2026-10-10T10:00:00Z",
                                               "head": {"sha": "h7"}, "merge_commit_sha": "mc7", "mergeable_state": "clean",
                                               "labels": [{"name": "factory"}, {"name": "spec:spec_x"}]},
        "GET /repos/will/ledgerline/pulls/7/reviews": [
            {"user": {"login": "will"}, "state": "CHANGES_REQUESTED"},
            {"user": {"login": "will"}, "state": "APPROVED"},
            {"user": {"login": "sam"}, "state": "COMMENTED"}],
        "POST /repos/will/ledgerline/issues/7/comments": (201, {"id": 1}),
    }
    f, rec = _forge(routes)
    cr = f.create_change_request(source_branch="factory/run_1/spec_x", target_branch="main", title="spec_x: title",
                                 description="body", labels=["factory", "spec:spec_x"], reviewers=["will"], draft=False)
    assert (cr.id, cr.url) == (7, "https://github.com/will/ledgerline/pull/7")
    pr_body = next(b for m, p, _, b in rec.calls if p.endswith("/pulls") and m == "POST")
    assert pr_body == {"title": "spec_x: title", "head": "factory/run_1/spec_x", "base": "main", "body": "body", "draft": False}
    assert next(b for m, p, _, b in rec.calls if p.endswith("/labels")) == {"labels": ["factory", "spec:spec_x"]}
    st = f.change_request_state(7)
    assert st["state"] == "merged" and st["sha"] == "h7" and st["labels"] == ["factory", "spec:spec_x"]
    assert f.change_request_approved(7) is True       # latest review by will is APPROVED


def test_github_workflow_runs_and_deployments():
    routes = {
        "GET /repos/will/ledgerline/actions/runs": {"workflow_runs": [
            {"id": 55, "status": "in_progress", "conclusion": None, "head_branch": "factory/run_1/spec_x", "head_sha": "h1",
             "html_url": "https://github.com/will/ledgerline/actions/runs/55", "name": "preview", "created_at": "t", "updated_at": "t"}]},
        "GET /repos/will/ledgerline/actions/runs/55": {"id": 55, "status": "completed", "conclusion": "failure",
                                                       "head_branch": "main", "head_sha": "h1", "html_url": "u"},
        "GET /repos/will/ledgerline/actions/runs/55/jobs": {"jobs": [
            {"id": 9, "name": "deploy", "status": "completed", "conclusion": "failure", "html_url": "ju"}]},
        "GET /repos/will/ledgerline/actions/jobs/9/logs": "line1\nrails db:migrate failed\n",
        "GET /repos/will/ledgerline/deployments": lambda b, p: (
            [{"id": 3, "sha": "h1", "ref": "factory/run_1/spec_x", "environment": "preview/factory/run_1/spec_x", "created_at": "t"}]
            if p.get("ref") == "factory/run_1/spec_x" or p.get("environment") == "dev" else []),
        "GET /repos/will/ledgerline/deployments/3/statuses": [{"state": "success", "environment_url": "https://run-1-spec-x.preview.example.com"}],
    }
    f, rec = _forge(routes)
    pipe = f.latest_pipeline("factory/run_1/spec_x", "h1")
    assert pipe["status"] == "running" and pipe["sha"] == "h1" and pipe["status"] not in PIPELINE_TERMINAL
    assert rec.calls[0][2] == {"branch": "factory/run_1/spec_x", "per_page": "1", "head_sha": "h1"}
    assert f.pipeline(55)["status"] == "failed"
    assert "db:migrate failed" in f.failed_job_log_excerpt(55)
    assert f.environment_url_for_branch("factory/run_1/spec_x") == "https://run-1-spec-x.preview.example.com"
    env = f.environment("dev")
    assert env["external_url"] == "https://run-1-spec-x.preview.example.com" and env["state"] == "success"
    # a workflow-scoped forge looks under /actions/workflows/<file>/runs
    f2, rec2 = _forge({"GET /repos/will/ledgerline/actions/workflows/preview.yml/runs": {"workflow_runs": []}}, workflow="preview.yml")
    assert f2.latest_pipeline("main") is None and rec2.calls[0][1].endswith("/actions/workflows/preview.yml/runs")


def test_gitlab_merge_request_flow_and_pipeline_reads():
    """GitLab is the second forge and has not run live; this pins its request shapes and how it normalises answers to the
    shapes the watcher and Stage 5 read (the same ones GitHubForge returns)."""
    p = "/api/v4/projects/group/app"
    envs = {"preview/factory-run-1-spec-x": {"id": 3, "name": "preview/factory-run-1-spec-x", "state": "available",
                                             "external_url": "https://run-1-spec-x.preview.example.internal"},
            "dev": {"id": 1, "name": "dev", "state": "available", "external_url": "https://dev.example.internal",
                    "last_deployment": {"id": 40, "status": "success", "sha": "m12", "ref": "main", "deployable": {"id": 9}}}}
    routes = {
        "GET /api/v4/users": lambda body, params: [{"id": 7, "username": "will"}] if params.get("username") == "will" else [],
        f"POST {p}/merge_requests": (201, {"iid": 12, "web_url": "https://gitlab.example.internal/group/app/-/merge_requests/12"}),
        f"GET {p}/merge_requests/12": {"state": "merged", "sha": "h12", "merge_commit_sha": "m12", "labels": ["factory"],
                                       "merged_at": "t", "detailed_merge_status": "mergeable"},
        f"GET {p}/merge_requests/12/approvals": {"approved": False, "approvals_left": 0, "approved_by": [{"user": {"username": "will"}}]},
        f"GET {p}/pipelines": [{"id": 5, "status": "running", "ref": "main", "sha": "m12", "web_url": "p5"}],
        f"GET {p}/pipelines/5": {"id": 5, "status": "failed", "ref": "main", "sha": "m12", "web_url": "p5"},
        f"GET {p}/pipelines/5/jobs": [{"id": 8, "name": "test", "status": "success"},
                                      {"id": 9, "name": "deploy-dev", "stage": "deploy", "status": "failed", "web_url": "j9",
                                       "environment": {"name": "dev"}, "failure_reason": "script_failure"}],
        f"GET {p}/jobs/9/trace": "line 1\nrails db:migrate failed\n",
        f"GET {p}/environments": lambda body, params: [envs[params["name"]]] if params.get("name") in envs else [],
    }
    rec = Recorder(routes)
    f = GitLabForge("https://gitlab.example.internal", "glpat", "group/app", transport=httpx.MockTransport(rec))

    cr = f.create_change_request(source_branch="factory/run_1/spec_x", target_branch="main", title="spec_x: title",
                                 description="body", labels=["factory", "spec:spec_x"], reviewers=["will", "nobody"], draft=True)
    assert (cr.id, cr.url.endswith("/merge_requests/12")) == (12, True)
    body = next(b for m, path, _, b in rec.calls if m == "POST")
    assert body == {"source_branch": "factory/run_1/spec_x", "target_branch": "main", "title": "Draft: spec_x: title",
                    "description": "body", "labels": "factory,spec:spec_x", "remove_source_branch": True, "reviewer_ids": [7]}
    assert f"{p.replace('group/app', 'group%2Fapp')}/merge_requests" in rec.raw_paths     # the project path is one encoded segment
    st = f.change_request_state(12)
    assert st["state"] == "merged" and st["merge_commit_sha"] == "m12" and st["labels"] == ["factory"]
    assert f.change_request_approved(12) is True                 # no approvals left and someone approved

    pipe = f.latest_pipeline("main", "m12")
    assert pipe["status"] == "running" and pipe["status"] not in PIPELINE_TERMINAL
    assert rec.calls[-1][2] == {"ref": "main", "order_by": "id", "sort": "desc", "per_page": "1", "sha": "m12"}
    assert f.pipeline(5)["status"] in PIPELINE_TERMINAL
    excerpt = f.failed_job_log_excerpt(5)
    assert excerpt.startswith("job deploy-dev (script_failure) j9") and "db:migrate failed" in excerpt
    dev = f.environment("dev")
    assert dev["external_url"] == "https://dev.example.internal" and dev["last_deployment"]["deployable_id"] == 9
    assert f.environment("staging") is None
    assert f.environment_url_for_branch("factory/run_1/spec_x") == "https://run-1-spec-x.preview.example.internal"


def test_github_session_resource_and_git_instructions():
    f, _ = _forge({})
    res = f.session_repository_resource("https://github.com/will/ledgerline.git", "factory/run_1/spec_x")
    assert res == {"type": "github_repository", "url": "https://github.com/will/ledgerline", "mount_path": "/workspace/repo",
                   "authorization_token": "ghp_test", "checkout": {"type": "branch", "name": "factory/run_1/spec_x"}}
    text = f.sandbox_git_instructions("https://github.com/will/ledgerline", "main", "factory/run_1/spec_x")
    assert "/workspace/repo" in text and "git push -u origin factory/run_1/spec_x" in text and "GITLAB_TOKEN" not in text
    # reviewers, the merge judge and the integrator get a read-only clause (post-mortem issue 18)
    ro = f.sandbox_git_instructions("https://github.com/will/ledgerline", "main", "factory/run_1/spec_x", read_only=True)
    assert "factory/run_1/spec_x" in ro and "never commit, never push" in ro and "push -u" not in ro and "Commit as you go" not in ro
    gl_ro = GitLabForge("https://gitlab.example.internal", "glpat", "group/app").sandbox_git_instructions(
        "https://gitlab.example.internal/group/app", "main", "b", read_only=True)
    assert "Authorization: Bearer $GITLAB_TOKEN" in gl_ro and "never commit, never push" in gl_ro and "push it when done" not in gl_ro
    assert f.authenticated_url("https://github.com/will/ledgerline") == "https://x-access-token:ghp_test@github.com/will/ledgerline"
    gl = GitLabForge("https://gitlab.example.internal", "glpat", "group/app")
    assert gl.session_repository_resource("https://gitlab.example.internal/group/app", "b") is None
    assert "Authorization: Bearer $GITLAB_TOKEN" in gl.sandbox_git_instructions("https://gitlab.example.internal/group/app", "main", "b")
    assert gl.authenticated_url("https://gitlab.example.internal/group/app").startswith("https://oauth2:glpat@")


def test_dotenv_loader_and_design_only_gaps(monkeypatch, tmp_path):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID", "FORGE_TOKEN", "FORGE_REPO", "LINEAR_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text("# comment\nexport ANTHROPIC_API_KEY='sk-test'\nANTHROPIC_WORKSPACE_ID=wrkspc_1\nFORGE_REPO=\"will/app\"\n")
    monkeypatch.setenv("FORGE_REPO", "already/set")
    # load_dotenv writes os.environ directly; register the keys with monkeypatch so the test leaves no trace
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder"); monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "placeholder"); monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID")
    loaded = cfgmod.load_dotenv(env)
    assert loaded == ["ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID"]      # FORGE_REPO was set: never overridden
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-test" and os.environ["FORGE_REPO"] == "already/set"
    cfg = cfgmod.load(tmp_path / "no-lock.json", dotenv_path=env)
    assert cfg.workspace_id == "wrkspc_1"
    full, design = cfgmod.missing(cfg), cfgmod.missing(cfg, design_only=True)
    assert any("FORGE_TOKEN" in g for g in full) and not any("FORGE_TOKEN" in g for g in design)
    assert not any("ANTHROPIC_API_KEY" in g for g in design)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert any("ANTHROPIC_API_KEY" in g for g in cfgmod.missing(cfg, design_only=True))
    pc = cfgmod.platform_check(cfg)
    assert pc["ok"] is False and "ANTHROPIC_API_KEY" in pc["error"]


def test_github_creates_missing_repository_and_seeds_workflow(root):
    from factory.forge import ensure_repository, repo_from_url
    assert repo_from_url("https://github.com/will/factory-sample-app.git") == "will/factory-sample-app"
    assert repo_from_url("https://gitlab.example.internal/group/sub/app") == "group/sub/app"
    routes = {
        "GET /repos/will/newapp": (404, {"message": "Not Found"}),
        "GET /user": {"login": "will"},
        "POST /user/repos": (201, {"html_url": "https://github.com/will/newapp", "default_branch": "main", "private": True, "id": 9}),
        "GET /repos/will/newapp/git/ref/heads/main": {"object": {"sha": "init"}},
        "GET /repos/will/newapp/git/commits/init": {"tree": {"sha": "t0"}},
        "POST /repos/will/newapp/git/blobs": (201, {"sha": "b1"}),
        "POST /repos/will/newapp/git/trees": (201, {"sha": "t1"}),
        "POST /repos/will/newapp/git/commits": (201, {"sha": "c1"}),
        "PATCH /repos/will/newapp/git/refs/heads/main": {"object": {"sha": "c1"}},
    }
    rec = Recorder(routes)
    f = GitHubForge("https://github.com", "ghp_test", "will/newapp", transport=httpx.MockTransport(rec))
    created = ensure_repository(f, {"url": "https://github.com/will/newapp", "create": True}, root / "templates" / "github-actions-preview.yml")
    assert created["url"] == "https://github.com/will/newapp" and created["default_branch"] == "main"
    create_call = next(b for m, p, _, b in rec.calls if p == "/user/repos")
    assert create_call == {"name": "newapp", "private": True, "auto_init": True, "description": "Created by the software factory"}
    tree = next(b for m, p, _, b in rec.calls if p.endswith("/git/trees"))
    assert tree["tree"][0]["path"] == ".github/workflows/preview.yml"
    # existing repository: nothing happens (the workflow is only checked when a template is given)
    rec2 = Recorder({"GET /repos/will/newapp": {"id": 1}})
    f2 = GitHubForge("https://github.com", "ghp_test", "will/newapp", transport=httpx.MockTransport(rec2))
    assert ensure_repository(f2, {"url": "https://github.com/will/newapp", "create": True}) is None
    assert ensure_repository(f2, {"url": "https://github.com/will/newapp"}) is None and len(rec2.calls) == 1


def test_github_seed_retries_while_the_new_repository_is_not_ready_and_reseeds_an_unseeded_one(root):
    """Live greenfield run: POST /git/trees answered 404 seconds after the repository was created. The seed is
    retried with backoff; and a repository left without the workflow by that crash gets it on the next run."""
    from factory.forge import ensure_repository
    tmpl = root / "templates" / "github-actions-preview.yml"
    routes = {
        "GET /repos/will/newapp": (404, {"message": "Not Found"}),
        "GET /user": {"login": "will"},
        "POST /user/repos": (201, {"html_url": "https://github.com/will/newapp", "default_branch": "main", "private": True, "id": 9}),
        "GET /repos/will/newapp/git/ref/heads/main": {"object": {"sha": "init"}},
        "GET /repos/will/newapp/git/commits/init": {"tree": {"sha": "t0"}},
        "POST /repos/will/newapp/git/blobs": (201, {"sha": "b1"}),
        "POST /repos/will/newapp/git/trees": Seq([(404, {"message": "Not Found"}), (404, {"message": "Not Found"}), (201, {"sha": "t1"})]),
        "POST /repos/will/newapp/git/commits": (201, {"sha": "c1"}),
        "PATCH /repos/will/newapp/git/refs/heads/main": {"object": {"sha": "c1"}},
    }
    rec = Recorder(routes)
    f = GitHubForge("https://github.com", "ghp_test", "will/newapp", transport=httpx.MockTransport(rec))
    slept = []
    skill = root / "skills" / "platform-contract" / "SKILL.md"
    created = ensure_repository(f, {"url": "https://github.com/will/newapp", "create": True}, tmpl, skill_file=skill, sleep=slept.append)
    # two seed commits (skill, then workflow); the mock answers 404 twice per distinct tree body, so each is retried twice
    assert created["default_branch"] == "main" and created["workflow_seeded"] is True and slept == [2.0, 4.0, 2.0, 4.0]
    trees = [b for m, p, _, b in rec.calls if p.endswith("/git/trees")]
    assert trees[0]["tree"][0]["path"] == ".claude/skills/platform-contract/SKILL.md"      # the skill lands first, on its own
    assert trees[-1]["tree"][0]["path"] == ".github/workflows/preview.yml" and len(trees) == 6
    assert sum(1 for m, p, _, _ in rec.calls if p.endswith("/git/refs/heads/main") and m == "PATCH") == 2
    # a forge that keeps refusing (a token without the workflow scope: GitHub answers 404 for ever): a warning, not a crash
    routes4 = {**routes, "POST /repos/will/newapp/git/trees": (404, {"message": "Not Found"})}
    rec4 = Recorder(routes4)
    f4 = GitHubForge("https://github.com", "ghp_test", "will/newapp", transport=httpx.MockTransport(rec4))
    logged = []
    created4 = ensure_repository(f4, {"url": "https://github.com/will/newapp", "create": True}, tmpl, skill_file=skill, sleep=lambda s: None, log=logged.append)
    assert created4["workflow_seeded"] is False and "workflow" in logged[-1] and "gh auth refresh -s workflow" in logged[-1]
    # a repository that exists but has no workflow (the earlier run died after creating it): seeded, not created
    routes2 = {**routes, "GET /repos/will/newapp": {"id": 9, "default_branch": "main"},
               "GET /repos/will/newapp/contents/.github/workflows/preview.yml": (404, {"message": "Not Found"}),
               "POST /repos/will/newapp/git/trees": (201, {"sha": "t1"})}
    rec2 = Recorder(routes2)
    f2 = GitHubForge("https://github.com", "ghp_test", "will/newapp", transport=httpx.MockTransport(rec2))
    assert ensure_repository(f2, {"url": "https://github.com/will/newapp", "create": True}, tmpl, sleep=lambda s: None) is None
    assert not any(p == "/user/repos" for _, p, _, _ in rec2.calls)
    assert sum(1 for m, p, _, _ in rec2.calls if p.endswith("/git/refs/heads/main") and m == "PATCH") == 1
    # and one that already has it: untouched
    routes3 = {**routes2, "GET /repos/will/newapp/contents/.github/workflows/preview.yml": {"sha": "x"}}
    rec3 = Recorder(routes3)
    f3 = GitHubForge("https://github.com", "ghp_test", "will/newapp", transport=httpx.MockTransport(rec3))
    assert ensure_repository(f3, {"url": "https://github.com/will/newapp", "create": True}, tmpl, sleep=lambda s: None) is None
    assert not any(p.endswith("/git/trees") for _, p, _, _ in rec3.calls)


def test_a_repository_not_marked_create_is_never_touched(root):
    """An existing repository may require every change through a pull request, so the factory never commits to one it
    did not create; its sessions get the platform-contract skill as an upload instead (post-mortem 2026-10-10, issue 12)."""
    from factory.forge import ensure_repository
    rec = Recorder({})
    f = GitHubForge("https://github.com", "ghp_test", "will/app", transport=httpx.MockTransport(rec))
    skill = root / "skills" / "platform-contract" / "SKILL.md"
    assert ensure_repository(f, {"url": "https://github.com/will/app", "default_branch": "main"}, skill_file=skill,
                             sleep=lambda s: None) is None
    assert rec.calls == []
