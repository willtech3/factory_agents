"""Onboarded apps live in the workspace as execution environments: reuse them, list them, keep one per app."""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from conftest import make_config
from factory.app_environments import DISCOVERY_SHA256, AppEnvironments
from factory.onboarding import AppOnboarding
from factory.test_environments import ensure_environment, environment_config, fingerprint
from factory.web.app import create_app

OPEN = {"type": "cloud", "networking": {"type": "limited", "allowed_hosts": [], "allow_package_managers": False}}


def env(env_id, name, created, metadata=None, config=OPEN, archived=None):
    return SimpleNamespace(id=env_id, name=name, created_at=created, updated_at=created, archived_at=archived,
                           metadata=dict(metadata or {}), config=SimpleNamespace(model_dump=lambda exclude_none=True: config))


class Workspace:
    """Environments and running sessions as the platform reports them; records every write."""

    def __init__(self, envs, running=()):
        self.envs = {e.id: e for e in envs}
        self.running, self.archived, self.updates, self.created = list(running), [], [], []
        self.beta = SimpleNamespace(
            environments=SimpleNamespace(list=self._list, retrieve=lambda i, timeout=None: self.envs[i], create=self._create,
                                         update=self._update, archive=self._archive),
            sessions=SimpleNamespace(list=self._sessions))

    def _list(self, limit=None, include_archived=False, timeout=None):
        return SimpleNamespace(data=[e for e in self.envs.values() if include_archived or not e.archived_at], has_next_page=lambda: False)

    def _create(self, name, config, metadata, timeout=None):
        e = env(f"env_new{len(self.created)}", name, "2026-10-11T12:00:00Z", metadata, config)
        self.envs[e.id] = e
        self.created.append(e.id)
        return e

    def _update(self, env_id, metadata, timeout=None):
        self.envs[env_id].metadata.update(metadata)
        self.updates.append((env_id, metadata))

    def _archive(self, env_id, timeout=None):
        self.envs[env_id].archived_at = "2026-10-11T12:00:00Z"
        self.archived.append(env_id)

    def _sessions(self, statuses=None, limit=None, timeout=None):
        assert statuses == ["running", "rescheduling"]
        return SimpleNamespace(data=[SimpleNamespace(environment_id=i) for i in self.running], has_next_page=lambda: False)


def screenshot_workspace(running=()):
    """The workspace from the report: a duplicated discovery environment, three Evergreen recipes, two proofs, and
    the hand-applied factory-cloud environment Factory must never touch."""
    test = lambda d: {"factory_purpose": "test", "profile_sha256": d}
    return Workspace([
        env("env_disc_new", "factory-app-discovery-ec7d7bc4-ac66c1", "2026-10-11T11:59:00Z", test(DISCOVERY_SHA256)),
        env("env_chat_new", "factory-evergreen-chat-31c1bb52-1558f3", "2026-10-11T10:00:00Z", test("31c1bb52" + "0" * 56)),
        env("env_chat_old", "factory-evergreen-chat-88a90ddb-37cb1b", "2026-10-11T09:50:00Z", test("88a90ddb" + "0" * 56)),
        env("env_synthetic", "factory-evergreen-synthetic-94d1e535-b44912", "2026-10-11T09:00:00Z", test("94d1e535" + "0" * 56)),
        env("env_proof", "factory-factory-onboarding-proof-d2b3a5d6-53a889", "2026-10-11T09:00:00Z", test("d2b3a5d6" + "0" * 56)),
        env("env_disc_old", "factory-app-discovery-ec7d7bc4-a2bba6", "2026-10-11T09:00:00Z", test(DISCOVERY_SHA256)),
        env("env_browser", "factory-greenfield-browser-proof-8f307277-db1146", "2026-10-11T09:00:00Z", test("8f307277" + "0" * 56)),
        env("env_cloud", "factory-cloud", "2026-10-10T19:00:00Z", {}),
    ], running)


