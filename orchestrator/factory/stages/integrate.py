"""Open a reviewed change request; keep the approved record in Factory's artifact store."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import quote
from typing import Any

from ..budget import reserve_budget
from ..forge import Forge
from ..sessions import SessionContext
from ..state import WorkItemStage
from . import RunContext
from .common import enter_item_stage, persist_outputs, platform_skill, run_session

INTEGRATE_DESCRIPTION = """Open the change request (pull request or merge request) for specification {spec_id}.
Inputs are under /mnt/session/uploads/ (spec.json, merge_readiness.json, findings.json, review_response.json,
test_results.json, run_summary.json, cr_template.md). The branch {branch} is already pushed; its head must equal
{head_sha} (the exact head verified and approved by the merge judge).
{git}
Factory records remain in the external artifact store: use run_summary.json artifact_links in the PR description.
Do not write or commit .factory files to the target repository.
Render the supplied current template, call open_change_request, and write cr.json to /mnt/session/outputs/. The verification was run in
preview mode "{preview_mode}" (run_summary.json `preview_mode`; test_results.json `environment` and `preview` say exactly
where and how). {skill_clause}"""


def artifact_links(ctx: RunContext, spec_id: str) -> dict[str, str]:
    base = f"{ctx.cfg.artifact_base_url}/api/artifacts/{quote(ctx.run_id, safe='')}"
    return {"spec_json": f"{base}/specify/final-{quote(spec_id, safe='')}.json",
            "spec_md": f"{base}/specify/final-{quote(spec_id, safe='')}.md",
            "verdict": f"{base}/judge/final-verdict.json",
            "merge_readiness": f"{base}/integrate/{quote(spec_id, safe='')}-approved-merge_readiness.json"}


def make_open_change_request(ctx: RunContext, forge: Forge, spec_id: str, work_item_id: str, expected_branch: str,
                             expected_target: str):
    def handler(tool_input: dict[str, Any], sctx: SessionContext) -> str:
        if tool_input["source_branch"] != expected_branch:
            return json.dumps({"error": f"source_branch must be {expected_branch}"})
        if tool_input["target_branch"] != expected_target:
            return json.dumps({"error": f"target_branch must be {expected_target}"})
        evaluation = ctx.evaluation_target
        title = tool_input["title"]
        description = tool_input["description_markdown"]
        if evaluation:
            title = "[Factory evaluation] " + title
            description = (f"Factory pipeline evaluation against original baseline `{evaluation[1]}` "
                           f"on `{expected_target}`. The implementation is already merged in the normal default branch. "
                           "This draft preserves the complete diff for evaluation; do not merge it.\n\n" + description)
        cr = forge.create_change_request(
            source_branch=tool_input["source_branch"], target_branch=tool_input["target_branch"],
            title=title[:120], description=description,
            labels=sorted(set(tool_input.get("labels", []) + ["factory", f"spec:{spec_id}", f"run:{ctx.run_id}"])),
            reviewers=tool_input.get("reviewers", []), draft=bool(evaluation or tool_input.get("draft", False)),
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

    # No integration commit: the PR must contain the exact candidate the judge reviewed.
    head_sha = judged_sha
    if verifier is not None:
        repo = verifier.checkout(branch)
        if _git(repo, "rev-parse", "HEAD").strip() != judged_sha:
            raise RuntimeError("implementation branch changed after the merge judgement; review the new head first")
        if ctx.evaluation_target:
            base = verifier.checkout(base_branch)
            if _git(base, "rev-parse", "HEAD").strip() != ctx.evaluation_target[1]:
                raise RuntimeError("evaluation comparison branch moved after review")
            verifier.checkout(branch)
    ctx.store.put_artifact(ctx.run_id, "integrate", f"{sid}-approved-merge_readiness.json",
                           json.dumps(verdict, indent=2).encode())

    uploads = {
        "spec.json": json.dumps(spec, indent=2).encode(),
        "merge_readiness.json": json.dumps(verdict, indent=2).encode(),
        "findings.json": json.dumps(findings, indent=2).encode(),
        "review_response.json": json.dumps(response, indent=2).encode(),
        "test_results.json": json.dumps(test_results, indent=2).encode(),
        "run_summary.json": json.dumps({**run_summary, "target_branch": base_branch, "forge": ctx.forge.kind,
                                        "preview_mode": ctx.cfg.preview_mode,
                                        "artifact_links": artifact_links(ctx, sid),
                                        "evaluation_target": ctx.evaluation_target}, indent=2).encode(),
        "cr_template.md": (ctx.cfg.templates_dir / "cr_template.md").read_bytes(),
    }
    skill_uploads, skill_clause = platform_skill(ctx)
    reserve_budget(ctx, "integrate")
    res = run_session(
        ctx, prefix=f"{sid}-", spec_id=sid,
        agent_key="integrator", title=f"{ctx.run_id} integrate {sid}", run_id=ctx.run_id, stage="integrate",
        uploads={**uploads, **skill_uploads}, work_item_id=wid,
        description=INTEGRATE_DESCRIPTION.format(spec_id=sid, branch=branch, head_sha=head_sha,
                                                 git=ctx.forge.sandbox_git_instructions(ctx.cfg.repo_url, base_branch, branch, read_only=True),
                                                 preview_mode=ctx.cfg.preview_mode, skill_clause=skill_clause),
        rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["integrate"],
        custom_tools={"open_change_request": make_open_change_request(ctx, ctx.forge, sid, wid, branch, base_branch)},
        repository=ctx.forge.session_repository_resource(ctx.cfg.repo_url, branch),
    )
    # The forge result, not an agent-authored cr.json, proves that a PR exists.
    opened = next((ev.payload for ev in reversed(ctx.store.events(ctx.run_id))
                   if ev.kind == "cr.opened" and ev.spec_id == sid and ev.payload.get("branch") == branch), None)
    if opened is None:
        persist_outputs(ctx, "integrate", res, prefix=f"{sid}-unconfirmed-{res.session_id}-", work_item_id=wid, spec_id=sid)
        raise RuntimeError(f"integrator session {res.session_id} did not open a change request for {sid}")
    cr = {"id": opened["id"], "url": opened["url"], "web_url": opened["url"], "iid": opened["id"]}
    res.outputs["cr.json"] = json.dumps(cr, indent=2).encode()
    persist_outputs(ctx, "integrate", res, prefix=f"{sid}-", work_item_id=wid, spec_id=sid)
    ctx.event("work_item.stage", {"stage": WorkItemStage.CR_OPEN.value, "cr_id": cr.get("id", cr.get("iid"))},
              work_item_id=wid, spec_id=sid)
    return cr


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout
