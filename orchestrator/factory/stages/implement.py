"""Stage 3, per spec: implement -> adversarial review -> respond -> fresh review -> merge judge.

Each box is its own session with its own context. The reviewer in round 2 is a *new*
session that receives the new diff plus the implementer's responses; it is not a
continuation of round 1 (one fresh pass per round; Song, arXiv:2603.16244). The merge
judge receives the orchestrator's deterministic checks and may not override them.
"""
from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .. import checks
from ..state import WorkItemStage
from . import RunContext
from .common import persist_outputs, provenance, require_json

GIT_SETUP = """Git access: the repository is NOT pre-mounted. Clone it with
  git -c http.extraHeader="Authorization: Bearer $GITLAB_TOKEN" clone --branch {base_branch} {repo_url} /workspace/repo
and configure the same header for push: `git config http.extraHeader "Authorization: Bearer $GITLAB_TOKEN"`.
$GITLAB_TOKEN is an opaque placeholder; the real token is substituted outside the sandbox. Work on branch {branch}
(create it from {base_branch} if it does not exist) and push it when done."""

IMPLEMENT_DESCRIPTION = """Implement specification {spec_id} from /mnt/session/uploads/spec.json (spec.md is the readable form).
{git}
{review_clause}
Write implementation_report.json{response_clause} to /mnt/session/outputs/. Run every command in the spec's test_plan
before finishing and record the results."""

REVIEW_DESCRIPTION = """Review the change for specification {spec_id}. The diff is at /mnt/session/uploads/diff.patch
(base {base_sha}, head {head_sha}); the repository is cloned at /workspace/repo on branch {branch}.
{git}
This is review round {round}.{second_round}
Write findings.json to /mnt/session/outputs/ and validate it. You never edit source files."""

MERGE_JUDGE_DESCRIPTION = """Decide merge readiness for specification {spec_id}, round {round}.
Inputs are under /mnt/session/uploads/: spec.json, diff.patch, findings.json, review_response.json,
test_results.json (authoritative; run by the orchestrator), deterministic_checks.json (you may not override these).
{git}
Write merge_readiness.json to /mnt/session/outputs/ and validate it."""


def _git(repo_dir: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo_dir, check=True, capture_output=True, text=True).stdout


