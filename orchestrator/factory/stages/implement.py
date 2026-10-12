"""Stage 3, per spec: implement -> adversarial review -> respond -> fresh review -> merge judge.

Each box is its own session with its own context. The reviewer in round 2 is a *new*
session that receives the new diff plus the implementer's responses; it is not a
continuation of round 1 (one fresh pass per round; Song, arXiv:2603.16244). The merge
judge receives the orchestrator's deterministic checks and may not override them.

Authoritative verification includes the spec's `test_plan.e2e` entries run against the
branch's **preview environment**. The platform's own CI (GitHub Actions or GitLab CI) deploys
every factory branch to a per-branch preview; the orchestrator reads the preview URL from the
forge's deployment or environment record (`Forge.environment_url_for_branch`) and hands it to
the implementer and reviewer as `preview.json`. No agent deploys anything
(docs/05-platform-and-deployment.md).

Repository access in the sandbox is the forge's business: GitHub sessions get the native
`github_repository` resource (token injected by Anthropic's git proxy), GitLab sessions get the
vault/bearer-header clone instructions of ADR-0003. Both come from `Forge.sandbox_git_instructions`.

`FACTORY_PREVIEW=local` (no platform yet): the orchestrator runs the authoritative verification on its own
host, in a per-run virtual environment it builds from the target repository (`LocalVerifier.prepare_environment`),
and starts the service itself from the manifest entrypoint for the preview entries (`LocalPreview`). No sandbox
can reach that instance, and `preview.json` says so (`unreachable_from_sandbox`): reviewers start their own
instance from the same entrypoint and judge the preview entries against it (post-mortem 2026-10-10, issues 7, 13).

`FACTORY_PREVIEW=managed` (an onboarded app): nothing of the target repository executes on the orchestrator host. Each
pushed head is verified in a fresh Anthropic-hosted sandbox built from the app's approved test recipe
(`hosted_verifier.HostedVerifier`): the recipe's own lifecycle, the test_plan commands, pytest collection and the
"preview" e2e entries with their browser evidence, all bound to the exact head and recipe. The host only reads git.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .. import visual
from .. import checks, security, size, steps
from ..hosted_verifier import ENVIRONMENT_CHECK, HostedVerifier, journey_name, preview_record
from ..budget import reserve_budget
from ..checks import CheckResult
from ..config import agent_model
# One definition each, shared with the hosted harness that runs the same collection and reads the same JUnit reports.
from ..sandbox_tools.environment_runner import NODE_ID_PLUGIN, collected_node_ids, parse_junit, write_node_id_plugin
from ..state import WorkItemStage
from . import RunContext
from .common import enter_item_stage, persist_outputs, platform_skill, public, reject_output, require_json, run_session, stamp_provenance, artifact_json_or_none, would_reuse
from .common import git as _git                # one git helper for every stage

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


NO_FORGE_GIT_SETUP = """Git access: no forge is configured for this run. The repository at {repo_url} is not reachable
from the sandbox; work on branch {branch} from {base_branch} only if it was mounted for you, otherwise record the
blocker in blocked.md."""

NO_FORGE_GIT_READ_ONLY = """Git access: no forge is configured for this run. If the repository at {repo_url} was mounted for
you, read branch {branch} there; this session is read-only on it: never commit, never push, and write everything you
produce under /mnt/session/outputs/."""

IMPLEMENT_DESCRIPTION = """Implement specification {spec_id} from /mnt/session/uploads/spec.json{spec_md_clause}.
{git}
{skill_clause}
{review_clause}
Write implementation_report.json{response_clause} to /mnt/session/outputs/ and validate it with the validate.py tool
(schema implementation_report.schema.json). The report must map every acceptance criterion to the tests that prove it, as
`criteria: [{{"ac_id": "AC-1", "tests": ["tests/test_x.py::test_name", ...]}}, ...]` with pytest node ids exactly as
`pytest --collect-only -q` prints them, or for Bun tests `path/to/x.test.ts::outer describe > inner describe > test name`
(the file relative to the repository root, describe blocks outermost first, the test name as Bun reports it); the
orchestrator runs the test plan itself, derives the passing set from its own run's test reports, and fails any automated
criterion whose tests it did not collect or did not see pass. Run every command in the spec's test_plan before finishing
and record the results. For a criterion proved by an e2e entry itself, use `e2e:<zero-based index>` in tests (for example
`e2e:0` for test_plan.e2e[0]). That entry must declare the criterion in covers_ac and pass in the controller's run,
including its exact browser evidence when visual; a baseline or an agent's claim never counts. {preview_clause}{guidance_clause}"""

GUIDANCE_CLAUSE = """
The owner approved this spec at Gate 2 with notes for you: /mnt/session/uploads/gate2_guidance.md. Follow them where they fit
inside the approved spec. They never change its acceptance criteria, allowed paths or test plan; where a note conflicts
with the spec, follow the spec and record the conflict in implementation_report.json notes."""

REVIEW_DESCRIPTION = """Review the change for specification {spec_id}. The diff is at /mnt/session/uploads/diff.patch
(base {base_sha}, head {head_sha}); the repository is cloned at /workspace/repo on branch {branch}.
{git}
{skill_clause}
This is review round {round}.{second_round}
{preview_clause}
Write findings.json to /mnt/session/outputs/ and validate it. You never edit source files."""

DELTA_REVIEW_CLAUSE = """ This is a follow-up round. First judge every finding in /mnt/session/uploads/previous_findings.json
against its refutation condition using review_response.json, and record each as upheld or withdrawn under its unchanged id.
Then review /mnt/session/uploads/delta.patch: the commits since the head the previous round reviewed ({previous_head}..{head_sha}).
diff.patch is the whole change, for context only: raise a new finding only on lines the delta changes or on behaviour the delta
breaks. Run the test plan once at this head; re-run a previous reproduction only to judge its own finding."""

NO_NEW_COMMITS_CLAUSE = """ This is a follow-up round and the implementer pushed no new commits: every previous finding was
answered in review_response.json without a code change. Judge each finding in /mnt/session/uploads/previous_findings.json against
its refutation condition and record it upheld or withdrawn under its unchanged id; do not review the unchanged diff again."""

PREVIEW_CLAUSE = """The platform has deployed this branch to a preview environment; its URL and pipeline are in
/mnt/session/uploads/preview.json. Run every test_plan.e2e entry with target "preview" against that URL (export
BASE_URL from preview.json) and record each command's exit code and output. The orchestrator re-runs them itself."""

LOCAL_PREVIEW_CLAUSE = """There is no preview deployment for this branch and none is reachable from this sandbox:
/mnt/session/uploads/preview.json says status "unreachable_from_sandbox" with a null url. The orchestrator runs every
test_plan.e2e entry itself, on its own host, against an instance it starts from the service manifest's runtime.entrypoint
(the manifest's runtime.port replaced by a free port, also exported as PORT) in a per-run virtual environment built from the
repository's pyproject (or the manifest's runtime.setup command); that run is the one that counts. Stand in for it the
same way: start the service from that entrypoint on a free port yourself, export BASE_URL, run every "preview" entry and
every "local" entry against it, and record each command, exit code and output tail. The service must start with the
manifest command and answer the manifest's health path, with nothing installed beyond the repository's declared
dependencies."""

LOCAL_PREVIEW_REVIEW_CLAUSE = LOCAL_PREVIEW_CLAUSE + """ The instance you start from the manifest entrypoint is the preview
stand-in for your spec_conformance table: record each run in checks_performed with target "local-manifest" and mark every
user_visible criterion met or not_met on that evidence. not_assessed is only for a criterion you could not exercise because
the entrypoint failed to start (which is itself a blocking finding), never because the orchestrator's instance was out of
reach."""

NO_PREVIEW_CLAUSE = """No preview environment exists for this branch yet (see "status" in /mnt/session/uploads/preview.json).
Push your branch early; the platform's pipeline deploys it. Run the test_plan.e2e entries with target "local" now and
record the "preview" ones as not run. The orchestrator runs the preview entries against the deployed branch once the
pipeline for your pushed head finishes; any later round of yours receives preview.json with the URL."""

SECURITY_REVIEW_DESCRIPTION = """Security review of the change for specification {spec_id}. The diff is at /mnt/session/uploads/diff.patch
(base {base_sha}, head {head_sha}); the repository is cloned at /workspace/repo on branch {branch}.
{git}
{skill_clause}
This is review round {round}. Two general adversarial reviewers run beside you on the same diff; you cover what they
do not: trust boundaries, attacker-controlled input, authorisation, secrets, dependencies, and the attack paths the
diff opens. The orchestrator's deterministic pre-checks are at /mnt/session/uploads/security_prechecks.json (a secret
scan over the added lines and, when the host has the tools, a dependency audit and a SAST run): inputs to judge, not
verdicts.{second_round}
{preview_clause}
Write findings.json to /mnt/session/outputs/ (category security, or concurrency / data_integrity when that is what the
defect is; one cwe per finding) and validate it. You never edit source files."""

MERGE_JUDGE_DESCRIPTION = """Decide merge readiness for specification {spec_id}, round {round}.
Inputs are under /mnt/session/uploads/: spec.json, diff.patch, findings.json, review_response.json,
test_results.json (authoritative; run by the orchestrator), deterministic_checks.json (you may not override these).
{git}
{skill_clause}
{criteria_clause}
Write merge_readiness.json to /mnt/session/outputs/ and validate it."""

CRITERIA_CLAUSE = """Fill `criteria` with your own status per acceptance criterion from evidence you located: the orchestrator's
e2e rows in test_results.json (each names the criteria it covers in covers_ac, and its target and exit code) are the
authoritative preview evidence; the reviewer's spec_conformance is an input, not the answer."""

