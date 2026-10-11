import copy
import json
from pathlib import Path
import subprocess

import pytest

from factory.test_environments import environment_config, fingerprint, read_snapshot_profile, snapshot, validate_profile
from factory.sandbox_tools.environment_runner import check_browser


@pytest.fixture
def profile(root):
    return json.loads((root / "examples/greenfield-browser/test_environment.json").read_text())


def test_existing_and_greenfield_share_contract(profile, schemas_dir):
    assert validate_profile(profile, schemas_dir) == []
    existing = copy.deepcopy(profile)
    existing["services"].append({"id": "database", "port": 3290, "health_path": "/version"})
    existing["secrets"] = [{"name": "PROVIDER_KEY", "required": True, "allowed_hosts": ["provider.example.com"], "injection": "header"}]
    existing["network"]["allowed_hosts"] = ["provider.example.com"]
    assert validate_profile(existing, schemas_dir) == []
    assert environment_config(existing)["networking"]["type"] == "limited"
    assert fingerprint(existing) != fingerprint(profile)


def test_invalid_profile_never_echoes_secret(profile, schemas_dir):
    profile["secrets"] = [{"name": "API_KEY", "required": True, "allowed_hosts": ["api.example.com"], "injection": "header", "value": "do-not-echo-this"}]
    errors = validate_profile(profile, schemas_dir)
    assert errors and "do-not-echo-this" not in str(errors)


def test_secret_scope_and_duplicate_services_rejected(profile, schemas_dir):
    profile["secrets"] = [{"name": "API_KEY", "required": True, "allowed_hosts": ["api.example.com"], "injection": "header"}]
    profile["services"].append(dict(profile["services"][0]))
    assert len(validate_profile(profile, schemas_dir)) == 3


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_snapshot_uses_committed_recipe_never_local_dotenv(tmp_path, profile, schemas_dir):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "synthetic@example.test")
    git(tmp_path, "config", "user.name", "Synthetic test")
    p = tmp_path / "test_environment.json"
    p.write_text(json.dumps(profile))
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "fixture")
    (tmp_path / ".env.local").write_text("SECRET=do-not-upload")
    p.write_text("uncommitted recipe")
    head, source = snapshot(tmp_path)
    assert len(head) == 40 and b"do-not-upload" not in source and b"uncommitted recipe" not in source
    assert read_snapshot_profile(source, "test_environment.json", schemas_dir) == profile
    git(tmp_path, "add", ".env.local")
    git(tmp_path, "commit", "-qm", "bad fixture")
    with pytest.raises(ValueError, match="dotenv"):
        snapshot(tmp_path)


def test_pass_requires_screenshots_viewports_and_demo(tmp_path, profile):
    # Deliberately minimal magic: the independent visual reviewer still assesses pixels.
    (tmp_path / "desktop.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    result = {"status": "passed", "steps": [{"status": "passed", "viewport": {"width": 1280, "height": 800}, "screenshot": "desktop.png"}]}
    p = tmp_path / "browser-result.json"
    p.write_text(json.dumps(result))
    assert check_browser(profile, tmp_path) == (False, "requested viewport coverage is incomplete")
    (tmp_path / "mobile.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    result["steps"].append({"status": "passed", "viewport": {"width": 390, "height": 844}, "screenshot": "mobile.png"})
    p.write_text(json.dumps(result))
    assert check_browser(profile, tmp_path)[1] == "requested demonstration recording is missing"
    result["demo"] = "demo.webm"
    (tmp_path / "demo.webm").write_bytes(b"\x1aE\xdf\xa3test-video")
    p.write_text(json.dumps(result))
    assert check_browser(profile, tmp_path)[0]
    result["steps"][1]["status"] = "blocked"
    p.write_text(json.dumps(result))
    assert not check_browser(profile, tmp_path)[0]
