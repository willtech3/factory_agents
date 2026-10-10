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


def test_e2e_commands_see_base_url_in_the_environment_including_the_guarded_form(tmp_path):
    """Live greenfield run: the spec guarded with ${BASE_URL:?BASE_URL is required} inside sh -c and the
    orchestrator's textual substitution turned it into ${http://...:?...}, a shell error on a passing check."""
    from factory.stages.implement import LocalVerifier
    v = LocalVerifier("unused", tmp_path)
    entries = [{"command": "sh -c ': \"${BASE_URL:?BASE_URL is required}\"; echo ok $BASE_URL'", "target": "preview", "covers_ac": ["AC-1"]},
               {"command": "echo ${BASE_URL}", "target": "preview", "covers_ac": []}]
    res = v.run_e2e("http://127.0.0.1:4321", entries, repo=tmp_path)
    assert [r["exit_code"] for r in res] == [0, 0]
    assert "ok http://127.0.0.1:4321" in res[0]["stdout_tail"] and res[1]["stdout_tail"].strip() == "http://127.0.0.1:4321"
    unrun = v.run_e2e(None, entries[:1], repo=tmp_path)
    assert unrun[0]["exit_code"] is None


def test_verifier_runs_spec_commands_under_a_modern_shell(tmp_path):
    """The construct that failed on macOS /bin/sh (bash 3.2): escaped quotes inside "$( ... )" inside a nested sh -c,
    which bash 3.2 brace-expands. With a bash 4+ on the host the verifier uses it, and as `sh` too."""
    import pytest
    from factory.stages import implement as impl
    from factory.stages.implement import LocalVerifier, verification_shell
    impl._SHELL = None
    exe, shim = verification_shell(tmp_path)
    if exe is None:
        pytest.skip("no bash 4+ on this host")
    assert (tmp_path / "shell-shim" / "sh").is_symlink()
    v = LocalVerifier("unused", tmp_path)
    cmd = """sh -c '
set -eu
K=abc
f() { printf "%s|" "$@"; }
out="$(f -d "{\\"key\\":\\"x$K\\",\\"r\\":1}")"
test "$out" = "-d|{\\"key\\":\\"xabc\\",\\"r\\":1}|"
echo "shell ok $(bash -c "echo ${BASH_VERSINFO[0]}")"
'"""
    res = v.run_commands(tmp_path, [cmd])
    assert res[0]["exit_code"] == 0, res[0]
    assert "shell ok" in res[0]["stdout_tail"]
    impl._SHELL = None