@pytest.fixture
def setup(tmp_path, root, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def make(workspace):
        onboarding = AppOnboarding(make_config(root, tmp_path / "runs", gate_surface="web", environment_id="env_cloud"), client=workspace)
        return onboarding, AppEnvironments(onboarding.cfg, onboarding)
    return make


def local_app(onboarding, *, environment_id, status="ready", history=(), repository="https://github.com/me/evergreen"):
    """A Console record for an app verified in `environment_id`, with earlier verification requests in `history`."""
    r = onboarding.create(kind="existing", name="Evergreen", repository_url=repository, description="Chat", budget_usd=10)
    folder = onboarding.folder(r["app_id"])
    for i, environment in enumerate([*history, environment_id]):
        (folder / f"evidence-{i}").mkdir()
        (folder / f"evidence-{i}" / "request.json").write_text(json.dumps({"environment_id": environment}))
    onboarding.update(r["app_id"], status=status, verification={"status": "passed" if status != "verifying" else None,
                                                                 "environment_id": environment_id})
    return r["app_id"]


def test_the_apps_tab_reads_one_app_per_recipe_from_the_workspace(setup):
    _, environments = setup(screenshot_workspace())
    view = environments.snapshot()
    assert view["configured"] and view["error"] is None
    assert {a["name"]: a["environment"]["id"] for a in view["apps"]} == {
        "evergreen-chat": "env_chat_new", "evergreen-synthetic": "env_synthetic",
        "factory-onboarding-proof": "env_proof", "greenfield-browser-proof": "env_browser"}
    assert view["discovery"]["id"] == "env_disc_new"
    assert {e["id"]: e["reason"] for e in view["retire"]} == {"env_disc_old": "duplicate", "env_chat_old": "superseded"}
    assert "env_cloud" not in {e["id"] for e in view["environments"]}
    assert view["apps"][0]["environment"]["console_url"].endswith("/environments/" + view["apps"][0]["environment"]["id"])


def test_this_consoles_records_join_an_apps_recipes_and_keep_its_verified_one(setup):
    workspace = screenshot_workspace()
    onboarding, environments = setup(workspace)
    app_id = local_app(onboarding, environment_id="env_chat_old", history=["env_synthetic", "env_chat_new"])
    view = environments.snapshot()
    evergreen = [a for a in view["apps"] if a["local_app_id"] == app_id]
    assert len(evergreen) == 1 and evergreen[0]["environment"]["id"] == "env_chat_old"
    assert evergreen[0]["repository_url"] == "https://github.com/me/evergreen" and evergreen[0]["environment_count"] == 3
    assert {e["id"] for e in view["retire"]} == {"env_disc_old", "env_chat_new", "env_synthetic"}


def test_running_sessions_pin_their_environment(setup):
    _, environments = setup(screenshot_workspace(running=["env_chat_old", "env_disc_old"]))
    view = environments.snapshot()
    assert not {e["id"] for e in view["retire"]} & {"env_chat_old", "env_disc_old"}


def test_cleanup_archives_only_confirmed_leftovers_rechecked_now(setup):
    workspace = screenshot_workspace()
    _, environments = setup(workspace)
    out = environments.cleanup(["env_chat_old", "env_disc_old", "env_cloud", "env_chat_new"])
    assert sorted(workspace.archived) == ["env_chat_old", "env_disc_old"]
    assert sorted(out["skipped"]) == ["env_chat_new", "env_cloud"]
    assert out["environments"]["retire"] == []
    with pytest.raises(HTTPException) as exc:
        environments.cleanup(["env_chat_old"])
    assert exc.value.status_code == 409


def test_one_app_environment_can_be_retired_but_not_one_in_use_or_verified_here(setup):
    workspace = screenshot_workspace(running=["env_proof"])
    onboarding, environments = setup(workspace)
    local_app(onboarding, environment_id="env_browser", repository="https://github.com/me/proof")
    for environment_id, code in (("env_proof", 409), ("env_browser", 409), ("env_cloud", 404), ("env_disc_new", 404)):
        with pytest.raises(HTTPException) as exc:
            environments.archive(environment_id)
        assert exc.value.status_code == code
    environments.archive("env_synthetic")
    assert workspace.archived == ["env_synthetic"]


def test_a_verified_app_settles_to_one_labelled_environment(setup):
    workspace = screenshot_workspace(running=["env_proof"])
    onboarding, environments = setup(workspace)
    for e, repo in (("env_chat_old", "https://github.com/me/evergreen"), ("env_synthetic", "https://github.com/me/evergreen"),
                    ("env_proof", "https://github.com/me/evergreen"), ("env_browser", "https://github.com/me/other")):
        workspace.envs[e].metadata["factory_repository"] = repo
    app_id = local_app(onboarding, environment_id="env_chat_new")
    retired = environments.settle(onboarding.get(app_id), "env_chat_new")
    assert sorted(retired) == ["env_chat_old", "env_synthetic"]          # env_proof is running; another app is untouched
    labels = workspace.envs["env_chat_new"].metadata
    assert labels["factory_app_id"] == app_id and labels["factory_app_name"] == "Evergreen" and labels["factory_verified_at"]
    assert labels["factory_repository"] == "https://github.com/me/evergreen"


def test_another_checkout_reuses_the_workspace_definition_instead_of_creating_one(tmp_path, root):
    profile = json.loads((root / "examples/greenfield-browser/test_environment.json").read_text())
    digest = fingerprint(profile)
    workspace = Workspace([env("env_there", "factory-greenfield-browser-proof-x", "2026-10-11T09:00:00Z",
                               {"factory_purpose": "test", "profile_sha256": digest}, environment_config(profile))])
    app = {"app_id": "app_0123456789ab", "name": "Proof", "repository_url": "https://github.com/me/proof"}
    assert ensure_environment(workspace, profile, tmp_path / "cache", app=app) == "env_there"
    assert workspace.created == [] and workspace.envs["env_there"].metadata["factory_app_id"] == "app_0123456789ab"
    # A definition someone edited by hand, or archived, is left alone: a fresh one is created.
    workspace.envs["env_there"].config = SimpleNamespace(model_dump=lambda exclude_none=True: {**OPEN, "networking": {"type": "unrestricted"}})
    assert ensure_environment(workspace, profile, tmp_path / "other-cache") == "env_new0"
    assert workspace.envs["env_new0"].metadata == {"factory_purpose": "test", "profile_sha256": digest}
    # A cached definition someone deleted in the Console is looked up afresh, not a failed verification.
    deleted = workspace.envs.pop("env_new0")

    def gone(env_id, timeout=None):
        raise type("NotFound", (Exception,), {"status_code": 404})()
    workspace.beta.environments.retrieve = gone
    assert ensure_environment(workspace, profile, tmp_path / "other-cache") == "env_new1" and deleted.id == "env_new0"


def test_an_environment_labelled_for_another_app_keeps_its_owner(tmp_path, root):
    profile = json.loads((root / "examples/greenfield-browser/test_environment.json").read_text())
    workspace = Workspace([env("env_shared", "factory-x", "2026-10-11T09:00:00Z",
                               {"factory_purpose": "test", "profile_sha256": fingerprint(profile), "factory_app_id": "app_aaaaaaaaaaaa"},
                               environment_config(profile))])
    ensure_environment(workspace, profile, tmp_path, app={"app_id": "app_bbbbbbbbbbbb", "name": "B", "repository_url": "https://github.com/me/b"})
    assert workspace.envs["env_shared"].metadata["factory_app_id"] == "app_aaaaaaaaaaaa" and workspace.updates == []


def test_discovery_reuses_a_legacy_discovery_environment_and_marks_it(tmp_path):
    from factory.app_environments import DISCOVERY_PROFILE
    workspace = Workspace([env("env_disc", "factory-app-discovery-ec7d7bc4-a2bba6", "2026-10-11T09:00:00Z",
                               {"factory_purpose": "test", "profile_sha256": DISCOVERY_SHA256})])
    assert ensure_environment(workspace, DISCOVERY_PROFILE, tmp_path, purpose="discovery") == "env_disc"
    assert workspace.created == [] and workspace.envs["env_disc"].metadata["factory_purpose"] == "discovery"


def test_api_lists_environments_and_guards_cleanup(tmp_path, root, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = make_config(root, tmp_path / "runs", gate_surface="web")
    app = create_app(cfg, demo_thread=False)
    with TestClient(app) as c:
        assert c.get("/api/apps/environments").json()["configured"] is False
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        workspace = screenshot_workspace()
        app.state.onboarding._client = workspace
        view = c.get("/api/apps/environments?refresh=true").json()
        assert len(view["apps"]) == 4 and len(view["retire"]) == 2
        body = {"environment_ids": ["env_chat_old"]}
        assert c.post("/api/apps/environments/cleanup", json=body, headers={"Origin": "https://evil.example"}).status_code == 403
        assert workspace.archived == []
        assert c.post("/api/apps/environments/cleanup", json=body).json()["archived"] == ["env_chat_old"]
        assert c.post("/api/apps/environments/env_cloud/archive", json={}).status_code == 404


def test_only_a_passing_verification_retires_the_apps_other_environments(setup, monkeypatch, tmp_path):
    onboarding, _ = setup(screenshot_workspace())
    app_id = onboarding.create(kind="existing", name="Evergreen", repository_url="https://github.com/me/evergreen",
                               description="Chat", budget_usd=10)["app_id"]
    plan = {"profile": {"secrets": []}, "profile_path": "test_environment.json", "limitations": [], "capabilities": []}
    onboarding.update(app_id, plan=plan, plan_sha256="p", source_sha="s")
    monkeypatch.setattr(onboarding, "_candidate", lambda r: tmp_path)
    settled = []
    monkeypatch.setattr(AppEnvironments, "settle", lambda self, app, environment_id: settled.append(environment_id) or ["env_chat_old"])

    def hosted(status):
        def run(cfg, repo, out, **kw):
            assert kw["app"]["app_id"] == app_id
            out.mkdir(parents=True)
            return {"status": status, "environment_id": "env_chat_new", "artifacts": [], "detail": "did not pass"}
        return run
    monkeypatch.setattr("factory.onboarding.run_hosted", hosted("blocked"))
    onboarding._verify(app_id, 1)
    assert settled == [] and onboarding.get(app_id)["status"] == "failed"
    monkeypatch.setattr("factory.onboarding.run_hosted", hosted("passed"))
    onboarding._verify(app_id, 1)
    r = onboarding.get(app_id)
    assert settled == ["env_chat_new"] and r["status"] == "ready"
    assert r["environment_id"] == "env_chat_new" and r["retired_environments"] == ["env_chat_old"]
