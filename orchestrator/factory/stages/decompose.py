"""Stage 1: outcome -> decomposition -> Gate 1."""
from __future__ import annotations

import json
from typing import Any

from .. import checks
from ..gates import Decision, GateKind
from ..state import RunStage
from . import RunContext
from .common import make_ask_human, open_gate_and_wait, persist_outputs, provenance, require_json

DESCRIPTION = """Produce a decomposition of the business outcome in /mnt/session/uploads/outcome.json.
The target repository is cloned at /workspace/repo (read it; do not modify it).
Write decomposition.json and decomposition.md to /mnt/session/outputs/ and validate the JSON with
`python -I /mnt/session/uploads/tools/validate.py /mnt/session/outputs/decomposition.json`.
{feedback_clause}"""


def run_decompose(ctx: RunContext, outcome: dict[str, Any], feedback_md: str | None = None,
                  prior_decomposition: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    ctx.event("stage.entered", {"stage": RunStage.DECOMPOSING.value}, stage="decompose")
    uploads = {"outcome.json": json.dumps(outcome, indent=2).encode(),
               "outcome.md": outcome["statement"].encode()}
    feedback_clause = ""
    if feedback_md and prior_decomposition:
        uploads["prior/feedback.md"] = feedback_md.encode()
        uploads["prior/decomposition.json"] = json.dumps(prior_decomposition, indent=2).encode()
        feedback_clause = ("This is a re-run after human review. Read /mnt/session/uploads/prior/feedback.md first "
                           "and address every comment explicitly in decomposition.md. Increment `revision`.")

    res = ctx.driver.run(
        agent_key="decomposer", title=f"{ctx.run_id} decompose", run_id=ctx.run_id, stage="decompose",
        uploads=uploads, description=DESCRIPTION.format(feedback_clause=feedback_clause),
        rubric_md=ctx.rubric("decomposition"), max_iterations=ctx.policy.outcome_max_iterations,
        budget_usd=ctx.policy.stage_budget_usd["decompose"],
        custom_tools={"ask_human": make_ask_human(ctx, "decomposer")},
    )
    decomposition = require_json(res, "decomposition.json")
    decomposition.setdefault("produced_by", provenance(ctx, res, "Decomposer", "claude-opus-5-5",
                                                       {"outcome.json": ctx.store.put_artifact(ctx.run_id, "intake", "outcome.json", uploads["outcome.json"])}))
    res.outputs["decomposition.json"] = json.dumps(decomposition, indent=2).encode()
    shas = persist_outputs(ctx, "decompose", res, prefix=f"r{decomposition['revision']}-")

    # Deterministic checks before a human sees it.
    results = [
        checks.schema_valid(decomposition, ctx.cfg.schemas_dir / "decomposition.schema.json", ctx.cfg.schemas_dir),
        checks.dag_acyclic(decomposition["work_items"]),
        checks.metric_coverage(outcome, decomposition),
    ]
    failed = [r for r in results if not r.passed]
    ctx.event("artifact.produced", {"artifact_type": "decomposition", "revision": decomposition["revision"],
                                    "work_items": decomposition["work_items"],
                                    "checks": [r.to_dict() for r in results]}, stage="decompose")
    if failed:
        # One automatic retry with the failures as feedback; then escalate.
        if not feedback_md:
            fb = "# Deterministic check failures\n" + "\n".join(f"- {r.check}: {r.detail}" for r in failed)
            return run_decompose(ctx, outcome, feedback_md=fb, prior_decomposition=decomposition)
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
    )
