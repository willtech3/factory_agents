"""The controller's own verification host for preview modes "local" and "platform": a clean clone of the target
repository, a per-run virtual environment built from it, and each command in its own process group.

`verification.verify` is the only caller that runs anything here; the implement loop reads its `EvidenceBundle`. Nothing
here runs in preview mode "managed", where every execution happens in the hosted sandbox (`hosted_verifier`). The test
commands run with the same reporters, and their reports are read with the same functions, as in the hosted sandbox
(`sandbox_tools.environment_runner.instrumented_command`, `reported_outcomes`), so a Bun test yields evidence on either host.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import checks, size, visual
from .checks import CheckResult
# One definition each, shared with the hosted harness that runs the same collection and reads the same JUnit reports.
from .sandbox_tools.environment_runner import (NODE_ID_PLUGIN, collected_node_ids, parse_junit, instrumented_command, reported_outcomes,
                                               write_node_id_plugin)
from .stages.common import git as _git                # one git helper for every stage

__all__ = ["LocalPreview", "LocalVerifier", "SETUP_CHECK", "measure_size", "parse_junit", "verification_env", "verification_shell"]

_SHELL: dict[str, tuple[str | None, str | None]] = {}        # per verifier workdir: (bash executable, shim dir)


def verification_shell(workdir: Path) -> tuple[str | None, str | None]:
    """The shell the verifier runs spec commands with: the newest bash 4+ on the host, plus a directory holding an
    `sh` symlink to it to put first on PATH so a nested `sh -c` in a command gets the same shell.

    macOS ships /bin/sh as bash 3.2, which mis-parses escaped quotes inside "$( ... )" and brace-expands the result;
    the sandbox the agents wrote and tested the command in runs bash 5. Live greenfield run: a passing end-to-end
    check failed with `test: too many arguments` on the orchestrator host for exactly that reason. Returns
    (executable, shim_dir), both None when no bash 4+ exists (then /bin/sh is used and a hint is printed once)."""
    import shutil

    key = str(workdir)
    if key in _SHELL:
        return _SHELL[key]
    candidates = [c for c in (shutil.which("bash"), "/opt/homebrew/bin/bash", "/usr/local/bin/bash", "/bin/bash") if c]
    chosen = None
    for c in candidates:
        try:
            out = subprocess.run([c, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True, timeout=5).stdout.strip()
            if out.isdigit() and int(out) >= 4:
                chosen = c
                break
        except (OSError, subprocess.SubprocessError):
            continue
    if chosen is None:
        print("[verify] no bash 4+ found; spec commands run under /bin/sh (bash 3.2 on macOS, which mis-parses "
              "escaped quotes inside \"$( ... )\"). `brew install bash` fixes it.")
        _SHELL[key] = (None, None)
        return _SHELL[key]
    shim = workdir / "shell-shim"
    shim.mkdir(parents=True, exist_ok=True)
    link = shim / "sh"
    if not link.exists() or os.readlink(link) != chosen:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(chosen)
    _SHELL[key] = (chosen, str(shim))
    return _SHELL[key]



def orchestrator_bin() -> Path | None:
    """The bin directory of the virtual environment the orchestrator itself runs from, or None from a system interpreter.

    Taken from `sys.prefix`, never by resolving `sys.executable`: a venv's `python` is a symlink to the base
    interpreter, so resolving it names the system's own bin directory (`/usr/bin` on Linux), and removing that from
    PATH took `curl`, `git` and the coreutils away from every target-repository command (exit 127)."""
    if sys.prefix == sys.base_prefix:
        return None
    return Path(sys.prefix) / "bin"


def verification_env(env_bin: Path | None) -> dict[str, str]:
    """The environment the target repository's commands run in on the orchestrator host.

    The per-run virtual environment's bin directory comes first on PATH and the orchestrator's own virtual
    environment (`orchestrator_bin`) is removed from it, so `python`, `pytest` and the service entrypoint resolve to
    what the target repository declared, never to the orchestrator's packages (post-mortem 2026-10-10, issue 13).
    The base interpreter's directory stays: it is where the system's tools live. Python environment variables that
    would leak the orchestrator's modules are dropped."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__")}
    own = orchestrator_bin()
    own_resolved = str(own.resolve()) if own is not None else None
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p and str(Path(p).resolve()) != own_resolved]
    if env_bin is not None:
        parts.insert(0, str(env_bin))
        env["VIRTUAL_ENV"] = str(env_bin.parent)
        shim = verification_shell(env_bin.parent.parent)[1]       # `sh` -> a modern bash, for nested sh -c
        if shim:
            parts.insert(1, shim)                                   # after the venv's bin, before everything else
    env["PATH"] = os.pathsep.join(parts)
    return env


