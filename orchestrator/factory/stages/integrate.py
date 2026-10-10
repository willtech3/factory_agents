"""Stage 4: record the approved design on the branch, then open the change request.

Two steps, in order:

1. **Run record** (deterministic, orchestrator side, no agent). The approved `spec.json`, `spec.md`,
   the spec judge's `verdict.json`, and the merge judge's `merge_readiness.json` are committed under
   `.factory/runs/<run_id>/` on the implementation branch from the orchestrator's own checkout, with a
   fixed author and timestamp so the commit sha is a pure function of the branch head and the files.
   The reviewer at Gate 3 therefore sees the contract next to the code, whatever the gate surface.
2. **Change request** (PR or MR). The `integrator` session renders `templates/cr_template.md` and calls
   the `open_change_request` custom tool; the orchestrator executes it with its own forge token.
   Gate 3 is the forge's own review and merge.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from ..budget import reserve_budget
from ..forge import Forge
from ..sessions import SessionContext
from ..state import WorkItemStage
from . import RunContext
from .common import enter_item_stage, persist_outputs, require_json

INTEGRATE_DESCRIPTION = """Open the change request (pull request or merge request) for specification {spec_id}.
Inputs are under /mnt/session/uploads/ (spec.json, merge_readiness.json, findings.json, review_response.json,
test_results.json, run_summary.json, cr_template.md). The branch {branch} is already pushed; its head must equal
{head_sha} (the orchestrator's run-record commit on top of the judged head {judged_sha}). Render the template, call
open_change_request, and write cr.json to /mnt/session/outputs/."""

RECORD_AUTHOR = ("factory", "factory@localhost")


def run_record_paths(run_id: str, spec_id: str) -> dict[str, str]:
    base = f".factory/runs/{run_id}"
    return {"spec_json": f"{base}/specs/{spec_id}.json", "spec_md": f"{base}/specs/{spec_id}.md",
            "verdict": f"{base}/verdict.json", "merge_readiness": f"{base}/evidence/{spec_id}/merge_readiness.json"}


def commit_run_record(repo: Path, run_id: str, spec: dict[str, Any], spec_md: str, spec_verdict: dict[str, Any] | None,
                      merge_verdict: dict[str, Any], *, push: bool = True, remote: str = "origin") -> str:
    """Write the record files into `repo` (already checked out on the implementation branch), commit them with a
    fixed identity and date, optionally push, and return the new head sha. Returns the current head unchanged when
    the files are already identical (re-runs are idempotent)."""
    sid = spec["spec_id"]
    paths = run_record_paths(run_id, sid)
    public = {k: v for k, v in merge_verdict.items() if not k.startswith("_")}
    files = {paths["spec_json"]: json.dumps(spec, indent=2, sort_keys=True) + "\n",
             paths["spec_md"]: spec_md if spec_md.endswith("\n") or not spec_md else spec_md + "\n",
             paths["merge_readiness"]: json.dumps(public, indent=2, sort_keys=True) + "\n"}
    if spec_verdict is not None:
        files[paths["verdict"]] = json.dumps(spec_verdict, indent=2, sort_keys=True) + "\n"
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _git(repo, "add", "-A", "--", ".factory")
    if not _git(repo, "status", "--porcelain", "--", ".factory").strip():
        return _git(repo, "rev-parse", "HEAD").strip()
    when = (merge_verdict.get("produced_by") or {}).get("produced_at") or "2026-01-01T00:00:00+00:00"
    env = {**os.environ, "GIT_AUTHOR_NAME": RECORD_AUTHOR[0], "GIT_AUTHOR_EMAIL": RECORD_AUTHOR[1],
           "GIT_COMMITTER_NAME": RECORD_AUTHOR[0], "GIT_COMMITTER_EMAIL": RECORD_AUTHOR[1],
           "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    msg = f"factory({run_id}): record approved spec and verdicts for {sid}\n\nJudged head: {merge_verdict.get('_head_sha', '')}"
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo, env=env, check=True, capture_output=True, text=True)
    if push:
        _git(repo, "push", "-q", remote, f"HEAD:{merge_verdict.get('_branch') or _git(repo, 'rev-parse', '--abbrev-ref', 'HEAD').strip()}")
    return _git(repo, "rev-parse", "HEAD").strip()


def make_open_change_request(ctx: RunContext, forge: Forge, spec_id: str, work_item_id: str, expected_branch: str):
    def handler(tool_input: dict[str, Any], sctx: SessionContext) -> str:
        if tool_input["source_branch"] != expected_branch:
            return json.dumps({"error": f"source_branch must be {expected_branch}"})
        cr = forge.create_change_request(
            source_branch=tool_input["source_branch"], target_branch=tool_input["target_branch"],
            title=tool_input["title"][:120], description=tool_input["description_markdown"],
            labels=sorted(set(tool_input.get("labels", []) + ["factory", f"spec:{spec_id}", f"run:{ctx.run_id}"])),
            reviewers=tool_input.get("reviewers", []), draft=bool(tool_input.get("draft", False)),
        )
        ctx.event("cr.opened", {"id": cr.id, "url": cr.url, "branch": cr.source_branch, "forge": forge.kind},
                  work_item_id=work_item_id, spec_id=spec_id)
        ctx.notifier.change_request_opened(ctx.run_id, spec_id, cr.url)
        return json.dumps({"id": cr.id, "url": cr.url, "web_url": cr.url, "iid": cr.id})
    return handler


def run_integrate(ctx: RunContext, spec: dict[str, Any], verdict: dict[str, Any], findings: dict[str, Any],
                  response: dict[str, Any], test_results: Any, run_summary: dict[str, Any],
                  base_branch: str, *, verifier: Any | None = None, spec_md: str = "",
                  spec_verdict: dict[str, Any] | None = None) -> dict[str, Any]:
    assert ctx.forge is not None, "a forge is required to open change requests"
    sid, wid = spec["spec_id"], spec["work_item_id"]
    branch, judged_sha = verdict["_branch"], verdict["_head_sha"]
    enter_item_stage(ctx, wid, sid, WorkItemStage.INTEGRATING)

    head_sha = judged_sha
    if verifier is not None:
        repo = verifier.checkout(branch)
        head_sha = commit_run_record(repo, ctx.run_id, spec, spec_md, spec_verdict, verdict)
        if head_sha != judged_sha:
            ctx.event("artifact.produced", {"artifact_type": "run_record", "path": f".factory/runs/{ctx.run_id}/",
                                            "sha256": None, "commit": head_sha, "judged_head": judged_sha},
                      work_item_id=wid, spec_id=sid, stage="integrate")
    verdict = {**verdict, "_head_sha": head_sha, "_judged_head_sha": judged_sha}

    uploads = {
        "spec.json": json.dumps(spec, indent=2).encode(),
        "merge_readiness.json": json.dumps(verdict, indent=2).encode(),
        "findings.json": json.dumps(findings, indent=2).encode(),
        "review_response.json": json.dumps(response, indent=2).encode(),
        "test_results.json": json.dumps(test_results, indent=2).encode(),
        "run_summary.json": json.dumps({**run_summary, "target_branch": base_branch, "forge": ctx.forge.kind,
                                        "run_record_paths": run_record_paths(ctx.run_id, sid)}, indent=2).encode(),
        "cr_template.md": (ctx.cfg.templates_dir / "cr_template.md").read_bytes(),
    }
    reserve_budget(ctx, "integrate")
    res = ctx.driver.run(
        agent_key="integrator", title=f"{ctx.run_id} integrate {sid}", run_id=ctx.run_id, stage="integrate",
        uploads=uploads, work_item_id=wid,
        description=INTEGRATE_DESCRIPTION.format(spec_id=sid, branch=branch, head_sha=head_sha, judged_sha=judged_sha),
        rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["integrate"],
        custom_tools={"open_change_request": make_open_change_request(ctx, ctx.forge, sid, wid, branch)},
        repository=ctx.forge.session_repository_resource(ctx.cfg.repo_url, branch),
    )
    cr = require_json(res, "cr.json")
    persist_outputs(ctx, "integrate", res, prefix=f"{sid}-")
    ctx.event("work_item.stage", {"stage": WorkItemStage.CR_OPEN.value, "cr_id": cr.get("id", cr.get("iid"))},
              work_item_id=wid, spec_id=sid)
    return cr


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout
