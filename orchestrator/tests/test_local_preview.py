"""The local preview fallback starts the sample app from its manifest and the e2e runner hits it. Local only."""
import json
import shutil
import subprocess
from pathlib import Path

from factory.stages.implement import LocalPreview, LocalVerifier


def test_local_preview_serves_the_sample_app(tmp_path, root):
    src = root / "examples" / "sample-app"
    repo = tmp_path / "repo"
    shutil.copytree(src, repo, ignore=shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache"))
    manifest = json.loads((repo / "service_manifest.json").read_text())
    with LocalPreview(repo, manifest, timeout=40) as url:
        assert url.startswith("http://127.0.0.1:")
        verifier = LocalVerifier("unused", tmp_path / "v")
        results = verifier.run_e2e(url, [{"command": "curl -fsS $BASE_URL/health", "target": "preview", "covers_ac": ["AC-1"]},
                                         {"command": "curl -fsS $BASE_URL/nope", "target": "preview", "covers_ac": ["AC-2"]}], repo=repo)
    assert [r["exit_code"] for r in results] == [0, 22] and "ok" in results[0]["stdout_tail"]
    # the orchestrator's own interpreter is on PATH for test commands, so `pytest` resolves inside the checkout
    out = verifier.run_commands(repo, ["python -m pytest -q"], timeout=120)
    assert out[0]["exit_code"] == 0, out[0]["stderr_tail"] + out[0]["stdout_tail"]
