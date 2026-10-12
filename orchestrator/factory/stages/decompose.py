"""Stage 1: outcome -> decomposition -> Gate 1."""
from __future__ import annotations

import json
from typing import Any

from .. import checks
from ..budget import reserve_budget
from ..config import agent_model
from ..gates import Decision, GateKind
from ..state import RunStage
from . import RunContext
from .common import auto_approval, hosted_planning_clause, make_ask_human, size_clause, open_gate_and_wait, persist_outputs, platform_skill, require_json, rubric_text, run_session, stamp_provenance

DESCRIPTION = """Produce a decomposition of the business outcome in /mnt/session/uploads/outcome.json.
{repo_clause} {skill_clause}
Write decomposition.json and decomposition.md to /mnt/session/outputs/ and validate the JSON with
`python -I /mnt/session/uploads/tools/validate.py /mnt/session/outputs/decomposition.json`.
{feedback_clause}"""

REPO_MOUNTED = "The target repository is cloned at /workspace/repo (read it; do not modify it)."
REPO_ABSENT = ("The target repository is NOT available in this session. Decompose from the outcome alone; record "
               "anything you would have checked in the codebase as an assumption or an open question with a default.")


def repository_access(ctx: RunContext, outcome: dict[str, Any], mounted: str = REPO_MOUNTED, absent: str = REPO_ABSENT
                      ) -> tuple[dict[str, Any] | None, str, bool]:
    """How this session reaches the outcome's repository: (session resource, kickoff clause, available).

    GitHub: the native `github_repository` resource and a "it is mounted" clause. GitLab: no resource, the vault
    clone instructions of ADR-0003 (read-only use here). No forge: nothing, and the agent is told so."""
    repos = outcome.get("target_repositories") or []
    if ctx.forge is None or not repos:
        return None, absent, False
    url, branch = repos[0]["url"], repos[0].get("default_branch", "main")
    resource = ctx.forge.session_repository_resource(url, branch)
    if resource is not None:
        return resource, mounted, True
    return None, ctx.forge.sandbox_git_instructions(url, branch, branch) + " Read it; do not modify or push it.", True


def run_decompose(ctx: RunContext, outcome: dict[str, Any], feedback_md: str | None = None,
                  prior_decomposition: dict[str, Any] | None = None,
                  retried: bool = False) -> tuple[dict[str, Any], dict[str, str]]:
    if ctx.store.state(ctx.run_id).stage != RunStage.DECOMPOSING:   # the automatic retry stays in the same stage
        ctx.event("stage.entered", {"stage": RunStage.DECOMPOSING.value}, stage="decompose")
    uploads = {"outcome.json": json.dumps(outcome, indent=2).encode(),
               "outcome.md": outcome["statement"].encode()}
    feedback_clause = ""
    if feedback_md and prior_decomposition:
        uploads["prior/feedback.md"] = feedback_md.encode()
        uploads["prior/decomposition.json"] = json.dumps(prior_decomposition, indent=2).encode()
        feedback_clause = ("This is a re-run after human review. Read /mnt/session/uploads/prior/feedback.md first "
                           "and address every comment explicitly in decomposition.md. Increment `revision`.")

    mount, repo_clause, repo_available = repository_access(ctx, outcome)
    skill_uploads, skill_clause = platform_skill(ctx)
    reserve_budget(ctx, "decompose")
    res = run_session(
        ctx, agent_key="decomposer", title=f"{ctx.run_id} decompose", run_id=ctx.run_id, stage="decompose",
        uploads={**uploads, **skill_uploads},
        description=DESCRIPTION.format(feedback_clause=feedback_clause, repo_clause=repo_clause, skill_clause=skill_clause) + size_clause(ctx) + hosted_planning_clause(ctx),
        rubric_md=rubric_text(ctx, "decomposition", repo_available), max_iterations=ctx.policy.outcome_max_iterations,
        budget_usd=ctx.policy.stage_budget_usd["decompose"],
        custom_tools={"ask_human": make_ask_human(ctx, "decomposer")}, repository=mount,
    )
    decomposition = require_json(res, "decomposition.json")
    if prior_decomposition is not None and int(decomposition.get("revision") or 1) <= int(prior_decomposition.get("revision") or 0):
        # Artefacts are named by revision and the store refuses to overwrite one; the number is the orchestrator's.
        decomposition["revision"] = int(prior_decomposition.get("revision") or 0) + 1
    stamp_provenance(decomposition, ctx, res, "Decomposer", agent_model("decomposer"),
                     {"outcome.json": ctx.store.put_artifact(ctx.run_id, "intake", "outcome.json", uploads["outcome.json"])})
    res.outputs["decomposition.json"] = json.dumps(decomposition, indent=2).encode()
    shas = persist_outputs(ctx, "decompose", res, prefix=f"r{decomposition['revision']}-")

    # Deterministic checks before a human sees it.
    results = [
        checks.schema_valid(decomposition, ctx.cfg.schemas_dir / "decomposition.schema.json", ctx.cfg.schemas_dir),
        checks.dag_acyclic(decomposition["work_items"]),
        checks.metric_coverage(outcome, decomposition),
        checks.decomposition_size(decomposition, ctx.policy.size_budget_lines),
    ]
    failed = [r for r in results if not r.passed]
    ctx.event("artifact.produced", {"artifact_type": "decomposition", "revision": decomposition["revision"],
                                    "work_items": decomposition["work_items"],
                                    "checks": [r.to_dict() for r in results]}, stage="decompose")
    if failed:
        # One automatic retry with the failures as feedback, after the owner's own when there was some; then escalate.
        if not retried:
            fb = (f"{feedback_md.rstrip()}\n\n" if feedback_md else "") + "# Deterministic check failures\n" + \
                "\n".join(f"- {r.check}: {r.detail}" for r in failed)
            return run_decompose(ctx, outcome, feedback_md=fb, prior_decomposition=decomposition, retried=True)
        ctx.event("run.escalated", {"reason": "decomposition failed deterministic checks twice",
                                    "checks": [r.to_dict() for r in failed]}, stage="decompose")
        raise RuntimeError("decomposition failed deterministic checks twice; escalated")
    return decomposition, shas


def gate_1(ctx: RunContext, outcome: dict[str, Any], decomposition: dict[str, Any], shas: dict[str, str],
           artifact_urls: dict[str, str] | None = None):
    ctx.event("stage.entered", {"stage": RunStage.GATE_1_DECOMPOSITION.value}, stage="gate_1")
    summary = decomposition["summary"]
    open_q = [q for q in decomposition.get("open_questions", []) if q.get("blocking")]
    if open_q:
        summary += "\n\nBlocking questions the owner must answer:\n" + "\n".join(f"- {q['question']}" for q in open_q)
    return open_gate_and_wait(
        ctx, GateKind.DECOMPOSITION_REVIEW, shas, summary, assignees=[outcome["owner"]["handle"]],
        options=(Decision.APPROVE, Decision.REQUEST_CHANGES, Decision.REJECT), urls=artifact_urls,
        auto_approve=auto_approval(ctx, "gate_1", [q["question"] for q in open_q]),
    )
