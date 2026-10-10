import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "ledgerline"
SCHEMAS = ROOT / "schemas"


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
    """A Config for tests: GitHub forge, file gate surface, no secrets."""
    from factory.config import Config

    base = dict(agents={}, environment_id="", vault_id=None, forge="github", forge_url="https://github.com",
                forge_token=None, forge_repo="ledgerline/app", gate_surface="file", page_publisher="local",
                linear_api_key=None, linear_team_id=None, linear_project_id=None, deploy_workflow=None,
                service_manifest_path="service_manifest.json", dev_environment_name="dev",
                preview_environment_prefix="preview/", preview_mode="platform", smoke_commands=(), console_workspace="w", workspace_id=None, runs_dir=runs_dir,
                schemas_dir=root / "schemas", rubrics_dir=root / "rubrics", templates_dir=root / "templates",
                skills_dir=root / "skills", pin_agent_versions=True)
    base.update(overrides)
    return Config(**base)
