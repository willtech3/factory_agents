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
"""
from __future__ import annotations

from dataclasses import dataclass

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .. import checks, security
from ..state import WorkItemStage
from . import RunContext
from .common import persist_outputs, provenance, require_json, artifact_json_or_none

_SHELL: tuple[str | None, str | None] | None = None


def verification_shell(workdir: Path) -> tuple[str | None, str | None]:
    """The shell the verifier runs spec commands with: the newest bash 4+ on the host, plus a directory holding an
    `sh` symlink to it to put first on PATH so a nested `sh -c` in a command gets the same shell.

    macOS ships /bin/sh as bash 3.2, which mis-parses escaped quotes inside "$( ... )" and brace-expands the result;
    the sandbox the agents wrote and tested the command in runs bash 5. Live greenfield run: a passing end-to-end
    check failed with `test: too many arguments` on the orchestrator host for exactly that reason. Returns
    (executable, shim_dir), both None when no bash 4+ exists (then /bin/sh is used and a hint is printed once)."""
    import shutil

    global _SHELL
    if _SHELL is not None:
        return _SHELL
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
        _SHELL = (None, None)
        return _SHELL
    shim = workdir / "shell-shim"
    shim.mkdir(parents=True, exist_ok=True)
    link = shim / "sh"
    if not link.exists() or os.readlink(link) != chosen:
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(chosen)
    _SHELL = (chosen, str(shim))
    return _SHELL


def _shell_env(workdir: Path, extra: dict[str, str] | None = None) -> tuple[dict[str, str], str | None]:
    """Environment for a verifier subprocess: the orchestrator's interpreter and the modern shell first on PATH."""
    exe, shim = verification_shell(workdir)
    path = f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"
    if shim:
        path = f"{shim}{os.pathsep}{path}"
    return {**os.environ, **(extra or {}), "PATH": path}, exe


NO_FORGE_GIT_SETUP = """Git access: no forge is configured for this run. The repository at {repo_url} is not reachable
from the sandbox; work on branch {branch} from {base_branch} only if it was mounted for you, otherwise record the
blocker in blocked.md."""

IMPLEMENT_DESCRIPTION = """Implement specification {spec_id} from /mnt/session/uploads/spec.json (spec.md is the readable form).
{git}
{review_clause}
Write implementation_report.json{response_clause} to /mnt/session/outputs/. The report must map every acceptance criterion
to the tests that prove it, as `criteria: [{{"ac_id": "AC-1", "tests": ["tests/test_x.py::test_name", ...]}}, ...]`
(pytest node ids or equivalent test ids); the orchestrator's merge checks read that list and fail any automated criterion
without a passing test. Run every command in the spec's test_plan before finishing and record the results. {preview_clause}"""

REVIEW_DESCRIPTION = """Review the change for specification {spec_id}. The diff is at /mnt/session/uploads/diff.patch
(base {base_sha}, head {head_sha}); the repository is cloned at /workspace/repo on branch {branch}.
{git}
This is review round {round}.{second_round}
{preview_clause}
Write findings.json to /mnt/session/outputs/ and validate it. You never edit source files."""

PREVIEW_CLAUSE = """The platform has deployed this branch to a preview environment; its URL and pipeline are in
/mnt/session/uploads/preview.json. Run every test_plan.e2e entry with target "preview" against that URL (export
BASE_URL from preview.json) and record each command's exit code and output. The orchestrator re-runs them itself."""

LOCAL_PREVIEW_CLAUSE = """No preview environment is reachable from this sandbox (see /mnt/session/uploads/preview.json). Run the
test_plan.e2e entries with target "local" now against an instance you start yourself and record the results; record the
"preview" entries as not run. The orchestrator runs every "preview" entry itself against an instance it starts from the
service manifest's entrypoint, so the service must start with that command and answer the manifest's health path."""

NO_PREVIEW_CLAUSE = """No preview environment exists for this branch yet (see "status" in /mnt/session/uploads/preview.json).
Push your branch early; the platform's pipeline deploys it. Run the test_plan.e2e entries with target "local" now and
record the "preview" ones as not run. The orchestrator runs the preview entries against the deployed branch once the
pipeline for your pushed head finishes; any later round of yours receives preview.json with the URL."""