class LocalPreview:
    """Start the service from its manifest on a free port, wait for its health path, serve as the preview.

    The development stand-in for the platform's per-branch preview (`FACTORY_PREVIEW=local`): the manifest's
    `runtime.entrypoint` runs from the checkout with the manifest's port replaced by a free one (also exported as
    PORT), and `health.path` must answer 200 within `timeout`. `env_bin` is the per-run virtual environment the
    verifier built from the repository (`LocalVerifier.prepare_environment`); the entrypoint resolves its
    interpreter and packages there, never in the orchestrator's own environment."""

    def __init__(self, repo: Path, manifest: dict[str, Any], timeout: float = 60.0, env_bin: Path | None = None):
        self.repo, self.manifest, self.timeout, self.env_bin = repo, manifest, timeout, env_bin
        self.proc: subprocess.Popen | None = None
        self.url: str | None = None
        self.log = repo.parent / "preview.log"

    def start(self) -> str:
        rt = self.manifest.get("runtime") or {}
        entry = rt.get("entrypoint")
        if not entry:
            raise RuntimeError("service manifest has no runtime.entrypoint; cannot start a local preview")
        port = _free_port()
        cmd = entry.replace(str(rt.get("port") or ""), str(port)) if rt.get("port") else entry
        env = {**verification_env(self.env_bin), "PORT": str(port)}
        self.proc = subprocess.Popen(cmd, shell=True, executable=verification_shell(self.repo.parent)[0], cwd=self.repo, env=env, stdout=self.log.open("w"), stderr=subprocess.STDOUT,
                                     start_new_session=True)
        self.url = f"http://127.0.0.1:{port}"
        health = (self.manifest.get("health") or {}).get("path", "/health")
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"local preview exited with {self.proc.returncode}; see {self.log}")
            try:
                with urllib.request.urlopen(self.url + health, timeout=2) as r:
                    if r.status == (self.manifest.get("health") or {}).get("expect_status", 200):
                        return self.url
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        self.stop()
        raise RuntimeError(f"local preview did not answer {health} within {self.timeout}s; see {self.log}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), 15)
            except Exception:  # noqa: BLE001
                self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def __enter__(self) -> str:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


SETUP_CHECK = checks.SETUP_CHECK
SETUP_TIMEOUT = 900


class LocalVerifier:
    """Orchestrator-side clone used for the authoritative test run and diff analysis.

    Runs the spec's test commands in a clean checkout of head_sha, inside a per-run virtual environment built
    from the target repository (`prepare_environment`), each command in its own process group. In production
    this is a CI job on the branch (GitHub Actions or GitLab CI); the interface is the same.
    """

    def __init__(self, repo_url_with_auth: str, workdir: Path, setup_timeout: int = SETUP_TIMEOUT):
        self.repo_url = repo_url_with_auth
        self.workdir = workdir
        self.setup_timeout = setup_timeout
        self.env_dir = workdir / "venv"          # the per-run environment; rebuilt only when missing
        self.setup: CheckResult | None = None    # the last `prepare_environment` result
        self._logs = 0

    @property
    def env_bin(self) -> Path | None:
        """The environment's bin directory once `prepare_environment` succeeded, else None (ambient PATH)."""
        return self.env_dir / "bin" if self.setup is not None and self.setup.passed else None

    def checkout(self, branch: str) -> Path:
        repo = self.workdir / "repo"
        if not repo.exists():
            subprocess.run(["git", "clone", "--quiet", self.repo_url, str(repo)], check=True)
        _git(repo, "fetch", "--quiet", "origin", branch)
        _git(repo, "checkout", "--quiet", "-B", branch, f"origin/{branch}")
        return repo

    # ------------------------------------------------------------ the per-run environment

    def prepare_environment(self, repo: Path, manifest: dict[str, Any] | None = None) -> CheckResult:
        """Build (or refresh) the virtual environment the target repository's commands run in, as a check.

        A fresh venv under the run's verify/ directory (uv when the host has it, else `python -m venv`), then the
        install step: the manifest's `runtime.setup` command when it declares one; otherwise the repository's
        pyproject (`-e .[dev]` when it is installable, its declared dependency lists when it only names them);
        otherwise nothing, which is a pass with a note. Re-run on every verification so a dependency the
        implementer added in this round is there. A failing step is a failed deterministic check with the
        installer's output, never an exception: the merge judge cannot override it and the implementer answers
        it like any other DET finding (post-mortem 2026-10-10, issue 13)."""
        self.env_dir.parent.mkdir(parents=True, exist_ok=True)
        uv = shutil.which("uv")
        python = self.env_dir / "bin" / "python"
        steps: list[tuple[str, list[str]]] = []
        if not python.exists():
            steps.append(("create venv", [uv, "venv", "--quiet", "--python", sys.executable, str(self.env_dir)] if uv
                          else [sys.executable, "-m", "venv", str(self.env_dir)]))
        setup_cmd = ((manifest or {}).get("runtime") or {}).get("setup")
        plan = _install_plan(repo, setup_cmd, python, uv)
        data: dict[str, Any] = {"env_dir": str(self.env_dir), "installer": "uv" if uv else "pip", "python": sys.version.split()[0],
                                "install": plan["description"]}
        for label, argv in steps:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=self.setup_timeout)
            if p.returncode != 0:
                self.setup = CheckResult(SETUP_CHECK, False, f"{label} failed (exit {p.returncode}): {(p.stderr or p.stdout)[-1500:].strip()}", data)
                return self.setup
        if plan["command"] is not None:
            env = {**verification_env(self.env_dir / "bin"), "UV_PROJECT_ENVIRONMENT": str(self.env_dir), "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
            res = self._run_in_group(plan["command"], repo, env, self.setup_timeout, shell=plan["shell"])
            data["exit_code"] = res["exit_code"]
            if res["exit_code"] != 0:
                tail = (res["stderr_tail"] or res["stdout_tail"] or "").strip()[-1500:]
                self.setup = CheckResult(SETUP_CHECK, False, f"install step `{plan['description']}` failed (exit {res['exit_code']}): {tail}", data)
                return self.setup
        self.setup = CheckResult(SETUP_CHECK, True, f"per-run virtualenv ready ({data['installer']}, python {data['python']}): {plan['description']}", data)
        return self.setup

    # ------------------------------------------------------------ running the target repository's commands

    def _run_in_group(self, cmd: str | list[str], cwd: Path, env: dict[str, str], timeout: int, shell: bool = True) -> dict[str, Any]:
        """Run one command as the leader of its own process group, with output on files rather than pipes.

        After the command exits (or times out, exit 124) the whole group is killed, so a server the command
        backgrounded (`uvicorn ... &`) cannot outlive it and bind the next command's port; files instead of
        pipes mean a leaked child holding stdout open cannot stall the orchestrator either."""
        self._logs += 1
        logs = self.workdir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        out_path, err_path = logs / f"cmd-{self._logs:03d}.out", logs / f"cmd-{self._logs:03d}.err"
        with out_path.open("w") as out, err_path.open("w") as err:
            proc = subprocess.Popen(cmd, shell=shell, executable=verification_shell(self.workdir)[0] if shell else None,
                                    cwd=cwd, env=env, stdout=out, stderr=err, start_new_session=True)
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                code = 124
            finally:
                _kill_group(proc)
        stdout, stderr = out_path.read_text(errors="replace"), err_path.read_text(errors="replace")
        if code == 124:
            stderr += f"\n[orchestrator] timed out after {timeout}s; process group killed"
        return {"exit_code": code, "stdout_tail": stdout[-4000:], "stderr_tail": stderr[-4000:]}

    def analyse(self, repo: Path, base_sha: str, head_sha: str) -> dict[str, Any]:
        names = _git(repo, "diff", "--name-status", f"{base_sha}..{head_sha}").splitlines()
        changed, deleted = [], []
        for line in names:
            status, _, path = line.partition("\t")
            paths = path.split("\t")
            (deleted if status.startswith("D") else changed).append(paths[-1])
            if status.startswith("R") and len(paths) == 2:
                deleted.append(paths[0])               # a rename removes the old path: a renamed existing test is not kept
        added_lines, removed_lines = _diff_lines(_git(repo, "diff", "--unified=0", f"{base_sha}..{head_sha}"))
        existing_tests = [p for p in _git(repo, "ls-tree", "-r", "--name-only", base_sha).splitlines()
                          if checks.is_test_file(p)]
        return {"changed": changed, "deleted": deleted, "added_lines": added_lines, "removed_lines": removed_lines, "existing_tests": existing_tests,
                "patch": _git(repo, "diff", f"{base_sha}..{head_sha}")}

    def run_commands(self, repo: Path, commands: list[str], timeout: int = 1800) -> list[dict[str, Any]]:
        """The spec's `test_plan.commands`, each in its own process group inside the per-run environment.

        When `prepare_environment` failed, nothing is run: every command is recorded with exit_code None and the
        reason, so `test_plan_commands` fails for the stated cause rather than for missing packages.

        Every command runs as the hosted sandbox runs it (`instrumented_command`): each pytest invocation inside it writes a junit
        report (appended through PYTEST_ADDOPTS, so `pytest`, `python -m pytest` and compound shell commands all get
        it), and a direct `bun test` gets Bun's JUnit reporter. A command that produced a report carries `tests:
        {framework, report: "junit", status: "reported", passed, failed, skipped}` with the test ids, which is where the
        orchestrator's passing-test set comes from (post-mortem issue 5: the set used to be copied from the implementer's
        own report, so any mapping passed, fabricated ids included)."""
        results = []
        if self.setup is not None and not self.setup.passed:
            return [{"command": cmd, "exit_code": None, "stdout_tail": "",
                     "stderr_tail": f"not run: {SETUP_CHECK} failed ({self.setup.detail[:300]})"} for cmd in commands]
        env = verification_env(self.env_bin)
        junit_dir = self.workdir / "junit"
        junit_dir.mkdir(parents=True, exist_ok=True)
        for i, cmd in enumerate(commands):
            junit, bun_report = junit_dir / f"cmd-{i}.xml", junit_dir / f"cmd-{i}-bun.xml"
            junit.unlink(missing_ok=True)
            bun_report.unlink(missing_ok=True)
            executed, command_env, bun, bun_reason = instrumented_command(cmd, junit, bun_report, env)
            row: dict[str, Any] = {"command": cmd, **self._run_in_group(executed, repo, command_env, timeout)}
            if bun:
                row["instrumented"] = "bun-junit"
            tests = reported_outcomes(junit, bun_report, bun, bun_reason, str(repo))
            if tests is not None:
                row["tests"] = tests
            results.append(row)
        return results

    def collect_test_ids(self, repo: Path, timeout: int = 300) -> list[str] | None:
        """Every pytest node id the checkout collects (`pytest --collect-only` in the per-run environment, ids printed by
        the harness's own plugin so a repository's `addopts = -v` cannot hide them), or None when pytest cannot collect
        here (no environment, not a pytest repository, a collection error): the mapping is then unverifiable, not verified."""
        if self.setup is not None and not self.setup.passed:
            return None
        plugin_dir = write_node_id_plugin(self.workdir / "pytest_plugin")
        env = verification_env(self.env_bin)
        env["PYTHONPATH"] = str(plugin_dir)      # verification_env drops the orchestrator's PYTHONPATH; only the plugin is added
        try:
            p = subprocess.run(f"python -m pytest --collect-only -q -p {NODE_ID_PLUGIN}", shell=True, cwd=repo, env=env,
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        if p.returncode not in (0, 5):          # 5: no tests collected, which is a (possibly empty) answer
            return None
        return collected_node_ids(p.stdout)

    def run_e2e(self, preview_url: str | None, e2e_entries: list[dict[str, Any]], repo: Path | None = None,
                target: str = "preview", timeout: int = 900) -> list[dict[str, Any]]:
        """The spec's `test_plan.e2e` entries for `target` against a running deployment at `preview_url` (`run_entries`)."""
        return self.run_entries({target: preview_url}, [e for e in e2e_entries if e.get("target", "preview") == target],
                                repo=repo, timeout=timeout)

    def run_entries(self, urls: dict[str, str | None], e2e_entries: list[dict[str, Any]], repo: Path | None = None,
                    timeout: int = 900) -> list[dict[str, Any]]:
        """Run the spec's `test_plan.e2e` entries in their order, each against the instance its target names in `urls`.

        `$BASE_URL` (and the env var BASE_URL) is the environment URL the orchestrator read from the forge's
        deployment record (or the instance it started itself in local mode), never a URL an agent reported.
        Entries are run from the checkout, in the per-run environment and their own process group, so fixtures
        and e2e specs under the repo are available. A `local` entry with no instance starts what it needs itself.
        If a preview's URL is unknown (no preview deploy yet), its entries are recorded with exit_code None, which
        `checks.e2e_passed` treats as a failure: not run is not passed (Anthropic, "Effective harnesses": agents
        declare victory without end-to-end verification).
        """
        results = []
        for e in e2e_entries:
            target = e.get("target", "preview")
            preview_url = urls.get(target)
            row = {"command": e["command"], "target": target, "covers_ac": list(e.get("covers_ac", [])),
                   "base_url": preview_url}
            if target == "local" and self.setup is not None and not self.setup.passed:
                # a local entry runs in the per-run environment, which could not be built
                results.append({**row, "exit_code": None, "stderr_tail": f"not run: {SETUP_CHECK} failed ({self.setup.detail[:300]})"})
                continue
            if target == "preview" and not preview_url:
                results.append({**row, "exit_code": None, "stderr_tail": "no preview URL available for this branch",
                                **({"visual": {**e["visual"], "description": e.get("description"), "status": "blocked",
                                               "summary": "No preview URL available for this branch", "media": []}} if e.get("visual") else {})})
                continue
            # The shell expands $BASE_URL, ${BASE_URL} and ${BASE_URL:?...} from the environment. Substituting the
            # text ourselves broke the guarded form (`${http://127.0.0.1:5xxxx:?...}`) and failed a passing check.
            env = {**verification_env(self.env_bin), "BASE_URL": preview_url or ""}
            cmd = e["command"]
            if e.get("visual"):
                if not preview_url or repo is None:
                    results.append({**row, "exit_code": None, "visual": {"status": "blocked", "summary": "A browser check needs a running app and an exact checkout"}})
                    continue
                head = _git(repo, "rev-parse", "HEAD").strip()
                directory, request = visual.new_request(self.workdir, repo, head, preview_url, e)
                missing = [k for k in e["visual"]["environment"].get("required_env", []) if not env.get(k)]
                if cmd.startswith("factory:journey"):
                    outcome = {"exit_code": None, "stderr_tail": "factory journeys run in the hosted sandbox (FACTORY_PREVIEW=managed)"}
                elif missing:
                    outcome = {"exit_code": None, "stderr_tail": "Missing test environment variables: " + ", ".join(missing)}
                else:
                    env.update(FACTORY_VISUAL_REQUEST=str(directory / "request.json"), FACTORY_EVIDENCE_DIR=str(directory))
                    outcome = (visual.wait_for_browser(directory, timeout) if cmd == "factory:browser" else
                               self._run_in_group(cmd, repo, env, timeout))
                evidence = visual.collect(directory, request, outcome.get("exit_code"))
                if missing or cmd.startswith("factory:journey"):
                    evidence["summary"] = outcome["stderr_tail"]
                (directory / "closed").touch()
                results.append({**row, **outcome, "visual": evidence})
            else:
                results.append({**row, **self._run_in_group(cmd, repo or self.workdir, env, timeout)})
        return results


def measure_size(repo: Path, base_sha: str, head_sha: str, spec: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Hand-written and generated changed lines between two commits (`size.measure_repo`), reading only git: the
    numstat and git's own linguist-generated attributes at the head. `spec` adds the files it declares regenerated."""
    return size.measure_repo(repo, base_sha, head_sha, (spec or {}).get("regenerated") or [])[1]


def _diff_lines(unified: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Added lines per new path and removed lines per old path from `git diff --unified=0`. The ---/+++ headers are read
    only before a file's first hunk, so a content line that itself starts with -- or ++ is never taken for a header. Every
    file whose `--- a/` header was read gets a removed entry, empty when nothing was removed; a file with none (binary,
    quoted path, not in the diff) is one this parse cannot vouch for."""
    added: dict[str, list[str]] = {}
    removed: dict[str, list[str]] = {}
    old = new = None
    in_hunk = False
    for line in unified.splitlines():
        if line.startswith("diff --git "):
            old = new = None
            in_hunk = False
        elif not in_hunk:
            if line.startswith("--- "):
                old = line[6:] if line.startswith("--- a/") else None
                if old is not None:
                    removed.setdefault(old, [])         # parsed: an empty list proves nothing was removed
            elif line.startswith("+++ "):
                new = line[6:] if line.startswith("+++ b/") else None
                if new is not None:
                    added.setdefault(new, [])
            elif line.startswith("@@"):
                in_hunk = True
        elif line.startswith("+") and new is not None:
            added[new].append(line[1:])
        elif line.startswith("-") and old is not None:
            removed.setdefault(old, []).append(line[1:])
    return added, removed


def _kill_group(proc: subprocess.Popen, grace: float = 2.0) -> None:
    """Terminate everything in the command's process group (SIGTERM, then SIGKILL), and reap the leader.

    `start_new_session=True` made the command its own session and group leader, so the group id is its pid
    even once it has been reaped; children it left behind (a backgrounded server) are still in the group."""
    pgid = proc.pid
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break                                   # nothing left in the group
        except PermissionError:  # pragma: no cover
            break
        if proc.poll() is None:                     # reap the leader so a zombie does not count as alive below
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                return                              # gone (macOS answers EPERM for a member that is already a zombie)
            time.sleep(0.05)
    if proc.poll() is None:  # pragma: no cover
        proc.kill()
        proc.wait(timeout=5)


def _install_plan(repo: Path, setup_cmd: str | None, python: Path, uv: str | None) -> dict[str, Any]:
    """What installs the target repository's dependencies into the per-run environment.

    Returns {command, shell, description}; `command` None means nothing to install. Order: the manifest's
    `runtime.setup` (any language, run through the shell); a pyproject with a build system (`-e .` plus whichever
    of the dev/test extras it declares); a pyproject that only names dependencies (installed as a list, the
    repository root being importable through pytest's pythonpath as the platform contract asks); nothing."""
    if setup_cmd:
        return {"command": setup_cmd, "shell": True, "description": f"runtime.setup: {setup_cmd}"}
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return {"command": None, "shell": False, "description": "no pyproject.toml and no runtime.setup: nothing installed"}
    try:
        import tomllib
        meta = tomllib.loads(pyproject.read_text())
    except Exception as e:  # noqa: BLE001  (a broken pyproject is reported by the install step, not here)
        meta = {}
        broken = str(e)[:200]
    else:
        broken = ""
    installer = [uv, "pip", "install", "--quiet", "--python", str(python)] if uv else [str(python), "-m", "pip", "install", "--quiet"]
    project = meta.get("project") or {}
    extras = [x for x in ("dev", "test", "tests") if x in (project.get("optional-dependencies") or {})]
    if broken or "build-system" in meta or "project" not in meta:
        spec = "-e ." + (f"[{','.join(extras)}]" if extras else "")
        return {"command": installer + [spec], "shell": False, "description": f"pip install {spec}"}
    deps = list(project.get("dependencies") or [])
    for x in extras:
        deps.extend(project["optional-dependencies"][x])
    if not deps:
        return {"command": None, "shell": False, "description": "pyproject declares no dependencies: nothing installed"}
    return {"command": installer + deps, "shell": False,
            "description": f"dependencies from pyproject ({', '.join(['project'] + extras)}): {' '.join(deps)}"}