class LocalVerifier:
    """Orchestrator-side clone used for the authoritative test run and diff analysis.

    Runs the spec's test commands in a clean checkout of head_sha. In production this is a
    CI job (GitLab pipeline) triggered on the branch; the interface is the same.
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
        for cmd in commands:
            p = subprocess.run(cmd, shell=True, cwd=repo, capture_output=True, text=True, timeout=timeout)
            results.append({"command": cmd, "exit_code": p.returncode,
                            "stdout_tail": p.stdout[-4000:], "stderr_tail": p.stderr[-4000:]})
        return results


def _branch_for(run_id: str, spec_id: str) -> str:
    return f"factory/{run_id}/{spec_id}"


def run_implement_one(ctx: RunContext, spec: dict[str, Any], repo_url: str, base_branch: str,
                      verifier: LocalVerifier, dependency_specs: dict[str, dict]) -> dict[str, Any]:
    """Drive one spec through implement/review/judge. Returns the final merge_readiness verdict."""
    sid, wid = spec["spec_id"], spec["work_item_id"]
    branch = _branch_for(ctx.run_id, sid)
    git = GIT_SETUP.format(base_branch=base_branch, repo_url=repo_url, branch=branch)
    ctx.event("work_item.stage", {"stage": WorkItemStage.IMPLEMENTING.value, "branch": branch}, work_item_id=wid, spec_id=sid)

    findings: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    verdict: dict[str, Any] | None = None
    base_sha = _git(verifier.checkout(base_branch), "rev-parse", "HEAD").strip()

    for round_no in range(1, ctx.policy.review_max_rounds + 2):
        # ---- implementer (round 1 implements; later rounds respond to findings)
        uploads = {"spec.json": json.dumps(spec, indent=2).encode(), "spec.md": spec.get("_markdown", "").encode()}
        for dsid, ds in dependency_specs.items():
            uploads[f"dependency_specs/{dsid}.json"] = json.dumps(ds, indent=2).encode()
        review_clause, response_clause = "", ""
        if findings:
            uploads["findings.json"] = json.dumps(findings, indent=2).encode()
            review_clause = ("Reviewer findings are at /mnt/session/uploads/findings.json. Respond to every finding "
                             "(fixed / refuted with evidence / accepted_risk) and push the updated branch.")
            response_clause = " and review_response.json"
        res = ctx.driver.run(
            agent_key="implementer", title=f"{ctx.run_id} implement {sid} r{round_no}", run_id=ctx.run_id,
            stage="implement", uploads=uploads, work_item_id=wid,
            description=IMPLEMENT_DESCRIPTION.format(spec_id=sid, git=git, review_clause=review_clause,
                                                     response_clause=response_clause),
            rubric_md=ctx.rubric("implementation"), max_iterations=ctx.policy.outcome_max_iterations,
            budget_usd=ctx.policy.stage_budget_usd["implement"],
        )
        if "blocked.md" in res.outputs:
            persist_outputs(ctx, "implement", res, prefix=f"{sid}-r{round_no}-")
            ctx.event("work_item.stage", {"stage": WorkItemStage.BLOCKED.value}, work_item_id=wid, spec_id=sid)
            return {"result": "escalate", "reason": res.outputs["blocked.md"].decode(errors="replace")}
        report = require_json(res, "implementation_report.json")
        if findings:
            response = require_json(res, "review_response.json")
            response["round"] = round_no - 1
            ctx.event("review.response", {"round": round_no - 1, "dispositions": [r["disposition"] for r in response["responses"]]},
                      work_item_id=wid, spec_id=sid, actor="agent")
        persist_outputs(ctx, "implement", res, prefix=f"{sid}-r{round_no}-")

        # ---- authoritative verification (orchestrator side)
        repo = verifier.checkout(branch)
        head_sha = _git(repo, "rev-parse", "HEAD").strip()
        diff = verifier.analyse(repo, base_sha, head_sha)
        cmd_results = verifier.run_commands(repo, spec["test_plan"]["commands"])
        passing_tests = [t for m in report.get("criteria_to_tests", []) for t in m.get("tests", [])]  # refined by CI parser
        det = checks.run_all_for_merge(
            spec=spec, changed_files=diff["changed"], deleted_files=diff["deleted"], existing_test_files=diff["existing_tests"],
            added_lines_by_file=diff["added_lines"], implementation_report=report, passing_tests=passing_tests,
            command_results=cmd_results,
        )
        ctx.store.put_artifact(ctx.run_id, "implement", f"{sid}-r{round_no}-deterministic_checks.json",
                               json.dumps([d.to_dict() for d in det], indent=2).encode())
        ctx.store.put_artifact(ctx.run_id, "implement", f"{sid}-r{round_no}-test_results.json",
                               json.dumps(cmd_results, indent=2).encode())

        if round_no > ctx.policy.review_max_rounds:
            break  # last implementer response done; go straight to the merge judge

        # ---- fresh-context adversarial review (N independent passes, merged)
        ctx.event("work_item.stage", {"stage": WorkItemStage.REVIEWING.value, "review_round": round_no}, work_item_id=wid, spec_id=sid)
        rev_uploads = {"spec.json": uploads["spec.json"], "spec.md": uploads["spec.md"],
                       "diff.patch": diff["patch"].encode(),
                       "implementation_report.json": json.dumps(report, indent=2).encode()}
        second = ""
        if findings and response:
            rev_uploads["previous_findings.json"] = json.dumps(findings, indent=2).encode()
            rev_uploads["review_response.json"] = json.dumps(response, indent=2).encode()
            second = (" Judge each previous finding against your refutation condition using review_response.json "
                      "(upheld/withdrawn), then review the new hunks.")

        def one_pass(i: int) -> dict[str, Any]:
            r = ctx.driver.run(
                agent_key="reviewer", title=f"{ctx.run_id} review {sid} r{round_no} p{i}", run_id=ctx.run_id,
                stage="review", uploads=rev_uploads, work_item_id=wid,
                description=REVIEW_DESCRIPTION.format(spec_id=sid, base_sha=base_sha, head_sha=head_sha, branch=branch,
                                                      git=git, round=round_no, second_round=second),
                rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["review"],
                metadata={"pass": str(i)},
            )
            f = require_json(r, "findings.json")
            persist_outputs(ctx, "review", r, prefix=f"{sid}-r{round_no}-p{i}-")
            return f

        with ThreadPoolExecutor(max_workers=ctx.policy.reviewer_passes) as pool:
            passes = list(pool.map(one_pass, range(1, ctx.policy.reviewer_passes + 1)))
        findings = merge_findings(passes, sid, round_no, base_sha, head_sha, branch)
        ctx.store.put_artifact(ctx.run_id, "review", f"{sid}-r{round_no}-findings.merged.json",
                               json.dumps(findings, indent=2).encode())
        ctx.event("review.findings", {"round": round_no, "count": len(findings["findings"]),
                                      "blocking": sum(f["severity"] == "blocking" for f in findings["findings"])},
                  work_item_id=wid, spec_id=sid, actor="agent")
        standing = [f for f in findings["findings"] if f["status"] in ("open", "upheld") and f["severity"] in ("blocking", "major")]
        if not standing and all(d.passed for d in det):
            break  # nothing standing; skip the implementer round and go to the judge
        ctx.event("work_item.stage", {"stage": WorkItemStage.RESPONDING.value}, work_item_id=wid, spec_id=sid)

    # ---- merge judge (independent)
    ctx.event("work_item.stage", {"stage": WorkItemStage.MERGE_JUDGING.value}, work_item_id=wid, spec_id=sid)
    mj_uploads = {
        "spec.json": json.dumps(spec, indent=2).encode(), "diff.patch": diff["patch"].encode(),
        "findings.json": json.dumps(findings or {"findings": []}, indent=2).encode(),
        "review_response.json": json.dumps(response or {"responses": []}, indent=2).encode(),
        "test_results.json": json.dumps(cmd_results, indent=2).encode(),
        "deterministic_checks.json": json.dumps([d.to_dict() for d in det], indent=2).encode(),
    }
    res = ctx.driver.run(
        agent_key="merge_judge", title=f"{ctx.run_id} merge-judge {sid}", run_id=ctx.run_id, stage="merge_judge",
        uploads=mj_uploads, work_item_id=wid,
        description=MERGE_JUDGE_DESCRIPTION.format(spec_id=sid, round=len([1 for _ in range(1)]), git=git),
        rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["merge_judge"],
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


def _sev(f: dict[str, Any]) -> int:
    return {"nit": 0, "minor": 1, "major": 2, "blocking": 3}[f["severity"]]


def _status_rank(s: str) -> int:
    return {"met": 0, "not_assessed": 1, "untestable_as_written": 2, "not_met": 3}[s]