MANAGED_CRITERIA_CLAUSE = CRITERIA_CLAUSE + """ This run used preview mode "managed": test_results.json `environment` names the
hosted sandbox session, the exact head and the approved recipe fingerprint every row is bound to. A row with exit_code null
was not run and proves nothing. A criterion is met only through rows that name it in covers_ac (UI criteria also need that
row's passing visual evidence); the recipe's baseline check (`hosted_environment`) shows the app runs, never that a criterion
is met. Inspect the uploaded visual_evidence.json and screenshots; if you cannot view an image, say so and do not claim visual
review."""

LOCAL_CRITERIA_CLAUSE = CRITERIA_CLAUSE + """ This run used preview mode "local": the orchestrator started the service from the
manifest entrypoint on its own host and ran the "preview" entries there (test_results.json `preview.orchestrator_instance`
and the e2e rows); reviewers could not reach that instance and ran the entries against one they started themselves. A
service criterion whose e2e rows exit 0 is met on that evidence. UI criteria additionally require passing visual evidence. Inspect the uploaded visual_evidence.json and screenshots; if you cannot view an image, explicitly report that limitation and do not claim visual review."""


MANAGED_PREVIEW_CLAUSE = """There is no preview URL for this branch and none is reachable from this sandbox:
/mnt/session/uploads/preview.json says status "hosted_sandbox" with a null url. After you push, the orchestrator verifies
your pushed head in a separate Anthropic-hosted test sandbox built from the app's approved test recipe (the service
manifest's test_environment.profile, default test_environment.json): the recipe's prepare/start/seed and baseline check,
every test_plan command, pytest collection, and every test_plan.e2e entry with target "preview" against the app inside that
sandbox ($BASE_URL). Only that run counts. Do not change the recipe: its approved fingerprint is pinned and a changed recipe
blocks verification. An e2e entry `factory:journey <name>` runs the factory's own browser runner on the journey you write
to /mnt/session/outputs/journeys/<name>.json (the format is in the platform-contract skill): one step per step of the
entry's visual contract, acting by role, label or text, with the expectations that prove the criterion. The journey is a
run artifact the orchestrator uploads and binds by hash; never commit browser-test code or anything that reads
FACTORY_VISUAL_REQUEST or FACTORY_EVIDENCE_DIR (the factory_proof_in_diff check refuses it). Bun tests count only from a direct `bun test ...` test_plan
command (no package script, pipeline, environment prefix or reporter of its own): the sandbox adds its own JUnit reporter to
it. Run what you can in this sandbox and record the results."""

JOURNEY_REVIEW_CLAUSE = """
The change's journeys are at /mnt/session/uploads/journeys/: run artifacts the factory's browser runner executed in the hosted
sandbox, not part of the diff. Review them as the evidence for their criteria: a journey that would still pass with the
behaviour broken leaves its criterion unproven, which is a finding against the change. A finding about a journey's own
plumbing only (how it reports a failure, its wording, a tighter check that proves nothing more) carries
"target": "verification"; it is recorded for the factory and never blocks the change or starts another round."""

