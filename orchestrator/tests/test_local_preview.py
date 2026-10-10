"""Local verification: the per-run virtual environment, process-group command runs, the manifest-entrypoint preview.

Everything but the last test runs offline on a stdlib-only repository (a `python -m http.server` entrypoint). The last
test installs the sample app's declared dependencies into the per-run environment (uv or pip; needs the package index
or a warm cache) and is the one that proves `pytest` and `uvicorn` resolve inside that environment, not the
orchestrator's."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from factory.stages.implement import (SETUP_CHECK, LocalPreview, LocalVerifier, deterministic_findings, is_environmental,
                                      verification_env)

HTTP_MANIFEST = {"service": "stdlib-http", "runtime": {"kind": "python", "entrypoint": "python -m http.server 8000 --bind 127.0.0.1", "port": 8000},
                 "health": {"path": "/"}, "env": []}


def _stdlib_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "index.html").write_text("<p>ok</p>")
    return repo


def test_prepare_environment_builds_a_venv_that_comes_first_on_path(tmp_path):
    repo = _stdlib_repo(tmp_path)
    v = LocalVerifier("unused", tmp_path / "verify")
    assert v.env_bin is None
    check = v.prepare_environment(repo, HTTP_MANIFEST)
    assert check.check == SETUP_CHECK and check.passed, check.detail
    assert "nothing installed" in check.detail and check.data["env_dir"] == str(tmp_path / "verify" / "venv")
    assert v.env_bin == tmp_path / "verify" / "venv" / "bin" and (v.env_bin / "python").exists()
    # the target repository's commands see that environment, not the orchestrator's
    out = v.run_commands(repo, ["python -c 'import sys; print(sys.prefix)'", "echo $VIRTUAL_ENV"])
    assert [r["exit_code"] for r in out] == [0, 0]
    assert Path(out[0]["stdout_tail"].strip()).resolve() == (tmp_path / "verify" / "venv").resolve()
    assert out[1]["stdout_tail"].strip() == str(tmp_path / "verify" / "venv")
    own_bin = str(Path(sys.executable).resolve().parent)
    assert own_bin not in verification_env(v.env_bin)["PATH"].split(os.pathsep)
    assert verification_env(v.env_bin)["PATH"].split(os.pathsep)[0] == str(v.env_bin)
    # a second call is a refresh, not a rebuild
    assert v.prepare_environment(repo, HTTP_MANIFEST).passed


def test_a_failing_setup_is_a_deterministic_check_and_nothing_else_runs(tmp_path):
    repo = _stdlib_repo(tmp_path)
    v = LocalVerifier("unused", tmp_path / "verify")
    manifest = {**HTTP_MANIFEST, "runtime": {**HTTP_MANIFEST["runtime"], "setup": "echo installing >&2; echo no such package >&2; exit 3"}}
    check = v.prepare_environment(repo, manifest)
    assert not check.passed and "runtime.setup" in check.detail and "exit 3" in check.detail and "no such package" in check.detail
    assert v.env_bin is None
    out = v.run_commands(repo, ["echo should-not-run"])
    assert out[0]["exit_code"] is None and SETUP_CHECK in out[0]["stderr_tail"]
    # the setup failure is the one finding; the command check it kept from running is a consequence, not a second defect
    from factory.checks import CheckResult, commands_passed
    fs = deterministic_findings([check, commands_passed(out), CheckResult("diff_scope", True, "ok")], out)
    assert [f["finding_id"] for f in fs] == [f"DET-{SETUP_CHECK}"] and "fresh virtual environment" in fs[0]["suggested_fix"]
    # a passing setup command runs inside the environment and from the repository root
    manifest["runtime"]["setup"] = "python -c 'import sys; print(sys.prefix)' > setup-ran.txt"
    check = v.prepare_environment(repo, manifest)
    assert check.passed, check.detail
    assert Path((repo / "setup-ran.txt").read_text().strip()).resolve() == (tmp_path / "verify" / "venv").resolve()


def test_each_command_runs_in_its_own_process_group_and_is_killed_afterwards(tmp_path):
    repo = _stdlib_repo(tmp_path)
    v = LocalVerifier("unused", tmp_path / "verify")
    v.prepare_environment(repo, HTTP_MANIFEST)
    t0 = time.monotonic()
    out = v.run_commands(repo, ["sleep 300 & echo $!; echo started", "sleep 30"], timeout=2)
    assert time.monotonic() - t0 < 15
    pid = int(out[0]["stdout_tail"].split()[0])
    assert out[0]["exit_code"] == 0 and "started" in out[0]["stdout_tail"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("the backgrounded sleep outlived its command")
    assert out[1]["exit_code"] == 124 and "timed out after 2s" in out[1]["stderr_tail"]


def test_environmental_failures_are_tagged(tmp_path):
    from factory.checks import commands_passed, e2e_passed
    cmds = [{"command": "python e2e.py", "exit_code": 2, "stderr_tail": "ERROR: [Errno 48] error while attempting to bind on address ('127.0.0.1', 8765): address already in use"}]
    assert is_environmental(cmds) and not is_environmental([{**cmds[0], "stderr_tail": "AssertionError"}])
    fs = deterministic_findings([commands_passed(cmds)], cmds)
    assert fs[0]["environmental"] is True and "already in use" in fs[0]["suggested_fix"] and "free port" in fs[0]["suggested_fix"]
    e2e = [{"command": "curl $BASE_URL/x", "target": "preview", "covers_ac": ["AC-1"], "exit_code": 1, "stdout_tail": "EADDRINUSE"}]
    fs = deterministic_findings([e2e_passed(e2e, "preview")], [], e2e)
    assert fs[0]["finding_id"] == "DET-e2e_preview" and fs[0]["environmental"] is True and "curl $BASE_URL/x" in fs[0]["evidence"]["reproduction"]
    plain = deterministic_findings([commands_passed([{"command": "pytest", "exit_code": 1, "stderr_tail": "1 failed"}])], [])
    assert "environmental" not in plain[0]


def test_local_preview_runs_the_entrypoint_in_the_per_run_environment(tmp_path):
    repo = _stdlib_repo(tmp_path)
    v = LocalVerifier("unused", tmp_path / "verify")
    assert v.prepare_environment(repo, HTTP_MANIFEST).passed
    with LocalPreview(repo, HTTP_MANIFEST, timeout=20, env_bin=v.env_bin) as url:
        assert url.startswith("http://127.0.0.1:")
        results = v.run_e2e(url, [{"command": "curl -fsS $BASE_URL/index.html", "target": "preview", "covers_ac": ["AC-1"]},
                                  {"command": "curl -fsS $BASE_URL/nope", "target": "preview", "covers_ac": ["AC-2"]},
                                  {"command": "python -c 'import sys,os; print(sys.prefix); print(os.environ[\"BASE_URL\"])'",
                                   "target": "preview", "covers_ac": ["AC-3"]}], repo=repo)
    assert [r["exit_code"] for r in results] == [0, 22, 0] and "ok" in results[0]["stdout_tail"]
    prefix, base = results[2]["stdout_tail"].split()
    assert Path(prefix).resolve() == (tmp_path / "verify" / "venv").resolve() and base == url
    # the server is gone once the preview stops
    import urllib.request
    with pytest.raises(Exception):
        urllib.request.urlopen(url + "/index.html", timeout=1)


@pytest.mark.skipif(not (shutil.which("uv") or shutil.which("pip3")), reason="needs an installer")
def test_sample_app_runs_from_its_own_pyproject_not_the_orchestrator_venv(tmp_path, root):
    """The sample app declares fastapi/uvicorn/pydantic and dev pytest/httpx; the per-run environment is built from
    that pyproject, and `python -m pytest` plus the uvicorn entrypoint resolve there (network or a warm uv cache)."""
    src = root / "examples" / "sample-app"
    repo = tmp_path / "repo"
    shutil.copytree(src, repo, ignore=shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache"))
    manifest = json.loads((repo / "service_manifest.json").read_text())
    v = LocalVerifier("unused", tmp_path / "verify")
    check = v.prepare_environment(repo, manifest)
    assert check.passed, check.detail
    assert "pip install -e .[dev]" in check.detail
    out = v.run_commands(repo, ["python -m pytest -q", "python -c 'import fastapi, sys; print(sys.prefix)'"], timeout=300)
    assert out[0]["exit_code"] == 0, out[0]["stderr_tail"] + out[0]["stdout_tail"]
    assert Path(out[1]["stdout_tail"].strip()).resolve() == (tmp_path / "verify" / "venv").resolve()
    with LocalPreview(repo, manifest, timeout=40, env_bin=v.env_bin) as url:
        results = v.run_e2e(url, [{"command": "curl -fsS $BASE_URL/health", "target": "preview", "covers_ac": ["AC-1"]}], repo=repo)
    assert results[0]["exit_code"] == 0 and "ok" in results[0]["stdout_tail"]
    assert subprocess.run([str(v.env_bin / "python"), "-c", "import uvicorn"], capture_output=True).returncode == 0
