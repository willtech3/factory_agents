"""Local verification: the per-run virtual environment, process-group command runs, the manifest-entrypoint preview.

Everything but the last test runs offline on a stdlib-only repository (a `python -m http.server` entrypoint). The last
test installs the sample app's declared dependencies into the per-run environment (uv or pip; needs the package index
or a warm cache, hence `@pytest.mark.network`: deselect with `-m "not network"`) and is the one that proves `pytest`
and `uvicorn` resolve inside that environment, not the orchestrator's."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from factory.stages.implement import (SETUP_CHECK, LocalPreview, LocalVerifier, deterministic_findings, is_environmental,
                                      orchestrator_bin, verification_env)

requires_curl = pytest.mark.skipif(not shutil.which("curl"), reason="the e2e entries use curl")

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
    path = verification_env(v.env_bin)["PATH"].split(os.pathsep)
    assert path[0] == str(v.env_bin)
    if orchestrator_bin() is not None:            # pytest runs from a venv: that venv is gone from PATH, the system's tools are not
        assert str(orchestrator_bin()) not in path
    assert shutil.which("sleep", path=os.pathsep.join(path[1:])) is not None
    # a second call is a refresh, not a rebuild
    assert v.prepare_environment(repo, HTTP_MANIFEST).passed


def test_verification_env_drops_the_orchestrators_venv_but_keeps_the_system_bin(tmp_path, monkeypatch):
    """A venv's `python` is a symlink to the base interpreter. Resolving it named `/usr/bin`, which then left PATH and
    took curl, git and the coreutils with it: every target command exited 127 on Linux (the three tests below, and
    the implement loop's local-preview round, failed that way). The orchestrator's own venv leaves PATH, the base
    interpreter's directory stays, the per-run environment comes first, and leaking Python variables are dropped."""
    orch, base, run_env = tmp_path / "orch", tmp_path / "usr", tmp_path / "run" / "venv"
    (orch / "bin").mkdir(parents=True)
    (base / "bin").mkdir(parents=True)
    (base / "bin" / "python3").write_text("")
    (orch / "bin" / "python").symlink_to(base / "bin" / "python3")
    monkeypatch.setattr(sys, "prefix", str(orch))
    monkeypatch.setattr(sys, "base_prefix", str(base))
    monkeypatch.setattr(sys, "executable", str(orch / "bin" / "python"))
    monkeypatch.setenv("PATH", os.pathsep.join([str(orch / "bin"), str(base / "bin"), "/other/bin"]))
    monkeypatch.setenv("PYTHONPATH", "/leaks/the/orchestrator")
    monkeypatch.setenv("VIRTUAL_ENV", str(orch))
    assert orchestrator_bin() == orch / "bin"
    env = verification_env(run_env / "bin")
    # macOS may insert the documented modern-sh shim between the run venv and system tools.
    from factory.stages.implement import verification_shell
    shim = verification_shell(run_env.parent)[1]
    expected = [str(run_env / "bin"), *([shim] if shim else []), str(base / "bin"), "/other/bin"]
    assert env["PATH"].split(os.pathsep) == expected
    assert "PYTHONPATH" not in env and env["VIRTUAL_ENV"] == str(run_env)
    # from a system interpreter nothing is removed: the per-run environment coming first is the whole protection
    monkeypatch.setattr(sys, "prefix", str(base))
    assert orchestrator_bin() is None
    assert verification_env(None)["PATH"].split(os.pathsep) == [str(orch / "bin"), str(base / "bin"), "/other/bin"]


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


@requires_curl
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


@pytest.mark.network
@requires_curl
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
    which bash 3.2 brace-expands. With a bash 4+ on the host a spec command runs under it, and so does a nested `sh -c`
    (the per-run environment puts an `sh` that is that bash first on PATH)."""
    import re

    from factory.checks import CheckResult
    from factory.stages import implement as impl
    from factory.stages.implement import verification_shell
    impl._SHELL.clear()
    work = tmp_path / "v"
    if verification_shell(work)[0] is None:
        pytest.skip("no bash 4+ on this host")
    v = LocalVerifier("unused", work)
    v.env_dir.symlink_to(Path(sys.executable).parent.parent, target_is_directory=True)      # a prepared per-run environment
    v.setup = CheckResult(SETUP_CHECK, True, "the test's own interpreter stands in for the per-run environment")
    versions = """echo "outer=${BASH_VERSINFO:-0}"; sh -c 'echo "nested=${BASH_VERSINFO:-0}"'"""
    construct = """sh -c '
set -eu
K=abc
f() { printf "%s|" "$@"; }
out="$(f -d "{\\"key\\":\\"x$K\\",\\"r\\":1}")"
test "$out" = "-d|{\\"key\\":\\"xabc\\",\\"r\\":1}|"
echo construct ok
'"""
    res = v.run_commands(tmp_path, [versions, construct])
    seen = dict(re.findall(r"(outer|nested)=(\d+)", res[0]["stdout_tail"]))
    assert int(seen["outer"]) >= 4 and int(seen["nested"]) >= 4, res[0]
    assert res[1]["exit_code"] == 0 and "construct ok" in res[1]["stdout_tail"], res[1]
