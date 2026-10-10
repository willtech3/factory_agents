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
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .. import checks, security
from ..state import WorkItemStage
from . import RunContext
from .common import persist_outputs, public, require_json, run_session, stamp_provenance, artifact_json_or_none

NO_FORGE_GIT_SETUP = """Git access: no forge is configured for this run. The repository at {repo_url} is not reachable
from the sandbox; work on branch {branch} from {base_branch} only if it was mounted for you, otherwise record the
blocker in blocked.md."""

NO_FORGE_GIT_READ_ONLY = """Git access: no forge is configured for this run. If the repository at {repo_url} was mounted for
you, read branch {branch} there; this session is read-only on it: never commit, never push, and write everything you
produce under /mnt/session/outputs/."""

IMPLEMENT_DESCRIPTION = """Implement specification {spec_id} from /mnt/session/uploads/spec.json{spec_md_clause}.
{git}
{review_clause}
Write implementation_report.json{response_clause} to /mnt/session/outputs/ and validate it with the validate.py tool
(schema implementation_report.schema.json). The report must map every acceptance criterion to the tests that prove it, as
`criteria: [{{"ac_id": "AC-1", "tests": ["tests/test_x.py::test_name", ...]}}, ...]` with pytest node ids exactly as
`pytest --collect-only -q` prints them; the orchestrator runs the test plan itself, derives the passing set from its own
pytest run, and fails any automated criterion whose tests it did not collect or did not see pass. Run every command in the
spec's test_plan before finishing and record the results. {preview_clause}"""

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
        env = {**os.environ, "PORT": str(port), "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"}
        self.proc = subprocess.Popen(cmd, shell=True, cwd=self.repo, env=env, stdout=self.log.open("w"), stderr=subprocess.STDOUT,
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

    def _env(self) -> dict[str, str]:
        return {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"}

    def run_commands(self, repo: Path, commands: list[str], timeout: int = 1800) -> list[dict[str, Any]]:
        """Run the spec's test_plan commands in the checkout. Every pytest invocation inside a command also writes a
        junit report (appended through PYTEST_ADDOPTS, so `pytest`, `python -m pytest` and compound shell commands all
        get it); a command that produced one carries `pytest: {passed, failed, skipped}` with the node ids, which is
        where the orchestrator's passing-test set comes from (post-mortem issue 5: the set used to be copied from the
        implementer's own report, so any mapping passed, fabricated ids included)."""
        results = []
        junit_dir = self.workdir / "junit"
        junit_dir.mkdir(parents=True, exist_ok=True)
        base_env = self._env()
        for i, cmd in enumerate(commands):
            junit = junit_dir / f"cmd-{i}.xml"
            junit.unlink(missing_ok=True)
            addopts = f"{base_env.get('PYTEST_ADDOPTS', '')} --junitxml={shlex.quote(str(junit))} -o junit_family=xunit1".strip()
            env = {**base_env, "PYTEST_ADDOPTS": addopts}
            p = subprocess.run(cmd, shell=True, cwd=repo, env=env, capture_output=True, text=True, timeout=timeout)
            row: dict[str, Any] = {"command": cmd, "exit_code": p.returncode,
                                   "stdout_tail": p.stdout[-4000:], "stderr_tail": p.stderr[-4000:]}
            if junit.is_file():
                row["pytest"] = parse_junit(junit.read_bytes())
            results.append(row)
        return results

    def collect_test_ids(self, repo: Path, timeout: int = 300) -> list[str] | None:
        """Every pytest node id the checkout collects (`pytest --collect-only -q`), or None when pytest cannot collect
        here (not a pytest repository, or a collection error): the mapping is then unverifiable, not verified."""
        try:
            p = subprocess.run("python -m pytest --collect-only -q", shell=True, cwd=repo, env=self._env(),
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        if p.returncode not in (0, 5):          # 5: no tests collected, which is a (possibly empty) answer
            return None
        return [ln.strip() for ln in p.stdout.splitlines() if NODE_ID_RE.match(ln.strip())]

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
            env = {**os.environ, "BASE_URL": preview_url or ""}
            cmd = e["command"].replace("$BASE_URL", preview_url or "")
            try:
                p = subprocess.run(cmd, shell=True, cwd=repo or self.workdir, env=env, capture_output=True,
                                   text=True, timeout=timeout)
                results.append({**row, "exit_code": p.returncode, "stdout_tail": p.stdout[-4000:],
                                "stderr_tail": p.stderr[-4000:]})
            except subprocess.TimeoutExpired:
                results.append({**row, "exit_code": 124, "stderr_tail": f"timed out after {timeout}s"})
        return results


NODE_ID_RE = re.compile(r"^[^\s:]+::\S+$")


def parse_junit(xml: bytes) -> dict[str, list[str]]:
    """Node ids by outcome from a pytest junit report (junit_family=xunit1, which keeps the `file` attribute).

    A node id is `file::Class::name`: `file` is relative to pytest's rootdir (the checkout), the class chain is what
    `classname` adds after the module's dotted path, `name` keeps the parametrisation suffix."""
    out: dict[str, list[str]] = {"passed": [], "failed": [], "skipped": []}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    for tc in root.iter("testcase"):
        node_id = _node_id(tc.get("file"), tc.get("classname", ""), tc.get("name", ""))
        tags = {child.tag for child in tc}
        if tags & {"failure", "error"}:
            out["failed"].append(node_id)
        elif "skipped" in tags:
            out["skipped"].append(node_id)
        else:
            out["passed"].append(node_id)
    return out


def _node_id(file: str | None, classname: str, name: str) -> str:
    if not file:
        parts = [p for p in classname.split(".") if p]
        return f"{'/'.join(parts)}.py::{name}" if parts else name
    module = Path(file).with_suffix("").as_posix().replace("/", ".")
    if classname == module or not classname:
        chain: list[str] = []
    elif classname.startswith(module + "."):
        chain = classname[len(module) + 1:].split(".")
    else:                                   # rootdir or package layout differs: everything after the module's last component
        parts = classname.split(".")
        stem = Path(file).stem
        chain = parts[len(parts) - parts[::-1].index(stem):] if stem in parts else []
    return "::".join([file, *chain, name])


def passed_test_ids(command_results: list[dict[str, Any]]) -> tuple[list[str], list[str]] | None:
    """(passed, failed) node ids across every command that ran pytest, or None when none did."""
    runs = [r["pytest"] for r in command_results if r.get("pytest")]
    if not runs:
        return None
    passed = sorted({t for r in runs for t in r.get("passed", [])})
    failed = sorted({t for r in runs for t in r.get("failed", [])})
    return [t for t in passed if t not in set(failed)], failed


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
                      verifier: LocalVerifier, dependency_specs: dict[str, dict], *, spec_md: str = "",
                      resume_from: ResumePoint | None = None) -> dict[str, Any]:
    """Drive one spec through implement/review/judge. Returns the final merge_readiness verdict.

    `spec_md` is the writer's spec.md for this spec (the runner carries it from Gate 2); the implementer, every
    reviewer pass and the merge judge receive it beside spec.json. `resume_from` continues the loop after a dead
    orchestrator (see `ResumePoint`); the work item then already sits in implementing or responding, so no stage
    event is emitted for the restart."""
    sid, wid = spec["spec_id"], spec["work_item_id"]
    spec_md = (spec_md or spec.get("_markdown") or "").strip()
    branch = _branch_for(ctx.run_id, sid)
    git, git_ro = git_clauses(ctx, repo_url, base_branch, branch)     # the implementer pushes; everyone else only reads

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
        uploads = {"spec.json": json.dumps(public(spec), indent=2).encode()}
        if spec_md:
            uploads["spec.md"] = spec_md.encode()
        for dsid, ds in dependency_specs.items():
            uploads[f"dependency_specs/{dsid}.json"] = json.dumps(public(ds), indent=2).encode()
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
            res = run_session(
                ctx, prefix=f"{sid}-r{round_no}-", spec_id=sid,
                agent_key="implementer", title=f"{ctx.run_id} implement {sid} r{round_no}", run_id=ctx.run_id,
                stage="implement", uploads=uploads, work_item_id=wid,
                description=IMPLEMENT_DESCRIPTION.format(spec_id=sid, git=git, review_clause=review_clause,
                                                         spec_md_clause=" (spec.md is the readable form)" if spec_md else "",
                                                         response_clause=response_clause, preview_clause=preview_clause),
                rubric_md=ctx.rubric("implementation"), max_iterations=ctx.policy.outcome_max_iterations,
                budget_usd=ctx.policy.stage_budget_usd["implement"],
                repository=mount(base_branch if round_no == 1 else branch),
            )
            if "blocked.md" in res.outputs:
                persist_outputs(ctx, "implement", res, prefix=f"{sid}-r{round_no}-", work_item_id=wid, spec_id=sid)
                ctx.event("work_item.stage", {"stage": WorkItemStage.BLOCKED.value}, work_item_id=wid, spec_id=sid)
                return {"result": "escalate", "reason": res.outputs["blocked.md"].decode(errors="replace")}
            report = require_json(res, "implementation_report.json")
            stamp_provenance(report, ctx, res, "Implementer", "claude-opus-5-5")
            res.outputs["implementation_report.json"] = json.dumps(report, indent=2).encode()
            if findings:
                response = require_json(res, "review_response.json")
                stamp_provenance(response, ctx, res, "Implementer", "claude-opus-5-5")
                res.outputs["review_response.json"] = json.dumps(response, indent=2).encode()
            persist_outputs(ctx, "implement", res, prefix=f"{sid}-r{round_no}-", work_item_id=wid, spec_id=sid)
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
        # The passing set is the orchestrator's own: node ids pytest reported passed in the test_plan run, checked against
        # what pytest collects in the checkout. A repository pytest cannot collect leaves the mapping unverified (said so
        # in the check's detail), never silently verified.
        collected = verifier.collect_test_ids(repo)
        pytest_runs = passed_test_ids(cmd_results)
        passing_tests = (pytest_runs[0] if pytest_runs else []) if collected is not None else None
        det = checks.run_all_for_merge(
            spec=spec, changed_files=diff["changed"], deleted_files=diff["deleted"], existing_test_files=diff["existing_tests"],
            added_lines_by_file=diff["added_lines"], implementation_report=report, passing_tests=passing_tests,
            tests_that_exist=collected, command_results=cmd_results, manifest_path=repo / ctx.cfg.service_manifest_path,
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
        rev_uploads = {"spec.json": uploads["spec.json"], **({"spec.md": uploads["spec.md"]} if "spec.md" in uploads else {}),
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
            r = run_session(
                ctx, prefix=f"{sid}-r{round_no}-p{i}-", spec_id=sid,
                agent_key="reviewer", title=f"{ctx.run_id} review {sid} r{round_no} p{i}", run_id=ctx.run_id,
                stage="review", uploads=rev_uploads, work_item_id=wid,
                description=REVIEW_DESCRIPTION.format(spec_id=sid, base_sha=base_sha, head_sha=head_sha, branch=branch,
                                                      git=git_ro, round=round_no, second_round=second,
                                                      preview_clause=rev_preview_clause),
                rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["review"],
                metadata={"pass": str(i)}, repository=mount(branch),
            )
            f = require_json(r, "findings.json")
            stamp_provenance(f, ctx, r, "Adversarial reviewer", "claude-opus-5-5")
            r.outputs["findings.json"] = json.dumps(f, indent=2).encode()
            persist_outputs(ctx, "review", r, prefix=f"{sid}-r{round_no}-p{i}-", work_item_id=wid, spec_id=sid)
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
            r = run_session(
                ctx, prefix=f"{sid}-r{round_no}-sec-", spec_id=sid,
                agent_key="security_reviewer", title=f"{ctx.run_id} security-review {sid} r{round_no}", run_id=ctx.run_id,
                stage="review", uploads={**rev_uploads, "security_prechecks.json": json.dumps(pre, indent=2).encode()}, work_item_id=wid,
                description=SECURITY_REVIEW_DESCRIPTION.format(spec_id=sid, base_sha=base_sha, head_sha=head_sha, branch=branch,
                                                               git=git_ro, round=round_no, second_round=second,
                                                               preview_clause=rev_preview_clause),
                rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["security_review"],
                metadata={"pass": "security"}, repository=mount(branch),
            )
            f = require_json(r, "findings.json")
            stamp_provenance(f, ctx, r, "Security reviewer", "claude-opus-5-5")
            r.outputs["findings.json"] = json.dumps(f, indent=2).encode()
            persist_outputs(ctx, "review", r, prefix=f"{sid}-r{round_no}-sec-", work_item_id=wid, spec_id=sid)
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
        "spec.json": json.dumps(public(spec), indent=2).encode(), "diff.patch": diff["patch"].encode(),
        **({"spec.md": spec_md.encode()} if spec_md else {}),
        "findings.json": json.dumps(findings or {"findings": []}, indent=2).encode(),
        "review_response.json": json.dumps(response or {"responses": []}, indent=2).encode(),
        "test_results.json": json.dumps({"commands": cmd_results, "e2e": e2e_results, "preview": preview}, indent=2).encode(),
        "deterministic_checks.json": json.dumps([d.to_dict() for d in det], indent=2).encode(),
    }
    # The judge sees the real round (the implementer round whose head it judges; the kickoff and merge_readiness.json
    # used to say "round 1" whatever happened), and its verdict is kept per round.
    prior_verdict = artifact_json_or_none(ctx, "merge_judge", f"{sid}-r{round_no}-merge_readiness.json") if resume_from else None
    if prior_verdict is not None and prior_verdict.get("_head_sha") == head_sha:
        ctx.event("merge.verdict", {"result": prior_verdict["result"], "round": round_no, "reused": True}, work_item_id=wid, spec_id=sid, actor="agent")
        return prior_verdict      # judged this very head before the orchestrator died
    res = run_session(
        ctx, prefix=f"{sid}-r{round_no}-", spec_id=sid,
        agent_key="merge_judge", title=f"{ctx.run_id} merge-judge {sid} r{round_no}", run_id=ctx.run_id, stage="merge_judge",
        uploads=mj_uploads, work_item_id=wid,
        description=MERGE_JUDGE_DESCRIPTION.format(spec_id=sid, round=round_no, git=git_ro),
        rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["merge_judge"],
        repository=mount(branch),
    )
    verdict = require_json(res, "merge_readiness.json")
    verdict["round"] = round_no
    # Deterministic checks are not negotiable: a failing check forces another_round or escalate.
    if any(not d.passed for d in det) and verdict["result"] == "ready":
        verdict["result"] = "another_round"
        verdict["summary_for_human"] = "Overridden: deterministic checks failed. " + verdict["summary_for_human"]
    verdict["deterministic_checks"] = [d.to_dict() for d in det]
    stamp_provenance(verdict, ctx, res, "Merge judge", "claude-opus-5-5")
    verdict["_head_sha"], verdict["_branch"], verdict["_base_sha"] = head_sha, branch, base_sha
    res.outputs["merge_readiness.json"] = json.dumps(verdict, indent=2).encode()
    persist_outputs(ctx, "merge_judge", res, prefix=f"{sid}-r{round_no}-", work_item_id=wid, spec_id=sid)
    ctx.event("merge.verdict", {"result": verdict["result"], "round": round_no}, work_item_id=wid, spec_id=sid, actor="agent")
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
            "suggested_fix": {"ac_test_mapping": "Map every automated acceptance criterion to a test in implementation_report.json `criteria` ([{ac_id, tests: [...]}]) using pytest node ids exactly as `pytest --collect-only -q` prints them; the orchestrator only counts tests it collected and saw pass in its own run of the test plan.",
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