MANAGED_PREVIEW_REVIEW_CLAUSE = """No preview URL is reachable from this sandbox (/mnt/session/uploads/preview.json: status
"hosted_sandbox", url null). The orchestrator verified this head in a separate Anthropic-hosted sandbox built from the app's
approved test recipe; deterministic_checks.json carries those results, and visual_evidence.json with the screenshots carries
the browser evidence. Never claim to have exercised a deployed preview. To probe behaviour, start the app in your own sandbox
from the recipe's commands; your sandbox may lack the recipe's network allowlist and test credentials, so mark what you could
not exercise not_assessed with that reason, never met."""


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
ENVIRONMENTAL_MARKERS = ("address already in use", "eaddrinuse")


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

        Every pytest invocation inside a command also writes a junit report (appended through PYTEST_ADDOPTS, so
        `pytest`, `python -m pytest` and compound shell commands all get it); a command that produced one carries
        `tests: {framework: "pytest", report: "junit", status: "reported", passed, failed, skipped}` with the node ids, which is where the orchestrator's passing-test set
        comes from (post-mortem issue 5: the set used to be copied from the implementer's own report, so any
        mapping passed, fabricated ids included)."""
        results = []
        if self.setup is not None and not self.setup.passed:
            return [{"command": cmd, "exit_code": None, "stdout_tail": "",
                     "stderr_tail": f"not run: {SETUP_CHECK} failed ({self.setup.detail[:300]})"} for cmd in commands]
        env = verification_env(self.env_bin)
        junit_dir = self.workdir / "junit"
        junit_dir.mkdir(parents=True, exist_ok=True)
        for i, cmd in enumerate(commands):
            junit = junit_dir / f"cmd-{i}.xml"
            junit.unlink(missing_ok=True)
            addopts = f"{env.get('PYTEST_ADDOPTS', '')} --junitxml={shlex.quote(str(junit))} -o junit_family=xunit1".strip()
            row: dict[str, Any] = {"command": cmd, **self._run_in_group(cmd, repo, {**env, "PYTEST_ADDOPTS": addopts}, timeout)}
            if junit.is_file():
                row["tests"] = {"framework": "pytest", "report": "junit", "status": "reported", **parse_junit(junit.read_bytes())}
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
        """Run the spec's `test_plan.e2e` entries for `target` against a running deployment.

        `$BASE_URL` (and the env var BASE_URL) is the environment URL the orchestrator read from the forge's
        deployment record (or the instance it started itself in local mode), never a URL an agent reported.
        Entries are run from the checkout, in the per-run environment and their own process group, so fixtures
        and e2e specs under the repo are available. If the URL is unknown (no preview deploy yet), every entry
        is recorded with exit_code None, which `checks.e2e_passed` treats as a failure: not run is not passed
        (Anthropic, "Effective harnesses": agents declare victory without end-to-end verification).
        """
        results = []
        for e in e2e_entries:
            if e.get("target", "preview") != target:
                continue
            row = {"command": e["command"], "target": target, "covers_ac": list(e.get("covers_ac", [])),
                   "base_url": preview_url}
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


def _reported(row: dict[str, Any]) -> dict[str, Any] | None:
    """A command row's test outcomes: its framework-neutral `tests` evidence when it is a report (pytest or Bun, local or
    hosted)."""
    tests = row.get("tests")
    return tests if isinstance(tests, dict) and tests.get("status") == "reported" else None


def passed_test_ids(command_results: list[dict[str, Any]]) -> tuple[list[str], list[str]] | None:
    """(passed, failed) test ids across every command that produced a test report, or None when none did."""
    runs = [t for t in map(_reported, command_results) if t]
    if not runs:
        return None
    passed = sorted({t for r in runs for t in r.get("passed", [])})
    failed = sorted({t for r in runs for t in r.get("failed", [])})
    return [t for t in passed if t not in set(failed)], failed


def managed_test_evidence(command_results: list[dict[str, Any]], collected: list[str] | None) -> tuple[list[str] | None, list[str] | None]:
    """(passing ids, known ids) for a managed run. Known is pytest's collection plus every id any report listed (Bun has
    no verified collect-only mode, so its inventory is the report of the run itself, skipped and filtered tests
    included). (None, None) when the run produced neither, so the mapping check fails closed."""
    runs = passed_test_ids(command_results)
    if runs is None and collected is None:
        return None, None
    listed = {t for r in map(_reported, command_results) if r for k in ("passed", "failed", "skipped") for t in r.get(k, [])}
    return (runs[0] if runs else []), sorted(set(collected or []) | listed)


def _branch_for(run_id: str, spec_id: str) -> str:
    return f"factory/{run_id}/{spec_id}"


def git_clauses(ctx: RunContext, repo_url: str, base_branch: str, branch: str) -> tuple[str, str]:
    """(write clause for the implementer, read-only clause for reviewers, the merge judge and the integrator).

    One clause used to serve every role, so reviewers were told to "commit as you go and push" right before "you
    never edit source files"; a less careful pass would have pushed its probes and tripped diff_scope on the next
    verification (post-mortem issue 18)."""
    if ctx.forge is None:
        return (NO_FORGE_GIT_SETUP.format(repo_url=repo_url, base_branch=base_branch, branch=branch),
                NO_FORGE_GIT_READ_ONLY.format(repo_url=repo_url, branch=branch))
    return (ctx.forge.sandbox_git_instructions(repo_url, base_branch, branch),
            ctx.forge.sandbox_git_instructions(repo_url, base_branch, branch, read_only=True))


def wait_for_preview(ctx: RunContext, branch: str, head_sha: str, *, poll_seconds: float = 20.0,
                     sleep=None, clock=None) -> dict[str, Any]:
    """Give the platform's pipeline time to deploy `head_sha` before running e2e against the preview.

    Bounded by `policy.preview_wait_seconds`; a preview still `pending` or `unavailable` at the deadline
    stays that way and `e2e_preview` fails closed (not run is not passed). The forge is polled; nothing
    pushes deployment state to the controller.
    """
    import time as _t
    sleep = sleep or _t.sleep
    clock = clock or _t.monotonic
    deadline = clock() + ctx.policy.preview_wait_seconds
    info = preview_info(ctx, branch, head_sha)
    while info["status"] in ("pending", "unavailable") and ctx.forge is not None and clock() < deadline:
        sleep(poll_seconds)
        info = preview_info(ctx, branch, head_sha)
    return info


def preview_info(ctx: RunContext, branch: str, head_sha: str | None = None) -> dict[str, Any]:
    """What the platform knows about this branch's preview deploy, read from the forge, never from an agent.

    Shape handed to agents as `preview.json`: {branch, status, url, pipeline}. `status` is one of
    `ready` (pipeline succeeded and an environment URL exists), `pending` (pipeline not finished),
    `failed`, `unavailable` (no forge, or no pipeline yet), or `unreachable_from_sandbox` (local mode: the
    orchestrator starts the service on its own host, which no sandbox can reach, so `url` is null and the
    agent's own manifest-entrypoint instance is the stand-in). UNVERIFIED against a live forge.
    """
    info: dict[str, Any] = {"branch": branch, "status": "unavailable", "url": None, "pipeline": None,
                            "head_sha": head_sha}
    if ctx.cfg.preview_mode == "managed":
        return preview_record(branch, head_sha)
    if ctx.cfg.preview_mode == "local":
        info.update(status="unreachable_from_sandbox", mode="local",
                    note=("no platform preview exists in local mode: the orchestrator starts the service from the manifest "
                          "entrypoint on its own host and runs the preview entries there; that instance cannot be reached from "
                          "a sandbox. Start the service from runtime.entrypoint yourself and run the entries against it."))
        return info
    if ctx.forge is None:
        return info
    try:
        pipe = ctx.forge.latest_pipeline(branch, head_sha)
        info["pipeline"] = pipe
        url = ctx.forge.environment_url_for_branch(branch, ctx.cfg.preview_environment_prefix)
        info["url"] = url
        if pipe is None:
            info["status"] = "unavailable"
        elif pipe["status"] == "success" and url:
            info["status"] = "ready"
        elif pipe["status"] in ("failed", "canceled"):
            info["status"] = "failed"
        else:
            info["status"] = "pending"
    except Exception as e:  # network or shape surprises must not take the stage down; the check still fails closed
        info["error"] = str(e)
    return info


def review_evidence_prefix(ctx: RunContext, sid: str, round_no: int, pass_id: str,
                           base_sha: str, head_sha: str) -> tuple[str, dict[str, Any] | None]:
    """The artifact prefix of one review pass, named for the exact diff it read (base and head), and its findings when
    that pass already persisted them and they name the same diff: only a review of the same diff is reused."""
    prefix = f"{sid}-r{round_no}-base{base_sha}-head{head_sha}-{pass_id}-"
    found = artifact_json_or_none(ctx, "review", prefix + "findings.json")
    ref = (found or {}).get("diff_ref") or {}
    return prefix, found if ref.get("base_sha") == base_sha and ref.get("head_sha") == head_sha else None


class BuildBlocked(RuntimeError):
    """The implementer wrote blocked.md: it cannot go on without a change of scope or a human. Its round is not
    recorded as finished, so the build-recovery decision that follows runs that implementer again."""


def run_implement_one(ctx: RunContext, spec: dict[str, Any], repo_url: str, base_branch: str,
                      verifier: LocalVerifier, dependency_specs: dict[str, dict], *, spec_md: str = "",
                      guidance: str = "", recovery: dict[str, Any] | None = None,
                      corrective: dict[str, Any] | None = None) -> dict[str, Any]:
    """Drive one spec through implement/review/judge. Returns the final merge_readiness verdict. `guidance` is the owner's
    bounded Gate 2 approval notes for this spec (`runner.record_approval`), uploaded to every implementer round.

    `spec_md` is the writer's spec.md for this spec (the runner carries it from Gate 2); the implementer, every
    reviewer pass and the merge judge receive it beside spec.json.

    Each round is a durable step (`implement/<spec>/r<n>`, ADR-0013) with its implementer session as a step inside it
    (`.../author`), and the merge judgement is one too (`implement/<spec>/judge#<k>`, `k` counting the owner's recovery
    decisions on the spec). A replay gets the rounds that finished back from their steps and continues at the first that
    did not; an older controller's rounds are adopted from their artefacts first (`adopt_rounds`). `recovery` is the
    owner's latest build-recovery decision on the spec: a recheck (approve) verifies the final head again and asks a new
    merge judge, and a blocked round's implementer runs again. `corrective` is their latest request for changes, which
    stays authorized through later rechecks: the round it names goes back to the implementer with their instructions
    and one more review is allowed."""
    _require_recorded_preview_mode(ctx)
    build = _Build(ctx, spec, repo_url, base_branch, verifier, dependency_specs, spec_md)
    sid, wid = build.sid, build.wid
    adopt_rounds(ctx, spec, corrective)
    current = ctx.store.state(ctx.run_id).work_items.get(wid)
    if current is not None and (current.stage in (WorkItemStage.SPEC_APPROVED, WorkItemStage.BLOCKED, WorkItemStage.ESCALATED)
                                or (current.stage == WorkItemStage.MERGE_JUDGING and corrective is not None)):
        # An item that has not started (or that the owner sent back) moves to implementing; one already in a round
        # stays where its last recorded event put it.
        ctx.event("work_item.stage", {"stage": WorkItemStage.IMPLEMENTING.value, "branch": build.branch}, work_item_id=wid, spec_id=sid)
    epoch = sum(1 for e in ctx.store.events(ctx.run_id) if e.kind == "build.recovery" and e.spec_id == sid)
    corrective_round = int(corrective["round"]) if corrective is not None else None
    review_through = max(ctx.policy.review_max_rounds, (corrective_round or 0) + 1)
    carried: dict[str, Any] = {"findings": None, "response": None, "det_only": False,
                               "journeys": _blobs(ctx, latest_journeys(ctx, sid, spec))}
    round_no = 0
    while True:
        round_no += 1
        notes = guidance
        if corrective is not None and round_no > corrective_round:
            notes = guidance + "\n# Build recovery instructions from the owner\n" + corrective.get("comments", "")
        inputs = {"findings": _sha_of(carried["findings"]), "response": _sha_of(carried["response"]),
                  "det_only": carried["det_only"], "journeys": carried["journeys"], "guidance": notes,
                  "review_through": round_no > review_through}
        try:
            result = steps.run_step(ctx, f"implement/{sid}/r{round_no}", inputs,
                                    lambda n=round_no, c=carried, g=notes: build.round(n, c, g, review_through))
        except BuildBlocked as blocked:
            return {"result": "escalate", "reason": str(blocked)}
        if corrective is not None and round_no == corrective_round and result["next"] == "judge":
            # The owner sent this round back: their findings (or the round's own) go to one more implementer round.
            findings = corrective.get("review_findings") or (_json_of(ctx, result["findings"]) if result["findings"] else None)
            carried = {"findings": findings or {"findings": []}, "response": _json_of(ctx, result["response"]),
                       "det_only": False, "journeys": result["journeys"]}
            continue
        if result["next"] == "judge":
            break
        carried = {"findings": _json_of(ctx, result["findings"]), "response": _json_of(ctx, result["response"]),
                   "det_only": result["det_only"], "journeys": result["journeys"]}
    recheck = recovery is not None and recovery.get("decision") == "approve"
    return steps.run_step(ctx, f"implement/{sid}/judge#{epoch}", {"round": steps.inputs_sha(result), "recheck": recheck},
                          lambda: build.judge(round_no, result, recheck))


def _blobs(ctx: RunContext, files: dict[str, bytes]) -> dict[str, str]:
    return {name: ctx.store.put_blob(ctx.run_id, content) for name, content in sorted(files.items())}


def _files(ctx: RunContext, blobs: dict[str, str]) -> dict[str, bytes]:
    return {name: ctx.store.get_blob(ctx.run_id, sha) for name, sha in sorted(blobs.items())}


def _sha_of(value: Any) -> str | None:
    return None if value is None else steps.inputs_sha(value)


def _json_of(ctx: RunContext, blob: str | None) -> Any:
    return None if blob is None else steps.get_json(ctx, blob)


class _Build:
    """What every round of one spec's build shares: the branch, the base it is cut from, the git clauses and uploads."""

    def __init__(self, ctx: RunContext, spec: dict[str, Any], repo_url: str, base_branch: str, verifier: LocalVerifier,
                 dependency_specs: dict[str, dict], spec_md: str):
        self.ctx, self.spec, self.repo_url, self.base_branch, self.verifier = ctx, spec, repo_url, base_branch, verifier
        self.dependency_specs = dependency_specs
        self.sid, self.wid = spec["spec_id"], spec["work_item_id"]
        self.spec_md = (spec_md or spec.get("_markdown") or "").strip()
        self.branch = _branch_for(ctx.run_id, self.sid)
        self.git, self.git_ro = git_clauses(ctx, repo_url, base_branch, self.branch)   # the implementer pushes; everyone else reads
        self.base_sha = _git(verifier.checkout(base_branch), "rev-parse", "HEAD").strip()
        self.skill_uploads, self.skill_clause = platform_skill(ctx)
        self.managed = ctx.cfg.preview_mode == "managed"

    def mount(self, checkout: str) -> dict[str, Any] | None:
        ctx = self.ctx
        return ctx.forge.session_repository_resource(self.repo_url, checkout) if ctx.forge is not None else None

    def spec_uploads(self) -> dict[str, bytes]:
        out = {"spec.json": json.dumps(public(self.spec), indent=2).encode(), **self.skill_uploads}
        if self.spec_md:
            out["spec.md"] = self.spec_md.encode()
        return out

    # ---------------------------------------------------------------- one round

    def round(self, round_no: int, carried: dict[str, Any], guidance: str, review_through: int) -> dict[str, Any]:
        """Implementer, the orchestrator's own verification, and (unless the round goes straight to the judge) the review.
        Its result is what the next round and the judge need, the larger parts kept as blobs."""
        ctx, sid, wid = self.ctx, self.sid, self.wid
        findings, det_only = carried["findings"], carried["det_only"]
        author_inputs = {"findings": _sha_of(findings), "det_only": det_only, "journeys": carried["journeys"], "guidance": guidance}
        reused_author = steps.recorded(ctx, f"implement/{sid}/r{round_no}/author", author_inputs)
        author = steps.run_step(ctx, f"implement/{sid}/r{round_no}/author", author_inputs,
                                lambda: self.author(round_no, findings, det_only, _files(ctx, carried["journeys"]), guidance))
        report = _json_of(ctx, author["report"])
        response = _json_of(ctx, author["response"])
        journeys = _files(ctx, author["journeys"])
        if findings and response is not None:
            response["round"] = round_no - 1
            ctx.event("review.response", {"round": round_no - 1, "dispositions": [r["disposition"] for r in response["responses"]]},
                      work_item_id=wid, spec_id=sid, actor="agent")
        v = self.verify(round_no, report, journeys, check_reported_head=reused_author)
        det = v["det"]
        result = {"report": author["report"], "response": steps.put_json(ctx, response) if response is not None else None,
                  "journeys": author["journeys"], "head_sha": v["head_sha"], "base_sha": self.base_sha,
                  "det": [d.to_dict() for d in det], "test_results": steps.put_json(ctx, v["test_results"]),
                  "patch": ctx.store.put_blob(ctx.run_id, v["diff"]["patch"].encode()), "visual": _blobs(ctx, v["visual_uploads"]),
                  "preview": v["preview"], "findings": steps.put_json(ctx, findings) if findings is not None else None,
                  "next": "judge", "det_only": False}
        if round_no > review_through:
            return result            # last implementer response done; go straight to the merge judge
        if det_only and all(d.passed for d in det):
            # The previous round's only standing findings were the orchestrator's own checks; they pass on the
            # pushed head, so a fresh adversarial read would re-prove what round one already proved. Straight to
            # the judge, which still sees findings.json and the implementer's answers (post-mortem issue 6).
            ctx.event("review.findings", {"round": round_no, "count": 0, "blocking": 0, "deterministic_only": True,
                                          "skipped": "deterministic checks pass on the response; no review round"},
                      work_item_id=wid, spec_id=sid)
            return result
        merged = self.review(round_no, report, response, findings, v, journeys)
        result["findings"] = steps.put_json(ctx, merged)
        standing = standing_findings(merged["findings"])
        if not standing and all(d.passed for d in det):
            return result            # nothing standing; skip the implementer round and go to the judge
        # Only the orchestrator's own checks stand (no agent finding is blocking or major): the next round is an
        # implementer response re-verified by the checks, not a fresh review of the whole diff.
        result["det_only"] = _deterministic_only(standing)
        enter_item_stage(ctx, wid, sid, WorkItemStage.RESPONDING, deterministic_only=result["det_only"])
        return {**result, "next": "respond"}

    def author(self, round_no: int, findings: dict[str, Any] | None, det_only: bool, journeys: dict[str, bytes],
               guidance: str) -> dict[str, Any]:
        """The implementer's session for this round (round 1 implements; later rounds respond to findings)."""
        ctx, sid, wid = self.ctx, self.sid, self.wid
        uploads = {**self.spec_uploads(), **{f"journeys/{name}.json": content for name, content in sorted(journeys.items())}}
        if guidance:
            uploads["gate2_guidance.md"] = guidance.encode()          # the owner's bounded approval notes; never a spec change
        for dsid, ds in self.dependency_specs.items():
            uploads[f"dependency_specs/{dsid}.json"] = json.dumps(public(ds), indent=2).encode()
        review_clause, response_clause = "", ""
        preview = preview_info(ctx, self.branch)
        uploads["preview.json"] = json.dumps(preview, indent=2).encode()
        preview_clause = (MANAGED_PREVIEW_CLAUSE if preview.get("mode") == "managed" else PREVIEW_CLAUSE if preview["status"] == "ready"
                          else LOCAL_PREVIEW_CLAUSE if preview.get("mode") == "local" else NO_PREVIEW_CLAUSE)
        if journeys:
            preview_clause += (" Previously saved browser journeys are at /mnt/session/uploads/journeys/. "
                               "Reuse these definitions; do not reconstruct them from the spec prose. Preserve existing assertions "
                               "unless a review finding requires a targeted correction. The controller carries unchanged journeys forward.")
        if findings:
            uploads["findings.json"] = json.dumps(findings, indent=2).encode()
            review_clause = ("Reviewer findings are at /mnt/session/uploads/findings.json. Respond to every finding "
                             "(fixed / refuted with evidence / accepted_risk) and push the updated branch.")
            if det_only:
                review_clause += (" Every standing finding is a DET-* entry: a deterministic check the orchestrator runs itself "
                                  "(its detail and reproduction say exactly what failed). Fix the cause and push; the orchestrator "
                                  "re-runs the checks on your pushed head and, if they pass, goes straight to the merge judge.")
            response_clause = " and review_response.json"
        session = dict(
            prefix=f"{sid}-r{round_no}-", spec_id=sid,
            agent_key="implementer", title=f"{ctx.run_id} implement {sid} r{round_no}", run_id=ctx.run_id,
            uploads=uploads, work_item_id=wid,
            description=IMPLEMENT_DESCRIPTION.format(spec_id=sid, git=self.git, review_clause=review_clause, skill_clause=self.skill_clause,
                                                     spec_md_clause=" (spec.md is the readable form)" if self.spec_md else "",
                                                     response_clause=response_clause, preview_clause=preview_clause,
                                                     guidance_clause=GUIDANCE_CLAUSE if guidance else ""),
            rubric_md=ctx.grader_rubric("implementation"), max_iterations=ctx.policy.outcome_max_iterations,
            budget_usd=ctx.policy.stage_budget_usd["implement"],
            repository=self.mount(self.base_branch if round_no == 1 else self.branch),
        )
        if not would_reuse(ctx, stage="implement", **session):
            reserve_budget(ctx, "implement")
        res = run_session(ctx, stage="implement", **session)
        if "blocked.md" in res.outputs:
            persist_outputs(ctx, "implement", res, prefix=f"{sid}-r{round_no}-", work_item_id=wid, spec_id=sid)
            ctx.event("work_item.stage", {"stage": WorkItemStage.BLOCKED.value}, work_item_id=wid, spec_id=sid)
            reason = res.outputs["blocked.md"].decode(errors="replace")
            raise BuildBlocked(str(reject_output(res, reason)))    # its session is not reused: a recovery asks again
        report = require_json(res, "implementation_report.json")
        stamp_provenance(report, ctx, res, "Implementer", agent_model("implementer"))
        res.outputs["implementation_report.json"] = json.dumps(report, indent=2).encode()
        response = None
        if findings:
            response = require_json(res, "review_response.json")
            stamp_provenance(response, ctx, res, "Implementer", agent_model("implementer"))
            res.outputs["review_response.json"] = json.dumps(response, indent=2).encode()
        persist_outputs(ctx, "implement", res, prefix=f"{sid}-r{round_no}-", work_item_id=wid, spec_id=sid)
        return {"report": steps.put_json(ctx, report), "response": steps.put_json(ctx, response) if response is not None else None,
                "journeys": _blobs(ctx, latest_journeys(ctx, sid, self.spec))}

    # ---------------------------------------------------------------- verification

    def verify(self, round_no: int, report: dict[str, Any], journeys: dict[str, bytes], *,
               check_reported_head: bool = False) -> dict[str, Any]:
        """The orchestrator's own verification of the branch head: the checks no agent can override."""
        ctx, sid, wid, spec, verifier = self.ctx, self.sid, self.wid, self.spec, self.verifier
        base_sha, branch, managed = self.base_sha, self.branch, self.managed
        repo = verifier.checkout(branch)
        head_sha = _git(repo, "rev-parse", "HEAD").strip()
        ctx.event("verification.started", {"head_sha": head_sha, "round": round_no}, spec_id=sid, work_item_id=wid)
        diff = verifier.analyse(repo, base_sha, head_sha)
        manifest_path = repo / ctx.cfg.service_manifest_path
        manifest = _manifest_or_none(manifest_path)
        extra_det: list[CheckResult] = []
        if managed:
            # Every execution happens in the hosted sandbox; this host only read git above. Missing, unbound or
            # unsupported evidence comes back as not-run rows, which the checks below fail.
            hosted = HostedVerifier(ctx, verifier.workdir).verify(repo, base_sha, head_sha, spec, branch=branch,
                                                                  round_no=round_no, work_item_id=wid, journeys=journeys)
            setup, cmd_results, e2e_results, preview, collected = hosted.setup, hosted.commands, hosted.e2e, hosted.preview, hosted.collected
            extra_det = [hosted.environment]
            environment = {**hosted.record, "detail": hosted.setup.detail}
        else:
            setup = verifier.prepare_environment(repo, manifest)       # the per-run environment, rebuilt from this head's pyproject
            cmd_results = verifier.run_commands(repo, spec["test_plan"]["commands"])
            # End-to-end against the preview the platform deployed for head_sha. The URL comes from the forge's
            # deployment record, not from the implementer's report; "not run" counts as failed.
            if ctx.cfg.preview_mode == "local":
                preview, e2e_results = _local_preview_e2e(ctx, repo, branch, head_sha, verifier, spec, manifest)
            else:
                preview = wait_for_preview(ctx, branch, head_sha)
                e2e_results = verifier.run_e2e(preview["url"] if preview["status"] == "ready" else None,
                                               spec["test_plan"].get("e2e", []), repo=repo)
            collected = None
            environment = {"mode": ctx.cfg.preview_mode, "host": "orchestrator" if ctx.cfg.preview_mode == "local" else "platform",
                           **setup.data, "ready": setup.passed, "detail": setup.detail}
        # The passing set is the orchestrator's own: node ids pytest reported passed in the test_plan run, checked against
        # what pytest collects in the checkout. A repository pytest cannot collect leaves the mapping unverified (said so
        # in the check's detail), never silently verified.
        visual_uploads = visual.persist(ctx, sid, round_no, head_sha, e2e_results)
        if managed:
            # Only reports the harness asked for count; an exit code alone, or an id only the implementer names, is no evidence.
            passing_tests, tests_known = managed_test_evidence(cmd_results, collected)
        else:
            collected = verifier.collect_test_ids(repo)
            pytest_runs = passed_test_ids(cmd_results)
            passing_tests = (pytest_runs[0] if pytest_runs else []) if collected is not None else None
            tests_known = collected
        measured = measure_size(repo, base_sha, head_sha, spec)
        det = [setup] + extra_det + checks.run_all_for_merge(
            spec=spec, changed_files=diff["changed"], deleted_files=diff["deleted"], existing_test_files=diff["existing_tests"],
            added_lines_by_file=diff["added_lines"], implementation_report=report, passing_tests=passing_tests,
            tests_that_exist=tests_known, command_results=cmd_results, manifest_path=manifest_path,
            schema_dir=ctx.cfg.schemas_dir, e2e_results=e2e_results, require_test_evidence=managed,
            size=measured, size_budget_lines=ctx.policy.size_budget_lines,
            # only exact test paths in the approved spec may be updated; every other existing test stays protected
            named_test_files=checks.named_test_paths(spec["allowed_paths"]), removed_lines_by_file=diff.get("removed_lines"),
        )
        test_results = {"commands": cmd_results, "e2e": e2e_results, "preview": preview, "environment": environment,
                        **({"collected_tests": collected} if managed else {})}
        if check_reported_head and report.get("head_sha") is not None:
            # The report came from an implementer session recorded before this verification (a replayed round, or an older
            # controller's): the branch may have moved since.
            reported_head = report.get("head_sha")
            det.append(CheckResult("implementation_head", reported_head == head_sha,
                                   f"implementation report names {reported_head}; verified candidate is {head_sha}. "
                                   "A changed candidate requires an updated implementation report and review response."))
        ctx.store.put_derived_artifact(ctx.run_id, "implement", f"{sid}-r{round_no}-deterministic_checks.json",
                                       json.dumps([d.to_dict() for d in det], indent=2).encode())
        ctx.store.put_derived_artifact(ctx.run_id, "implement", f"{sid}-r{round_no}-test_results.json",
                                       json.dumps(test_results, indent=2).encode())
        return {"repo": repo, "head_sha": head_sha, "diff": diff, "det": det, "test_results": test_results, "preview": preview,
                "visual_uploads": visual_uploads, "measured": measured, "cmd_results": cmd_results, "e2e_results": e2e_results}

    # ---------------------------------------------------------------- review

    def review(self, round_no: int, report: dict[str, Any], response: dict[str, Any] | None, findings: dict[str, Any] | None,
               v: dict[str, Any], journeys: dict[str, bytes]) -> dict[str, Any]:
        """Fresh-context adversarial review shaped by the change (`review_plan`): general passes plus a security pass,
        merged, with the orchestrator's failed checks as findings no reviewer can clear."""
        ctx, sid, wid, verifier = self.ctx, self.sid, self.wid, self.verifier
        base_sha, branch, managed, head_sha, repo, diff, det = self.base_sha, self.branch, self.managed, v["head_sha"], v["repo"], v["diff"], v["det"]
        preview, visual_uploads = v["preview"], v["visual_uploads"]
        security_available = "security_reviewer" in ctx.cfg.agents
        previous_head = ((findings or {}).get("diff_ref") or {}).get("head_sha") if findings and response else None
        delta = delta_since(verifier, repo, previous_head, head_sha) if previous_head else None
        plan = review_plan(ctx.policy, v["measured"], findings, response, (delta or {}).get("added_lines"), security_available)
        if plan["scope"] == "delta" and delta is None and previous_head != head_sha:
            # The head the previous round reviewed is not in this branch's history: read the whole change again.
            plan = {**plan, "scope": "full", "security": bool(ctx.policy.security_review and security_available),
                    "security_why": "the previous head is gone; full review"}
        pass_ids = list(range(1, plan["passes"] + 1))
        with_security = plan["security"]
        # A pass that already read this exact diff (named for its base and head) is reused, not paid for again: a round
        # that runs again after a crash keeps the reviews it had.
        review_bindings = {i: review_evidence_prefix(ctx, sid, round_no, f"p{i}", base_sha, head_sha) for i in pass_ids}
        security_prefix, saved_security = review_evidence_prefix(ctx, sid, round_no, "sec", base_sha, head_sha)
        reused = {i: binding[1] for i, binding in review_bindings.items()}
        reused_sec = saved_security if with_security else None
        to_run = [i for i in pass_ids if reused.get(i) is None]
        run_sec = with_security and reused_sec is None
        if to_run or run_sec:
            enter_item_stage(ctx, wid, sid, WorkItemStage.REVIEWING, review_round=round_no, review_plan=plan)
            # every session of the round, before the first one starts: the general passes and the security pass together
            if to_run:
                reserve_budget(ctx, "review", len(to_run), extra={"security_review": 1} if run_sec else None)
            else:
                reserve_budget(ctx, "security_review", 1)
        spec_uploads = self.spec_uploads()
        journey_uploads = {f"journeys/{name}.json": content for name, content in sorted(journeys.items())}
        rev_uploads = {**spec_uploads, "diff.patch": diff["patch"].encode(), **visual_uploads, **journey_uploads,
                       "implementation_report.json": json.dumps(report, indent=2).encode(),
                       "preview.json": json.dumps(preview, indent=2).encode(),
                       "deterministic_checks.json": json.dumps([d.to_dict() for d in det], indent=2).encode()}
        rev_preview_clause = (MANAGED_PREVIEW_REVIEW_CLAUSE if preview.get("mode") == "managed" else
                              PREVIEW_CLAUSE if preview["status"] == "ready" and preview.get("mode") != "local" else
                              LOCAL_PREVIEW_REVIEW_CLAUSE if preview.get("mode") == "local" else NO_PREVIEW_CLAUSE)
        if journeys:
            rev_preview_clause += JOURNEY_REVIEW_CLAUSE
        second = ""
        if findings and response:
            rev_uploads["previous_findings.json"] = json.dumps(findings, indent=2).encode()
            rev_uploads["review_response.json"] = json.dumps(response, indent=2).encode()
            if plan["scope"] == "delta" and delta is not None and delta["patch"].strip():
                rev_uploads["delta.patch"] = delta["patch"].encode()
                second = DELTA_REVIEW_CLAUSE.format(previous_head=previous_head, head_sha=head_sha)
            elif plan["scope"] == "delta":
                second = NO_NEW_COMMITS_CLAUSE
            else:
                second = (" Judge each previous finding against your refutation condition using review_response.json "
                          "(upheld/withdrawn), then review the new hunks.")

        def one_pass(i: int) -> dict[str, Any]:
            if reused.get(i) is not None:
                return tag_pass(reused[i], str(i))
            r = run_session(
                ctx, prefix=review_bindings[i][0], spec_id=sid,
                agent_key="reviewer", title=f"{ctx.run_id} review {sid} r{round_no} p{i}", run_id=ctx.run_id,
                stage="review", uploads=rev_uploads, work_item_id=wid,
                description=REVIEW_DESCRIPTION.format(spec_id=sid, base_sha=base_sha, head_sha=head_sha, branch=branch,
                                                      git=self.git_ro, round=round_no, second_round=second, skill_clause=self.skill_clause,
                                                      preview_clause=rev_preview_clause),
                rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["review"],
                metadata={"pass": str(i)}, repository=self.mount(branch),
            )
            f = require_json(r, "findings.json")
            stamp_provenance(f, ctx, r, "Adversarial reviewer", agent_model("reviewer"))
            r.outputs["findings.json"] = json.dumps(f, indent=2).encode()
            persist_outputs(ctx, "review", r, prefix=review_bindings[i][0], work_item_id=wid, spec_id=sid)
            return tag_pass(f, str(i))

        def security_pass() -> dict[str, Any] | None:
            """One security reviewer beside the general passes; skipped when the policy or the deployment lacks it."""
            if not with_security:
                return None
            if reused_sec is not None:
                return tag_pass(reused_sec, "security")
            pre = managed_prechecks(diff["added_lines"]) if managed else security.prechecks(repo, diff["changed"], diff["added_lines"])
            ctx.store.put_artifact(ctx.run_id, "review", security_prefix + "security_prechecks.json", json.dumps(pre, indent=2).encode())
            security_skill = ctx.cfg.skills_dir / "security-review" / "SKILL.md"
            if not security_skill.is_file():
                raise RuntimeError("security reviewer requires skills/security-review/SKILL.md")
            r = run_session(
                ctx, prefix=security_prefix, spec_id=sid,
                agent_key="security_reviewer", title=f"{ctx.run_id} security-review {sid} r{round_no}", run_id=ctx.run_id,
                stage="review", uploads={**rev_uploads, "security_prechecks.json": json.dumps(pre, indent=2).encode(),
                         "skills/security-review/SKILL.md": security_skill.read_bytes()}, work_item_id=wid,
                description=SECURITY_REVIEW_DESCRIPTION.format(spec_id=sid, base_sha=base_sha, head_sha=head_sha, branch=branch,
                                                               git=self.git_ro, round=round_no, second_round=second, skill_clause=self.skill_clause,
                                                               preview_clause=rev_preview_clause),
                rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["security_review"],
                metadata={"pass": "security"}, repository=self.mount(branch),
            )
            f = require_json(r, "findings.json")
            stamp_provenance(f, ctx, r, "Security reviewer", agent_model("security_reviewer"))
            r.outputs["findings.json"] = json.dumps(f, indent=2).encode()
            persist_outputs(ctx, "review", r, prefix=security_prefix, work_item_id=wid, spec_id=sid)
            return tag_pass(f, "security")

        with ThreadPoolExecutor(max_workers=ctx.policy.reviewer_passes + 1) as pool:
            sec_future = pool.submit(steps.carry(security_pass))
            passes = list(pool.map(steps.carry(one_pass), pass_ids))
            sec = sec_future.result()
        merged = merge_findings(passes + ([sec] if sec else []), sid, round_no, base_sha, head_sha, branch,
                                previous=findings, response=response)
        # A failed deterministic check is a finding the implementer must answer; nobody else can clear it.
        merged["findings"] = apply_deterministic(merged["findings"], deterministic_findings(det, v["cmd_results"], v["e2e_results"], managed=managed))
        ctx.store.put_derived_artifact(ctx.run_id, "review", f"{sid}-r{round_no}-findings.merged.json",
                                       json.dumps(merged, indent=2).encode())
        record_verification_findings(ctx, sid, round_no, merged["findings"])
        ctx.event("review.findings", {"round": round_no, "count": len(merged["findings"]),
                                      "blocking": sum(f["severity"] == "blocking" for f in merged["findings"]),
                                      "passes": len(pass_ids), "security_pass": with_security, "scope": plan["scope"]},
                  work_item_id=wid, spec_id=sid, actor="agent")
        return merged

    # ---------------------------------------------------------------- the merge judge

    def judge(self, round_no: int, final: dict[str, Any], recheck: bool) -> dict[str, Any]:
        """The independent merge judge on the final round. A recheck (the owner's build-recovery approve) verifies that
        head again first: the judge never sees evidence older than the decision to look again, and an earlier verdict
        never stands in for it."""
        ctx, sid, wid = self.ctx, self.sid, self.wid
        det = [CheckResult.from_dict(d) for d in final["det"]]
        test_results = _json_of(ctx, final["test_results"])
        patch = ctx.store.get_blob(ctx.run_id, final["patch"])
        visual_uploads = _files(ctx, final["visual"])
        head_sha = final["head_sha"]
        journeys = _files(ctx, final["journeys"])
        if recheck:
            v = self.verify(round_no, _json_of(ctx, final["report"]), journeys, check_reported_head=True)
            det, test_results, head_sha, visual_uploads = v["det"], v["test_results"], v["head_sha"], v["visual_uploads"]
            patch = v["diff"]["patch"].encode()
        findings, response = _json_of(ctx, final["findings"]), _json_of(ctx, final["response"])
        mj_uploads = {
            "spec.json": json.dumps(public(self.spec), indent=2).encode(), "diff.patch": patch, **visual_uploads,
            **{f"journeys/{name}.json": content for name, content in sorted(journeys.items())},
            **({"spec.md": self.spec_md.encode()} if self.spec_md else {}),
            "findings.json": json.dumps(findings or {"findings": []}, indent=2).encode(),
            "review_response.json": json.dumps(response or {"responses": []}, indent=2).encode(),
            "test_results.json": json.dumps(test_results, indent=2).encode(),
            "deterministic_checks.json": json.dumps([d.to_dict() for d in det], indent=2).encode(),
            **self.skill_uploads,
        }
        input_hashes = {name: hashlib.sha256(content).hexdigest() for name, content in sorted(mj_uploads.items())}

        def name_this_judgement() -> str:
            # A new judge output is immutable, even when it revisits the same implementation round: the next free name,
            # chosen once (a step of its own), so a judgement replayed after a crash keeps the name its verdict was saved
            # under instead of looking like an earlier attempt.
            folder = ctx.cfg.runs_dir / ctx.run_id / "artifacts" / "merge_judge"
            original_prefix = f"{sid}-r{round_no}-"
            attempts = sorted(folder.glob(original_prefix + "*merge_readiness.json"))
            return original_prefix if not attempts else original_prefix + f"recheck-{len(attempts):04d}-"
        judge_prefix = steps.run_step(ctx, f"{steps.scope()}/name", {"round": round_no}, name_this_judgement)
        enter_item_stage(ctx, wid, sid, WorkItemStage.MERGE_JUDGING)
        session = dict(
            prefix=judge_prefix, spec_id=sid,
            agent_key="merge_judge", title=f"{ctx.run_id} merge-judge {sid}", run_id=ctx.run_id,
            uploads=mj_uploads, work_item_id=wid,
            description=MERGE_JUDGE_DESCRIPTION.format(spec_id=sid, round=round_no, git=self.git_ro, skill_clause=self.skill_clause,
                                                       criteria_clause=MANAGED_CRITERIA_CLAUSE if ctx.cfg.preview_mode == "managed" else
                                                       LOCAL_CRITERIA_CLAUSE if ctx.cfg.preview_mode == "local" else CRITERIA_CLAUSE),
            rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["merge_judge"],
            repository=self.mount(self.branch),
        )
        if not would_reuse(ctx, stage="merge_judge", **session):
            reserve_budget(ctx, "merge_judge")
        res = run_session(ctx, stage="merge_judge", **session)
        verdict = require_json(res, "merge_readiness.json")
        verdict["round"] = round_no
        # Deterministic checks are not negotiable: a failing check forces another_round or escalate.
        if any(not d.passed for d in det) and verdict["result"] == "ready":
            verdict["result"] = "another_round"
            verdict["summary_for_human"] = "Overridden: deterministic checks failed. " + verdict["summary_for_human"]
        verdict["deterministic_checks"] = [d.to_dict() for d in det]
        stamp_provenance(verdict, ctx, res, "Merge judge", agent_model("merge_judge"))
        verdict["_input_hashes"] = input_hashes
        verdict["_head_sha"], verdict["_branch"], verdict["_base_sha"] = head_sha, self.branch, self.base_sha
        res.outputs["merge_readiness.json"] = json.dumps(verdict, indent=2).encode()
        persist_outputs(ctx, "merge_judge", res, prefix=judge_prefix, work_item_id=wid, spec_id=sid)
        ctx.event("merge.verdict", {"result": verdict["result"], "round": round_no}, work_item_id=wid, spec_id=sid, actor="agent")
        return verdict


def adopt_rounds(ctx: RunContext, spec: dict[str, Any], corrective: dict[str, Any] | None = None) -> None:
    """Record the rounds an older controller left as artefacts (no round steps in the run's database), so the loop
    continues where they stop instead of running the implementer again (ADR-0013). The latest round with an implementer
    report keeps that report (its implementer is not run again; its verification and review are); every earlier round
    with merged findings is recorded as answered. A latest round whose report lacks the response its findings required
    runs its implementer again. Nothing is recorded when the run has round steps, or no report."""
    sid = spec["spec_id"]
    if any(s.key.startswith(f"implement/{sid}/r") for s in ctx.store.steps(ctx.run_id)):
        return
    folder = ctx.cfg.runs_dir / ctx.run_id / "artifacts" / "implement"
    rounds = sorted(int(m.group(1)) for p in folder.glob(f"{sid}-r*-implementation_report.json")
                    if (m := re.fullmatch(re.escape(sid) + r"-r(\d+)-implementation_report\.json", p.name)))
    if not rounds:
        return
    n = rounds[-1]

    def findings_of(k: int) -> dict[str, Any] | None:
        found = artifact_json_or_none(ctx, "review", f"{sid}-r{k}-findings.merged.json")
        if found is None and corrective is not None and int(corrective.get("round", 0)) == k:
            found = corrective.get("review_findings") or {"findings": []}
        return found
    if n > 1 and findings_of(n - 1) is None:
        raise RuntimeError(f"cannot continue {sid}: round {n} has a report but round {n - 1} has no merged findings")
    journeys = _blobs(ctx, latest_journeys(ctx, sid, spec))
    carried: dict[str, Any] = {"findings": None, "response": None, "det_only": False, "journeys": journeys}
    for k in range(1, n + 1):
        report = artifact_json_or_none(ctx, "implement", f"{sid}-r{k}-implementation_report.json")
        response = artifact_json_or_none(ctx, "implement", f"{sid}-r{k}-review_response.json")
        last = k == n and not (corrective is not None and int(corrective.get("round", 0)) == n)
        if last:
            if report is not None and not (carried["findings"] and response is None):
                steps.record_step(ctx, f"implement/{sid}/r{k}/author",
                                  {"report": steps.put_json(ctx, report), "journeys": journeys,
                                   "response": steps.put_json(ctx, response) if response is not None else None})
            return
        findings = findings_of(k)
        det = artifact_json_or_none(ctx, "implement", f"{sid}-r{k}-deterministic_checks.json") or []
        test_results = artifact_json_or_none(ctx, "implement", f"{sid}-r{k}-test_results.json") or {}
        standing = standing_findings((findings or {}).get("findings", []))
        steps.record_step(ctx, f"implement/{sid}/r{k}", {
            "report": steps.put_json(ctx, report or {}), "response": steps.put_json(ctx, response) if response is not None else None,
            "journeys": journeys, "head_sha": ((findings or {}).get("diff_ref") or {}).get("head_sha"), "base_sha": None,
            "det": det, "test_results": steps.put_json(ctx, test_results), "patch": ctx.store.put_blob(ctx.run_id, b""),
            "visual": {}, "preview": test_results.get("preview") or {}, "findings": steps.put_json(ctx, findings or {"findings": []}),
            "next": "judge" if k == n else "respond", "det_only": _deterministic_only(standing)})
        carried = {"findings": findings or {"findings": []}, "response": response, "det_only": _deterministic_only(standing),
                   "journeys": journeys}


def latest_journeys(ctx: RunContext, sid: str, spec: dict[str, Any]) -> dict[str, bytes]:
    """Newest journey outputs named in the spec, including provider listings that flatten `journeys/<name>.json`
    to `<name>.json`. A later round may rewrite one journey and leave the others; unrelated reports never qualify."""
    root = ctx.cfg.runs_dir / ctx.run_id / "artifacts" / "implement"
    names = {name for entry in spec["test_plan"].get("e2e", []) if (name := journey_name(entry["command"]))}
    found: dict[str, tuple[int, bytes]] = {}
    for name in sorted(names):
        pattern = re.compile(re.escape(sid) + r"-r(\d+)-(?:journeys/)?" + re.escape(name) + r"\.json")
        paths = [*root.glob(f"{sid}-r*-{name}.json"), *root.glob(f"{sid}-r*-journeys/{name}.json")]
        matches = [(int(m.group(1)), path) for path in paths if (m := pattern.fullmatch(path.relative_to(root).as_posix()))]
        if not matches:
            continue
        latest = max(round_no for round_no, _ in matches)
        contents = {path.read_bytes() for round_no, path in matches if round_no == latest}
        if len(contents) != 1:
            raise ValueError(f"conflicting journey outputs for {name} in {sid} round {latest}")
        found[name] = (latest, contents.pop())
    return {name: content for name, (_, content) in sorted(found.items())}


def record_verification_findings(ctx: RunContext, sid: str, round_no: int, findings: list[dict[str, Any]]) -> None:
    """Findings about the journey's own plumbing (`target: verification`) never block the change; keep them in the run
    and append them to the factory's backlog (runs/_backlog/verification-findings.jsonl) for the runner's owners."""
    mine = [f for f in findings if f.get("target") == "verification" and f.get("status") in ("open", "upheld")]
    if not mine:
        return
    ctx.store.put_artifact(ctx.run_id, "review", f"{sid}-r{round_no}-verification_findings.json", json.dumps(mine, indent=2).encode(),
                           replace=True)
    backlog = ctx.cfg.runs_dir / "_backlog"
    backlog.mkdir(parents=True, exist_ok=True)
    with (backlog / "verification-findings.jsonl").open("a") as f:
        for finding in mine:
            f.write(json.dumps({"run_id": ctx.run_id, "spec_id": sid, "round": round_no, "finding": finding}) + "\n")
    ctx.event("verification.backlog", {"round": round_no, "count": len(mine), "finding_ids": [f["finding_id"] for f in mine]}, spec_id=sid)


def _require_recorded_preview_mode(ctx: RunContext) -> None:
    """A run started in managed mode never continues with controller-side execution: a resume without the managed
    configuration would otherwise build a local environment and run the repository on this host."""
    from .common import hosted_run
    if hosted_run(ctx.store.events(ctx.run_id)) and ctx.cfg.preview_mode != "managed":
        raise RuntimeError("this run verifies candidates in a hosted sandbox (preview mode managed); continue it with "
                           "FACTORY_PREVIEW=managed and the same app binding, never with local or platform execution")


def _manifest_or_none(path: Path) -> dict[str, Any] | None:
    """The service manifest as a dict, or None when it is missing or not JSON (`manifest_valid` reports that)."""
    try:
        m = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return m if isinstance(m, dict) else None


def _local_preview_e2e(ctx: RunContext, repo: Path, branch: str, head_sha: str, verifier: "LocalVerifier",
                       spec: dict[str, Any], manifest: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Start the checked-out service from its manifest, in the per-run environment, and run the spec's preview
    entries against it.

    The returned preview record is the one agents and the change request see: status `unreachable_from_sandbox`,
    url null (nobody but the orchestrator can reach this instance), and `orchestrator_instance` saying whether the
    service started and where the orchestrator's own e2e rows ran."""
    preview = preview_info(ctx, branch, head_sha)
    instance: dict[str, Any] = {"status": "failed", "url": None}
    preview["orchestrator_instance"] = instance
    if manifest is None:
        manifest = _manifest_or_none(repo / ctx.cfg.service_manifest_path)
    if manifest is None:
        instance["error"] = f"no readable service manifest at {ctx.cfg.service_manifest_path}; cannot start the service"
        return preview, verifier.run_e2e(None, spec["test_plan"].get("e2e", []), repo=repo)
    if verifier.setup is not None and not verifier.setup.passed:
        instance["error"] = f"not started: {SETUP_CHECK} failed ({verifier.setup.detail[:300]})"
        return preview, verifier.run_e2e(None, spec["test_plan"].get("e2e", []), repo=repo)
    try:
        with LocalPreview(repo, manifest, env_bin=verifier.env_bin) as url:
            instance.update(status="ready", url=url)
            return preview, verifier.run_e2e(url, spec["test_plan"].get("e2e", []), repo=repo)
    except Exception as e:  # noqa: BLE001
        instance["error"] = str(e)[:500]
        return preview, verifier.run_e2e(None, spec["test_plan"].get("e2e", []), repo=repo)


def _deterministic_only(findings: list[dict[str, Any]]) -> bool:
    """True when `findings` is non-empty and every entry is one of the orchestrator's own DET-* checks."""
    return bool(findings) and all(str(f.get("finding_id", "")).startswith("DET-") for f in findings)


def is_environmental(results: list[dict[str, Any]]) -> bool:
    """A failed command whose output blames the verification host (a port already bound) rather than the change."""
    for r in results:
        if r.get("exit_code", 1) in (0, None):
            continue
        text = ((r.get("stderr_tail") or "") + (r.get("stdout_tail") or "")).lower()
        if any(m in text for m in ENVIRONMENTAL_MARKERS):
            return True
    return False


def managed_prechecks(added_lines_by_file: dict[str, list[str]]) -> dict[str, Any]:
    """Security pre-checks without executing anything on this host: the secret scan reads the diff text; the dependency
    audit and SAST would run repository tooling, which managed mode only does in a hosted sandbox."""
    reason = "not run on the orchestrator host in managed preview mode; the security reviewer audits dependencies and code in its own sandbox"
    return {"secret_scan": security.secret_scan(added_lines_by_file),
            "dependency_audit": {"tool": None, "status": "skipped", "reason": reason},
            "sast": {"tool": None, "status": "skipped", "reason": reason}}


def deterministic_findings(det: list[Any], command_results: list[dict[str, Any]] | None = None,
                           e2e_results: list[dict[str, Any]] | None = None, *, managed: bool = False) -> list[dict[str, Any]]:
    """Findings (review_findings schema shape) for every failed deterministic check, so the implementer sees
    exactly what blocks the merge and can fix or refute it like any other finding.

    A failed `verification_setup` (the per-run environment could not be built) is the one finding for that round:
    the command and e2e checks it kept from running are consequences, not separate defects. A command or e2e
    failure whose output says the port was already in use is tagged `environmental`: the implementer still
    answers it (the usual fix is a command that does not assume host state), and the judge weighs it as such."""
    failed = [d for d in det if not d.passed]
    setup_failed = any(d.check == SETUP_CHECK for d in failed)
    out = []
    for d in failed:
        name = d.check.split(":")[0]
        if setup_failed and (name in ("test_plan_commands", "e2e_preview", "ac_test_mapping") or (managed and name in (ENVIRONMENT_CHECK, "visual_e2e"))):
            continue
        repro, environmental = "", False
        if name == "test_plan_commands":
            rows = [r for r in (command_results or []) if r.get("exit_code", 1) != 0]
            repro = "; ".join(f"{r['command']} -> exit {r.get('exit_code')}: {((r.get('diagnostic') or {}).get('summary') or r.get('stderr_tail') or r.get('stdout_tail') or '')[-300:]}" for r in rows)
            environmental = is_environmental(rows)
        elif name == "e2e_preview":
            rows = [r for r in (e2e_results or []) if r.get("exit_code", 1) != 0]
            repro = "; ".join(f"{r['command']} -> exit {r.get('exit_code')}: {((r.get('diagnostic') or {}).get('summary') or r.get('stderr_tail') or r.get('stdout_tail') or '')[-300:]}" for r in rows)
            environmental = is_environmental(rows)
        fix = {"ac_test_mapping": "Map every automated acceptance criterion to a test in implementation_report.json `criteria` ([{ac_id, tests: [...]}]) using pytest node ids exactly as `pytest --collect-only -q` prints them, or Bun ids `path/x.test.ts::outer describe > test name`; the orchestrator only counts tests its own run of the test plan collected or reported and saw pass (Bun only from direct `bun test` commands).",
               "test_plan_commands": "Make every test_plan command exit 0 from a clean checkout; if a command depends on a port or host, make it robust or pick a free port.",
               "protected_tests": ("Existing tests are protected. An approved spec may name an existing test file by its exact path in "
                                   "allowed_paths; that file may then be updated to test the specified behavior. "
                                   "Globs do not authorize existing test edits. Never delete or rename an existing test file, "
                                   "skip a test, or weaken assertions to make the change pass."),
               SETUP_CHECK: ("The hosted verification could not run on this head; the detail says why. A failed recipe prepare phase is "
                             "fixed in the change. A changed or missing recipe, or test credentials that are not attached, needs a person "
                             "to re-approve the app's recipe: do not edit the recipe to get past it.") if managed else
                            "Make the repository installable into a fresh virtual environment: a pyproject.toml whose dependencies resolve (runtime plus a dev/test extra for the test plan), or a `runtime.setup` command in the service manifest that installs what the tests and the entrypoint need.",
               ENVIRONMENT_CHECK: "The app did not start, its services did not answer their health paths, or the recipe's baseline check failed in the hosted sandbox (hosted_verification.json has the phases). Make the change keep the app starting and the baseline journey working under the approved recipe; do not edit the recipe."}.get(name, "Fix the cause named in the detail.")
        if environmental:
            fix = ("The failure reads as environmental (a port already in use on the verification host), not as a defect in the "
                   "change: make the command bind a free port or reuse $BASE_URL instead of a fixed port, then push; the "
                   "orchestrator re-runs the check. ") + fix
        out.append({
            "finding_id": f"DET-{name}", "severity": "blocking",
            "category": "security" if name == "secret_scan" else ("test_quality" if name in ("ac_test_mapping", "test_plan_commands", "test_weakening", "protected_tests", "e2e_coverage", "e2e_preview", SETUP_CHECK, ENVIRONMENT_CHECK) else "spec_deviation"),
            "claim": f"Deterministic check `{name}` failed: {d.detail}",
            "evidence": {"locations": ["deterministic_checks.json:1"], **({"reproduction": repro} if repro else {}), "observed": d.detail},
            "violates": [f"orchestrator check {name} (cannot be overridden by any judge)"],
            "refutation_condition": f"`{name}` passes on the orchestrator's next verification of the pushed head",
            "status": "open",
            "suggested_fix": fix,
            **({"environmental": True} if environmental else {}),
        })
    return out


def merge_findings(passes: list[dict[str, Any]], spec_id: str, round_no: int, base_sha: str, head_sha: str,
                   branch: str, previous: dict[str, Any] | None = None, response: dict[str, Any] | None = None) -> dict[str, Any]:
    """Union of independent reviewer passes, with ids that survive rounds (post-mortem issue 14).

    Round 1: findings are de-duplicated by (category, first location), the strictest severity wins, and they are
    numbered F-1.. in severity order. A later round gets `previous` (the merged findings the implementer answered)
    and `response` (its review_response.json): a finding a reviewer re-adjudicated keeps its previous id (matched on
    `previous_finding_id`, on its own `finding_id` when the category matches, or on (category, location)), the
    stricter re-evaluation wins across passes (upheld over withdrawn, then the higher severity), every previous
    finding no pass mentioned (the orchestrator's DET-* included) is carried with the implementer's disposition as
    its status, and only new findings are numbered, from the highest previous number on. Each finding and each
    checks_performed row carries the pass that produced it; scope_check is the union of the passes'."""
    prev_findings = (previous or {}).get("findings", [])
    prev_by_id = {f["finding_id"]: f for f in prev_findings}
    prev_by_key = {_loc_key(f): f["finding_id"] for f in reversed(prev_findings)}
    disposition = {r.get("finding_id"): r.get("disposition") for r in (response or {}).get("responses", [])}
    carried: dict[str, dict[str, Any]] = {}
    fresh: dict[tuple[str, str, str], dict[str, Any]] = {}
    checks_performed: list[dict[str, Any]] = []
    conformance: dict[str, dict[str, Any]] = {}
    scope: dict[str, set[str]] = {"files_outside_allowed_paths": set(), "unrelated_changes": set()}
    for p in passes:
        checks_performed.extend(p.get("checks_performed", []))
        for f in p.get("findings", []):
            pid = _previous_id(f, prev_by_id, prev_by_key)
            if pid is not None:
                f = {**f, "finding_id": pid, "previous_finding_id": pid}
                if pid not in carried or _reevaluation_rank(f) > _reevaluation_rank(carried[pid]):
                    carried[pid] = f
                continue
            key = (*_loc_key(f), f.get("target", "change"))      # a verification finding never hides a change finding
            if key not in fresh or _sev(f) > _sev(fresh[key]):
                fresh[key] = f
        for row in p.get("spec_conformance", []):
            prev = conformance.get(row["ac_id"])
            # The stricter status wins across passes.
            if prev is None or _status_rank(row["status"]) > _status_rank(prev["status"]):
                conformance[row["ac_id"]] = row
        for k in scope:
            scope[k] |= set((p.get("scope_check") or {}).get(k, []))
    findings: list[dict[str, Any]] = []
    for fid, prev in prev_by_id.items():                      # previous order first, ids intact
        if fid in carried:
            findings.append(carried[fid])
        else:
            answered = disposition.get(fid)
            findings.append({**prev, "status": answered if answered in ("fixed", "refuted", "accepted_risk") else prev["status"]})
    next_no = max([int(fid[2:]) for fid in prev_by_id if re.fullmatch(r"F-[0-9]+", fid)] + [0]) + 1
    for i, f in enumerate(sorted(fresh.values(), key=_sev, reverse=True), next_no):
        f["finding_id"] = f"F-{i}"
        findings.append(f)
    return {
        "review_id": f"rev_{spec_id.replace('spec_', '')}{round_no:02d}{head_sha[:6]}",
        "spec_id": spec_id, "round": round_no,
        "diff_ref": {"base_sha": base_sha, "head_sha": head_sha, "branch": branch},
        "checks_performed": checks_performed, "findings": findings,
        "spec_conformance": list(conformance.values()),
        "scope_check": {k: sorted(v) for k, v in scope.items()},
        "produced_by": passes[0].get("produced_by", {}),
    }


def apply_deterministic(findings: list[dict[str, Any]], det_findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold this round's failed deterministic checks into the merged findings: a DET-* id that is failing again
    replaces its carried entry (open and blocking once more), a carried DET-* whose check now passes is `fixed`
    unless the implementer's disposition already closed it, and new failures are appended."""
    failing = {f["finding_id"]: f for f in det_findings}
    out = []
    for f in findings:
        fid = f["finding_id"]
        if fid in failing:
            out.append(failing.pop(fid))
        elif fid.startswith("DET-") and f.get("status") in ("open", "upheld"):
            out.append({**f, "status": "fixed"})
        else:
            out.append(f)
    return out + list(failing.values())


def _loc_key(f: dict[str, Any]) -> tuple[str, str]:
    loc = (f.get("evidence") or {}).get("locations") or [""]
    return (f.get("category", ""), loc[0].split(":")[0] + ":" + loc[0].split(":")[-1][:3])


def _previous_id(f: dict[str, Any], prev_by_id: dict[str, dict[str, Any]], prev_by_key: dict[tuple[str, str], str]) -> str | None:
    pid = f.get("previous_finding_id")
    if pid in prev_by_id:
        return pid
    own = f.get("finding_id")
    if own in prev_by_id and prev_by_id[own].get("category") == f.get("category"):
        return own
    return prev_by_key.get(_loc_key(f))


def _reevaluation_rank(f: dict[str, Any]) -> tuple[int, bool, int]:
    return ({"upheld": 3, "open": 2, "fixed": 1, "refuted": 1, "accepted_risk": 1, "withdrawn": 0}.get(f.get("status", "open"), 2),
            f.get("target") != "verification", _sev(f))


def tag_pass(findings: dict[str, Any], label: str) -> dict[str, Any]:
    """Mark every finding and every checks_performed row of a reviewer pass with the pass that produced it (kept
    through merging, so two passes' probe counts are never read as one)."""
    for f in findings.get("findings", []):
        f.setdefault("pass", label)
    for row in findings.get("checks_performed", []):
        row.setdefault("pass", label)
    return findings


def standing_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings the implementer must answer before the merge judge: open or upheld, and either blocking/major or a
    reproduced security, concurrency or data-integrity defect whatever label the reviewer put on it (reviewers
    under-rate rare races; see docs/postmortems/2026-10-10-first-live-runs.md issue 8). A finding about the journey's own
    plumbing (`target: verification`) never stands: it is recorded for the factory (post-mortem 2026-10-11 §1)."""
    out = []
    for f in findings:
        if f["status"] not in ("open", "upheld") or f.get("target") == "verification":
            continue
        reproduced = bool((f.get("evidence") or {}).get("reproduction"))
        if f["severity"] in ("blocking", "major") or (f["category"] in ("security", "concurrency", "data_integrity") and reproduced):
            out.append(f)
    return out


def review_plan(policy: Any, size_measured: dict[str, Any] | None, previous: dict[str, Any] | None,
                response: dict[str, Any] | None, delta_added_lines: dict[str, list[str]] | None,
                security_available: bool) -> dict[str, Any]:
    """The shape of one review round: how many general passes, whether the security pass runs, and what they read.

    A first round reads the whole diff: one general pass when the change is at most `review_single_pass_lines`
    hand-written lines, `reviewer_passes` above that. A later round (the implementer answered `previous`) reads the delta
    since the head the previous round reviewed, with `reviewer_passes_followup` passes that also adjudicate every
    previous finding; the security pass comes back only when one of its findings still stands or the delta's secret scan
    flags a line. Unadjudicated findings keep the implementer's disposition (`merge_findings`)."""
    security_on = bool(security_available and getattr(policy, "security_review", True))
    previous_head = ((previous or {}).get("diff_ref") or {}).get("head_sha") if previous and response else None
    if previous_head:
        standing_security = [f["finding_id"] for f in previous.get("findings", [])
                             if f.get("pass") == "security" and f.get("status") in ("open", "upheld")]
        scan = security.secret_scan(delta_added_lines or {})
        flagged = bool(scan["hits"] or scan["suspects"])
        why = ("previous security findings stand: " + ", ".join(standing_security) if standing_security
               else "the delta's secret scan flagged a line" if flagged else "no security finding stands and the delta is clean")
        return {"scope": "delta", "previous_head": previous_head,
                "passes": max(1, int(getattr(policy, "reviewer_passes_followup", 1))),
                "security": security_on and bool(standing_security or flagged), "security_why": why}
    hand = (size_measured or {}).get("hand_written")
    small = hand is not None and hand <= int(getattr(policy, "review_single_pass_lines", 0) or 0)
    return {"scope": "full", "previous_head": None, "hand_written_lines": hand,
            "passes": 1 if small else max(1, int(policy.reviewer_passes)),
            "security": security_on, "security_why": "first round" if security_on else "off or not deployed"}


def delta_since(verifier: LocalVerifier, repo: Path, previous_head: str, head_sha: str) -> dict[str, Any] | None:
    """What changed between the head the previous round reviewed and this one, or None when git cannot say (the previous
    head is gone from the branch, which a review of the whole diff then covers)."""
    if previous_head == head_sha:
        return None
    try:
        return verifier.analyse(repo, previous_head, head_sha)
    except subprocess.CalledProcessError:
        return None


def _sev(f: dict[str, Any]) -> int:
    return {"nit": 0, "minor": 1, "major": 2, "blocking": 3}[f["severity"]]


def _status_rank(s: str) -> int:
    return {"met": 0, "not_assessed": 1, "untestable_as_written": 2, "not_met": 3}[s]
