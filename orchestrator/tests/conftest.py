import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "ledgerline"
SCHEMAS = ROOT / "schemas"


@pytest.fixture(autouse=True)
def _no_shell_shim_in_checkout():
    """No test may leave the verifier's `shell-shim/` (an `sh` symlink to the host's bash) in the orchestrator
    directory: it belongs under a verifier workdir, and a host-specific symlink in the checkout gets committed."""
    shim = ROOT / "orchestrator" / "shell-shim"
    before = shim.exists()
    yield
    assert before or not shim.exists(), f"this test created {shim}; give the verifier a tmp_path workdir"


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    """Commits the tests (and the code under test) make never depend on the developer's global git configuration: a
    fixed identity, and no commit signing even where the global config asks for it."""
    for key, value in {"GIT_AUTHOR_NAME": "factory-tests", "GIT_AUTHOR_EMAIL": "tests@example.invalid",
                       "GIT_COMMITTER_NAME": "factory-tests", "GIT_COMMITTER_EMAIL": "tests@example.invalid",
                       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "commit.gpgsign", "GIT_CONFIG_VALUE_0": "false"}.items():
        monkeypatch.setenv(key, value)


@pytest.fixture(scope="session")
def root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def schemas_dir() -> Path:
    return SCHEMAS


@pytest.fixture(scope="session")
def examples() -> dict:
    return {p.stem: json.loads(p.read_text()) for p in EXAMPLES.glob("*.json")}


def make_config(root: Path, runs_dir: Path, **overrides):
    """A Config for tests: GitHub forge, the web (file queue) gate surface, platform previews, no secrets."""
    from factory.config import Config

    base = dict(agents={}, environment_id="", vault_id=None, forge="github", forge_url="https://github.com",
                forge_token=None, forge_repo="ledgerline/app", gate_surface="web", page_publisher="local",
                linear_api_key=None, linear_team_id=None, linear_project_id=None, deploy_workflow=None,
                service_manifest_path="service_manifest.json", dev_environment_name="dev",
                preview_environment_prefix="preview/", preview_mode="platform", smoke_commands=(), console_workspace="w", workspace_id=None, runs_dir=runs_dir,
                schemas_dir=root / "schemas", rubrics_dir=root / "rubrics", templates_dir=root / "templates",
                skills_dir=root / "skills", pin_agent_versions=True)
    base.update(overrides)
    return Config(**base)
