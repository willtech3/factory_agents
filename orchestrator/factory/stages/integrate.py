"""Stage 4: open the GitLab merge request (Gate 3 is GitLab's own review and approval)."""
from __future__ import annotations

import json
from typing import Any

from ..gitlab import GitLabClient
from ..sessions import SessionContext
from ..state import WorkItemStage
from . import RunContext
from .common import persist_outputs, require_json

INTEGRATE_DESCRIPTION = """Open the merge request for specification {spec_id}. Inputs are under /mnt/session/uploads/
(spec.json, merge_readiness.json, findings.json, review_response.json, test_results.json, run_summary.json, mr_template.md).
The branch {branch} is already pushed; its head must equal {head_sha}. Render the template, call open_merge_request,
and write mr.json to /mnt/session/outputs/."""


def make_open_mr(ctx: RunContext, gitlab: GitLabClient, spec_id: str, work_item_id: str, expected_branch: str):
    def handler(tool_input: dict[str, Any], sctx: SessionContext) -> str:
        if tool_input["source_branch"] != expected_branch:
            return json.dumps({"error": f"source_branch must be {expected_branch}"})
        mr = gitlab.create_mr(
            source_branch=tool_input["source_branch"], target_branch=tool_input["target_branch"],
            title=tool_input["title"][:120], description=tool_input["description_markdown"],
            labels=sorted(set(tool_input.get("labels", []) + ["factory", f"spec:{spec_id}", f"run:{ctx.run_id}"])),
            reviewer_usernames=tool_input.get("reviewers", []), draft=bool(tool_input.get("draft", False)),
        )
        ctx.event("mr.opened", {"iid": mr.iid, "url": mr.web_url, "branch": mr.source_branch},
                  work_item_id=work_item_id, spec_id=spec_id)
        ctx.notifier.mr_opened(ctx.run_id, spec_id, mr.web_url)
        return json.dumps({"iid": mr.iid, "web_url": mr.web_url})
    return handler


def run_integrate(ctx: RunContext, spec: dict[str, Any], verdict: dict[str, Any], findings: dict[str, Any],
                  response: dict[str, Any], test_results: list[dict[str, Any]], run_summary: dict[str, Any],
                  base_branch: str) -> dict[str, Any]:
    assert ctx.gitlab is not None, "GitLab client required to open merge requests"
    sid, wid = spec["spec_id"], spec["work_item_id"]
    branch, head_sha = verdict["_branch"], verdict["_head_sha"]
    ctx.event("work_item.stage", {"stage": WorkItemStage.INTEGRATING.value}, work_item_id=wid, spec_id=sid)
    uploads = {
        "spec.json": json.dumps(spec, indent=2).encode(),
        "merge_readiness.json": json.dumps(verdict, indent=2).encode(),
        "findings.json": json.dumps(findings, indent=2).encode(),
        "review_response.json": json.dumps(response, indent=2).encode(),
        "test_results.json": json.dumps(test_results, indent=2).encode(),
        "run_summary.json": json.dumps({**run_summary, "target_branch": base_branch}, indent=2).encode(),
        "mr_template.md": (ctx.cfg.templates_dir / "mr_template.md").read_bytes(),
    }
    res = ctx.driver.run(
        agent_key="integrator", title=f"{ctx.run_id} integrate {sid}", run_id=ctx.run_id, stage="integrate",
        uploads=uploads, work_item_id=wid,
        description=INTEGRATE_DESCRIPTION.format(spec_id=sid, branch=branch, head_sha=head_sha),
        rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["integrate"],
        custom_tools={"open_merge_request": make_open_mr(ctx, ctx.gitlab, sid, wid, branch)},
    )
    mr = require_json(res, "mr.json")
    persist_outputs(ctx, "integrate", res, prefix=f"{sid}-")
    ctx.event("work_item.stage", {"stage": WorkItemStage.MR_OPEN.value, "mr_iid": mr["iid"]}, work_item_id=wid, spec_id=sid)
    return mr