SECURITY_REVIEW_DESCRIPTION = """Security review of the change for specification {spec_id}. The diff is at /mnt/session/uploads/diff.patch
(base {base_sha}, head {head_sha}); the repository is cloned at /workspace/repo on branch {branch}.
{git}
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
Write merge_readiness.json to /mnt/session/outputs/ and validate it."""


def _git(repo_dir: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo_dir, check=True, capture_output=True, text=True).stdout


class LocalPreview:
    """Start the service from its manifest on a free port, wait for its health path, serve as the preview.

    The development stand-in for the platform's per-branch preview (`FACTORY_PREVIEW=local`): the manifest's
    `runtime.entrypoint` runs from the checkout with the manifest's port replaced by a free one (also exported as
    PORT), and `health.path` must answer 200 within `timeout`. Dependencies must be importable from the
    orchestrator's interpreter (its bin directory is put first on PATH), which is true for the sample app."""

    def __init__(self, repo: Path, manifest: dict[str, Any], timeout: float = 60.0):
        self.repo, self.manifest, self.timeout = repo, manifest, timeout
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
        env, shell = _shell_env(self.repo.parent, {"PORT": str(port)})
        self.proc = subprocess.Popen(cmd, shell=True, executable=shell, cwd=self.repo, env=env, stdout=self.log.open("w"), stderr=subprocess.STDOUT,
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


class LocalVerifier:
    """Orchestrator-side clone used for the authoritative test run and diff analysis.

    Runs the spec's test commands in a clean checkout of head_sha. In production this is a
    CI job on the branch (GitHub Actions or GitLab CI); the interface is the same.
    """

    def __init__(self, repo_url_with_auth: str, workdir: Path):
        self.repo_url = repo_url_with_auth
        self.workdir = workdir

    def checkout(self, branch: str) -> Path:
        repo = self.workdir / "repo"
        if not repo.exists():
            subprocess.run(["git", "clone", "--quiet", self.repo_url, str(repo)], check=True)
        _git(repo, "fetch", "--quiet", "origin", branch)
        _git(repo, "checkout", "--quiet", "-B", branch, f"origin/{branch}")
        return repo

    def analyse(self, repo: Path, base_sha: str, head_sha: str) -> dict[str, Any]:
        names = _git(repo, "diff", "--name-status", f"{base_sha}..{head_sha}").splitlines()
        changed, deleted = [], []
        for line in names:
            status, _, path = line.partition("\t")
            (deleted if status.startswith("D") else changed).append(path.split("\t")[-1])
        added_lines: dict[str, list[str]] = {}
        current = None
        for line in _git(repo, "diff", "--unified=0", f"{base_sha}..{head_sha}").splitlines():
            if line.startswith("+++ b/"):
                current = line[6:]
                added_lines.setdefault(current, [])
            elif line.startswith("+") and not line.startswith("+++") and current:
                added_lines[current].append(line[1:])
        existing_tests = [p for p in _git(repo, "ls-tree", "-r", "--name-only", base_sha).splitlines()
                          if checks.is_test_file(p)]
        return {"changed": changed, "deleted": deleted, "added_lines": added_lines, "existing_tests": existing_tests,
                "patch": _git(repo, "diff", f"{base_sha}..{head_sha}")}

    def run_commands(self, repo: Path, commands: list[str], timeout: int = 1800) -> list[dict[str, Any]]:
        results = []
        env, shell = _shell_env(self.workdir)
        for cmd in commands:
            p = subprocess.run(cmd, shell=True, executable=shell, cwd=repo, env=env, capture_output=True, text=True, timeout=timeout)
            results.append({"command": cmd, "exit_code": p.returncode,
                            "stdout_tail": p.stdout[-4000:], "stderr_tail": p.stderr[-4000:]})
        return results

    def run_e2e(self, preview_url: str | None, e2e_entries: list[dict[str, Any]], repo: Path | None = None,
                target: str = "preview", timeout: int = 900) -> list[dict[str, Any]]:
        """Run the spec's `test_plan.e2e` entries for `target` against a running deployment.

        `$BASE_URL` (and the env var BASE_URL) is the environment URL the orchestrator read from the forge's
        deployment record, never a URL an agent reported. Entries are run from the checkout so fixtures
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
                results.append({**row, "exit_code": None, "stderr_tail": "no preview URL available for this branch"})
                continue
            # The shell expands $BASE_URL, ${BASE_URL} and ${BASE_URL:?...} from the environment. Substituting the
            # text ourselves broke the guarded form (`${http://127.0.0.1:5xxxx:?...}`) and failed a passing check.
            env, shell = _shell_env(self.workdir, {"BASE_URL": preview_url or ""})
            cmd = e["command"]
            try:
                p = subprocess.run(cmd, shell=True, executable=shell, cwd=repo or self.workdir, env=env, capture_output=True,
                                   text=True, timeout=timeout)
                results.append({**row, "exit_code": p.returncode, "stdout_tail": p.stdout[-4000:],
                                "stderr_tail": p.stderr[-4000:]})
            except subprocess.TimeoutExpired:
                results.append({**row, "exit_code": 124, "stderr_tail": f"timed out after {timeout}s"})
        return results


def _branch_for(run_id: str, spec_id: str) -> str:
    return f"factory/{run_id}/{spec_id}"


@dataclass
class ResumePoint:
    """Where `run_implement_one` picks a spec up again after the orchestrator died mid-loop (`factory resume`).

    `round_no` is the implementer round to continue at. `report` (and `response`, from round 2 on) are that round's
    outputs when its session finished and they were persisted: the implementer is then not run again and the loop
    goes straight to the orchestrator's verification. `findings` is the merged review of the previous round, which
    that round answers. Reviewer passes and the merge verdict already persisted for the round are reused too."""
    round_no: int = 1
    findings: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    response: dict[str, Any] | None = None


def wait_for_preview(ctx: RunContext, branch: str, head_sha: str, *, poll_seconds: float = 20.0,
                     sleep=None, clock=None) -> dict[str, Any]:
    """Give the platform's pipeline time to deploy `head_sha` before running e2e against the preview.

    Bounded by `policy.preview_wait_seconds`; a preview still `pending` or `unavailable` at the deadline
    stays that way and `e2e_preview` fails closed (not run is not passed). Polling is the source of truth;
    a pipeline webhook can shorten the wait but is not relied on.
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
    `failed`, or `unavailable` (no forge, or no pipeline yet). UNVERIFIED against a live forge.
    """
    info: dict[str, Any] = {"branch": branch, "status": "unavailable", "url": None, "pipeline": None,
                            "head_sha": head_sha}
    if ctx.cfg.preview_mode == "local":
        info["mode"] = "local"
        info["note"] = "the orchestrator starts the service itself from the manifest and runs the preview checks against it"
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


def run_implement_one(ctx: RunContext, spec: dict[str, Any], repo_url: str, base_branch: str,
                      verifier: LocalVerifier, dependency_specs: dict[str, dict], *,
                      resume_from: ResumePoint | None = None) -> dict[str, Any]:
    """Drive one spec through implement/review/judge. Returns the final merge_readiness verdict.

    `resume_from` continues the loop after a dead orchestrator (see `ResumePoint`); the work item then already
    sits in implementing or responding, so no stage event is emitted for the restart."""
    sid, wid = spec["spec_id"], spec["work_item_id"]
    branch = _branch_for(ctx.run_id, sid)
    git = (ctx.forge.sandbox_git_instructions(repo_url, base_branch, branch) if ctx.forge is not None
           else NO_FORGE_GIT_SETUP.format(repo_url=repo_url, base_branch=base_branch, branch=branch))

    def mount(checkout: str) -> dict[str, Any] | None:
        return ctx.forge.session_repository_resource(repo_url, checkout) if ctx.forge is not None else None
    if resume_from is None:
        ctx.event("work_item.stage", {"stage": WorkItemStage.IMPLEMENTING.value, "branch": branch}, work_item_id=wid, spec_id=sid)

    findings: dict[str, Any] | None = resume_from.findings if resume_from else None
    response: dict[str, Any] | None = None
    verdict: dict[str, Any] | None = None
    base_sha = _git(verifier.checkout(base_branch), "rev-parse", "HEAD").strip()

    for round_no in range(resume_from.round_no if resume_from else 1, ctx.policy.review_max_rounds + 2):
        resumed_round = resume_from is not None and round_no == resume_from.round_no
        # ---- implementer (round 1 implements; later rounds respond to findings)
        uploads = {"spec.json": json.dumps(spec, indent=2).encode(), "spec.md": spec.get("_markdown", "").encode()}
        for dsid, ds in dependency_specs.items():
            uploads[f"dependency_specs/{dsid}.json"] = json.dumps(ds, indent=2).encode()
        review_clause, response_clause = "", ""
        preview = preview_info(ctx, branch)
        uploads["preview.json"] = json.dumps(preview, indent=2).encode()
        preview_clause = PREVIEW_CLAUSE if preview["status"] == "ready" else (LOCAL_PREVIEW_CLAUSE if preview.get("mode") == "local" else NO_PREVIEW_CLAUSE)
        if findings:
            uploads["findings.json"] = json.dumps(findings, indent=2).encode()
            review_clause = ("Reviewer findings are at /mnt/session/uploads/findings.json. Respond to every finding "
                             "(fixed / refuted with evidence / accepted_risk) and push the updated branch.")
            response_clause = " and review_response.json"
        if resumed_round and resume_from.report is not None:
            # This round's implementer finished before the orchestrator died; its outputs are in the store.
            report, response = resume_from.report, resume_from.response
            if findings and response is None:
                raise RuntimeError(f"cannot resume {sid} at round {round_no}: no review_response for round {round_no - 1}'s findings")
        else:
            res = ctx.driver.run(
                agent_key="implementer", title=f"{ctx.run_id} implement {sid} r{round_no}", run_id=ctx.run_id,
                stage="implement", uploads=uploads, work_item_id=wid,
                description=IMPLEMENT_DESCRIPTION.format(spec_id=sid, git=git, review_clause=review_clause,
                                                         response_clause=response_clause, preview_clause=preview_clause),
                rubric_md=ctx.rubric("implementation"), max_iterations=ctx.policy.outcome_max_iterations,
                budget_usd=ctx.policy.stage_budget_usd["implement"],
                repository=mount(base_branch if round_no == 1 else branch),
            )
            if "blocked.md" in res.outputs:
                persist_outputs(ctx, "implement", res, prefix=f"{sid}-r{round_no}-")
                ctx.event("work_item.stage", {"stage": WorkItemStage.BLOCKED.value}, work_item_id=wid, spec_id=sid)
                return {"result": "escalate", "reason": res.outputs["blocked.md"].decode(errors="replace")}
            report = require_json(res, "implementation_report.json")
            if findings:
                response = require_json(res, "review_response.json")
            persist_outputs(ctx, "implement", res, prefix=f"{sid}-r{round_no}-")
        if findings and response is not None:
            response["round"] = round_no - 1
            ctx.event("review.response", {"round": round_no - 1, "dispositions": [r["disposition"] for r in response["responses"]]},
                      work_item_id=wid, spec_id=sid, actor="agent")

        # ---- authoritative verification (orchestrator side)
        repo = verifier.checkout(branch)
        head_sha = _git(repo, "rev-parse", "HEAD").strip()
        diff = verifier.analyse(repo, base_sha, head_sha)
        cmd_results = verifier.run_commands(repo, spec["test_plan"]["commands"])
        # End-to-end against the preview the platform deployed for head_sha. The URL comes from the forge's
        # deployment record, not from the implementer's report; "not run" counts as failed.
        if ctx.cfg.preview_mode == "local":
            preview, e2e_results = _local_preview_e2e(ctx, repo, branch, head_sha, verifier, spec)
        else:
            preview = wait_for_preview(ctx, branch, head_sha)
            e2e_results = verifier.run_e2e(preview["url"] if preview["status"] == "ready" else None,
                                           spec["test_plan"].get("e2e", []), repo=repo)
        passing_tests = [t for ts in checks.criteria_mapping(report).values() for t in ts]  # refined by a CI parser later
        det = checks.run_all_for_merge(
            spec=spec, changed_files=diff["changed"], deleted_files=diff["deleted"], existing_test_files=diff["existing_tests"],
            added_lines_by_file=diff["added_lines"], implementation_report=report, passing_tests=passing_tests,
            command_results=cmd_results, manifest_path=repo / ctx.cfg.service_manifest_path,
            schema_dir=ctx.cfg.schemas_dir, e2e_results=e2e_results,
        )
        ctx.store.put_artifact(ctx.run_id, "implement", f"{sid}-r{round_no}-deterministic_checks.json",
                               json.dumps([d.to_dict() for d in det], indent=2).encode())
        ctx.store.put_artifact(ctx.run_id, "implement", f"{sid}-r{round_no}-test_results.json",
                               json.dumps({"commands": cmd_results, "e2e": e2e_results, "preview": preview}, indent=2).encode())

        if round_no > ctx.policy.review_max_rounds:
            break  # last implementer response done; go straight to the merge judge

        # ---- fresh-context adversarial review (N independent passes, merged)
        ctx.event("work_item.stage", {"stage": WorkItemStage.REVIEWING.value, "review_round": round_no}, work_item_id=wid, spec_id=sid)
        rev_uploads = {"spec.json": uploads["spec.json"], "spec.md": uploads["spec.md"],
                       "diff.patch": diff["patch"].encode(),
                       "implementation_report.json": json.dumps(report, indent=2).encode(),
                       "preview.json": json.dumps(preview, indent=2).encode(),
                       "deterministic_checks.json": json.dumps([d.to_dict() for d in det], indent=2).encode()}
        rev_preview_clause = PREVIEW_CLAUSE if preview["status"] == "ready" and preview.get("mode") != "local" else (
            LOCAL_PREVIEW_CLAUSE if preview.get("mode") == "local" else NO_PREVIEW_CLAUSE)
        second = ""
        if findings and response:
            rev_uploads["previous_findings.json"] = json.dumps(findings, indent=2).encode()
            rev_uploads["review_response.json"] = json.dumps(response, indent=2).encode()
            second = (" Judge each previous finding against your refutation condition using review_response.json "
                      "(upheld/withdrawn), then review the new hunks.")

        def one_pass(i: int) -> dict[str, Any]:
            if resumed_round:
                prior = artifact_json_or_none(ctx, "review", f"{sid}-r{round_no}-p{i}-findings.json")
                if prior is not None:
                    return tag_pass(prior, str(i))      # this pass finished before the orchestrator died
            r = ctx.driver.run(
                agent_key="reviewer", title=f"{ctx.run_id} review {sid} r{round_no} p{i}", run_id=ctx.run_id,
                stage="review", uploads=rev_uploads, work_item_id=wid,
                description=REVIEW_DESCRIPTION.format(spec_id=sid, base_sha=base_sha, head_sha=head_sha, branch=branch,
                                                      git=git, round=round_no, second_round=second,
                                                      preview_clause=rev_preview_clause),
                rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["review"],
                metadata={"pass": str(i)}, repository=mount(branch),
            )
            f = require_json(r, "findings.json")
            persist_outputs(ctx, "review", r, prefix=f"{sid}-r{round_no}-p{i}-")
            return tag_pass(f, str(i))

        def security_pass() -> dict[str, Any] | None:
            """One security reviewer beside the general passes; skipped when the policy or the deployment lacks it."""
            if not ctx.policy.security_review or "security_reviewer" not in ctx.cfg.agents:
                return None
            if resumed_round:
                prior = artifact_json_or_none(ctx, "review", f"{sid}-r{round_no}-sec-findings.json")
                if prior is not None:
                    return tag_pass(prior, "security")
            pre = security.prechecks(repo, diff["changed"], diff["added_lines"])
            ctx.store.put_artifact(ctx.run_id, "review", f"{sid}-r{round_no}-security_prechecks.json", json.dumps(pre, indent=2).encode())
            r = ctx.driver.run(
                agent_key="security_reviewer", title=f"{ctx.run_id} security-review {sid} r{round_no}", run_id=ctx.run_id,
                stage="review", uploads={**rev_uploads, "security_prechecks.json": json.dumps(pre, indent=2).encode()}, work_item_id=wid,
                description=SECURITY_REVIEW_DESCRIPTION.format(spec_id=sid, base_sha=base_sha, head_sha=head_sha, branch=branch,
                                                               git=git, round=round_no, second_round=second,
                                                               preview_clause=rev_preview_clause),
                rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["security_review"],
                metadata={"pass": "security"}, repository=mount(branch),
            )
            f = require_json(r, "findings.json")
            persist_outputs(ctx, "review", r, prefix=f"{sid}-r{round_no}-sec-")
            return tag_pass(f, "security")

        with ThreadPoolExecutor(max_workers=ctx.policy.reviewer_passes + 1) as pool:
            sec_future = pool.submit(security_pass)
            passes = list(pool.map(one_pass, range(1, ctx.policy.reviewer_passes + 1)))
            sec = sec_future.result()
        findings = merge_findings(passes + ([sec] if sec else []), sid, round_no, base_sha, head_sha, branch)
        # A failed deterministic check is a finding the implementer must answer; nobody else can clear it.
        findings["findings"].extend(deterministic_findings(det, cmd_results))
        ctx.store.put_artifact(ctx.run_id, "review", f"{sid}-r{round_no}-findings.merged.json",
                               json.dumps(findings, indent=2).encode())
        ctx.event("review.findings", {"round": round_no, "count": len(findings["findings"]),
                                      "blocking": sum(f["severity"] == "blocking" for f in findings["findings"])},
                  work_item_id=wid, spec_id=sid, actor="agent")
        standing = standing_findings(findings["findings"])
        if not standing and all(d.passed for d in det):
            break  # nothing standing; skip the implementer round and go to the judge
        ctx.event("work_item.stage", {"stage": WorkItemStage.RESPONDING.value}, work_item_id=wid, spec_id=sid)

    # ---- merge judge (independent)
    ctx.event("work_item.stage", {"stage": WorkItemStage.MERGE_JUDGING.value}, work_item_id=wid, spec_id=sid)
    mj_uploads = {
        "spec.json": json.dumps(spec, indent=2).encode(), "diff.patch": diff["patch"].encode(),
        "findings.json": json.dumps(findings or {"findings": []}, indent=2).encode(),
        "review_response.json": json.dumps(response or {"responses": []}, indent=2).encode(),
        "test_results.json": json.dumps({"commands": cmd_results, "e2e": e2e_results, "preview": preview}, indent=2).encode(),
        "deterministic_checks.json": json.dumps([d.to_dict() for d in det], indent=2).encode(),
    }
    prior_verdict = artifact_json_or_none(ctx, "merge_judge", f"{sid}-merge_readiness.json") if resume_from else None
    if prior_verdict is not None and prior_verdict.get("_head_sha") == head_sha:
        ctx.event("merge.verdict", {"result": prior_verdict["result"], "reused": True}, work_item_id=wid, spec_id=sid, actor="agent")
        return prior_verdict      # judged this very head before the orchestrator died
    res = ctx.driver.run(
        agent_key="merge_judge", title=f"{ctx.run_id} merge-judge {sid}", run_id=ctx.run_id, stage="merge_judge",
        uploads=mj_uploads, work_item_id=wid,
        description=MERGE_JUDGE_DESCRIPTION.format(spec_id=sid, round=len([1 for _ in range(1)]), git=git),
        rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["merge_judge"],
        repository=mount(branch),
    )
    verdict = require_json(res, "merge_readiness.json")
    # Deterministic checks are not negotiable: a failing check forces another_round or escalate.
    if any(not d.passed for d in det) and verdict["result"] == "ready":
        verdict["result"] = "another_round"
        verdict["summary_for_human"] = "Overridden: deterministic checks failed. " + verdict["summary_for_human"]
    verdict["deterministic_checks"] = [d.to_dict() for d in det]
    verdict.setdefault("produced_by", provenance(ctx, res, "Merge judge", "claude-opus-5-5", {}))
    verdict["_head_sha"], verdict["_branch"], verdict["_base_sha"] = head_sha, branch, base_sha
    res.outputs["merge_readiness.json"] = json.dumps(verdict, indent=2).encode()
    persist_outputs(ctx, "merge_judge", res, prefix=f"{sid}-")
    ctx.event("merge.verdict", {"result": verdict["result"]}, work_item_id=wid, spec_id=sid, actor="agent")
    return verdict


def _local_preview_e2e(ctx: RunContext, repo: Path, branch: str, head_sha: str, verifier: "LocalVerifier",
                       spec: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Start the checked-out service from its manifest and run the spec's preview entries against it."""
    manifest_path = repo / ctx.cfg.service_manifest_path
    preview: dict[str, Any] = {"branch": branch, "head_sha": head_sha, "mode": "local", "status": "failed", "url": None, "pipeline": None}
    if not manifest_path.is_file():
        preview["error"] = f"no service manifest at {ctx.cfg.service_manifest_path}; cannot start a local preview"
        return preview, verifier.run_e2e(None, spec["test_plan"].get("e2e", []), repo=repo)
    try:
        with LocalPreview(repo, json.loads(manifest_path.read_text())) as url:
            preview.update(status="ready", url=url)
            return preview, verifier.run_e2e(url, spec["test_plan"].get("e2e", []), repo=repo)
    except Exception as e:  # noqa: BLE001
        preview["error"] = str(e)[:500]
        return preview, verifier.run_e2e(None, spec["test_plan"].get("e2e", []), repo=repo)


def deterministic_findings(det: list[Any], command_results: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Findings (review_findings schema shape) for every failed deterministic check, so the implementer sees
    exactly what blocks the merge and can fix or refute it like any other finding."""
    out = []
    for d in det:
        if d.passed:
            continue
        name = d.check.split(":")[0]
        repro = ""
        if name == "test_plan_commands":
            failed = [r for r in (command_results or []) if r.get("exit_code", 1) != 0]
            repro = "; ".join(f"{r['command']} -> exit {r.get('exit_code')}: {(r.get('stderr_tail') or r.get('stdout_tail') or '')[-300:]}" for r in failed)
        out.append({
            "finding_id": f"DET-{name}", "severity": "blocking",
            "category": "security" if name == "secret_scan" else ("test_quality" if name in ("ac_test_mapping", "test_plan_commands", "test_weakening", "protected_tests", "e2e_coverage", "e2e_preview") else "spec_deviation"),
            "claim": f"Deterministic check `{name}` failed: {d.detail}",
            "evidence": {"locations": ["deterministic_checks.json:1"], **({"reproduction": repro} if repro else {}), "observed": d.detail},
            "violates": f"orchestrator check {name} (cannot be overridden by any judge)",
            "refutation_condition": f"`{name}` passes on the orchestrator's next verification of the pushed head",
            "status": "open",
            "suggested_fix": {"ac_test_mapping": "Map every automated acceptance criterion to a test in implementation_report.json `criteria` ([{ac_id, tests: [...]}]) and make sure those tests exist and pass.",
                              "test_plan_commands": "Make every test_plan command exit 0 from a clean checkout; if a command depends on a port or host, make it robust or pick a free port."}.get(name, "Fix the cause named in the detail."),
        })
    return out


def merge_findings(passes: list[dict[str, Any]], spec_id: str, round_no: int, base_sha: str, head_sha: str,
                   branch: str) -> dict[str, Any]:
    """Union of independent reviewer passes, de-duplicated by (category, first location)."""
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    checks_performed: list[dict[str, Any]] = []
    conformance: dict[str, dict[str, Any]] = {}
    for p in passes:
        checks_performed.extend(p.get("checks_performed", []))
        for f in p.get("findings", []):
            key = (f["category"], f["evidence"]["locations"][0].split(":")[0] + ":" + f["evidence"]["locations"][0].split(":")[-1][:3])
            if key not in seen or _sev(f) > _sev(seen[key]):
                seen[key] = f
        for row in p.get("spec_conformance", []):
            prev = conformance.get(row["ac_id"])
            # The stricter status wins across passes.
            if prev is None or _status_rank(row["status"]) > _status_rank(prev["status"]):
                conformance[row["ac_id"]] = row
    findings = sorted(seen.values(), key=_sev, reverse=True)
    for i, f in enumerate(findings, 1):
        f["finding_id"] = f"F-{i}"
    return {
        "review_id": f"rev_{spec_id.replace('spec_', '')}{round_no:02d}{head_sha[:6]}",
        "spec_id": spec_id, "round": round_no,
        "diff_ref": {"base_sha": base_sha, "head_sha": head_sha, "branch": branch},
        "checks_performed": checks_performed, "findings": findings,
        "spec_conformance": list(conformance.values()),
        "produced_by": passes[0].get("produced_by", {}),
    }


def tag_pass(findings: dict[str, Any], label: str) -> dict[str, Any]:
    """Mark every finding of a reviewer pass with the pass that produced it (kept through merging)."""
    for f in findings.get("findings", []):
        f.setdefault("pass", label)
    return findings


def standing_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings the implementer must answer before the merge judge: open or upheld, and either blocking/major or a
    reproduced security, concurrency or data-integrity defect whatever label the reviewer put on it (reviewers
    under-rate rare races; see docs/postmortems/2026-10-10-first-live-runs.md issue 8)."""
    out = []
    for f in findings:
        if f["status"] not in ("open", "upheld"):
            continue
        reproduced = bool((f.get("evidence") or {}).get("reproduction"))
        if f["severity"] in ("blocking", "major") or (f["category"] in ("security", "concurrency", "data_integrity") and reproduced):
            out.append(f)
    return out


def _sev(f: dict[str, Any]) -> int:
    return {"nit": 0, "minor": 1, "major": 2, "blocking": 3}[f["severity"]]


def _status_rank(s: str) -> int:
    return {"met": 0, "not_assessed": 1, "untestable_as_written": 2, "not_met": 3}[s]
