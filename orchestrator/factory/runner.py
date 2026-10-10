"""Top-level run loop: wires the stages together and handles gate decisions.

This is the deterministic reducer at the heart of the factory. Everything a human sees in
the Factory Console is derived from the events this file appends.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .gates import Decision
from .gitlab import render_feedback_md
from .stages import RunContext
from .stages.decompose import gate_1, run_decompose
from .stages.implement import LocalVerifier, run_implement_one
from .stages.integrate import run_integrate
from .stages.specify import gate_2, run_specify
from .state import RunStage, WorkItemStage


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:10]}"


def _design_mr(ctx: RunContext, outcome: dict[str, Any], files: dict[str, bytes], title: str) -> tuple[int | None, dict[str, str]]:
    """Commit design artifacts under .factory/runs/<run_id>/ on a branch and open (or update) a draft MR.

    Returns (mr_iid, {artifact_name: url}) so gate notifications can deep-link to the files.
    """
    if ctx.gitlab is None:
        return None, {}
    repo = outcome["target_repositories"][0]
    branch = f"factory/{ctx.run_id}/design"
    ctx.gitlab.create_branch(branch, repo["default_branch"])
    paths = {f".factory/runs/{ctx.run_id}/{name}": content for name, content in files.items()}
    ctx.gitlab.commit_files(branch, f"factory({ctx.run_id}): {title}", paths)
    state = ctx.store.state(ctx.run_id)
    existing = next((g for g in state.open_gates.values() if g.get("design_mr_iid")), None)
    if existing:
        iid = existing["design_mr_iid"]
    else:
        mr = ctx.gitlab.create_mr(source_branch=branch, target_branch=repo["default_branch"],
                                  title=f"[factory design] {outcome['title']}", description=outcome["statement"],
                                  labels=["factory", "design", f"run:{ctx.run_id}"],
                                  reviewer_usernames=[outcome["owner"]["handle"]], draft=True)
        iid = mr.iid
    base = ctx.cfg.gitlab_url.rstrip("/") + "/" + ctx.cfg.gitlab_project
    urls = {name: f"{base}/-/blob/{branch}/.factory/runs/{ctx.run_id}/{name}" for name in files}
    return iid, urls


def _feedback_from_design_mr(ctx: RunContext, iid: int | None, decision_comments: str) -> str:
    parts = [decision_comments] if decision_comments.strip() else []
    if ctx.gitlab is not None and iid is not None:
        parts.append(render_feedback_md(ctx.gitlab.mr_notes(iid), ctx.gitlab.mr_discussions(iid)))
    return "\n\n".join(parts) or "# Human feedback\n(no comments provided)"


def run(ctx: RunContext, outcome: dict[str, Any]) -> RunStage:
    ctx.event("run.created", {"outcome_id": outcome["outcome_id"], "title": outcome["title"]}, stage="intake")
    ctx.store.put_artifact(ctx.run_id, "intake", "outcome.json", json.dumps(outcome, indent=2).encode())
    repo = outcome["target_repositories"][0]

    # ---------------- Stage 1 + Gate 1 (loop until approved or rejected)
    feedback, prior = None, None
    while True:
        decomposition, shas = run_decompose(ctx, outcome, feedback, prior)
        design_iid, urls = _design_mr(ctx, outcome, {
            "decomposition.md": ctx.store.get_artifact(ctx.run_id, "decompose", f"r{decomposition['revision']}-decomposition.md"),
            "decomposition.json": json.dumps(decomposition, indent=2).encode()}, "decomposition")
        d = gate_1(ctx, outcome, decomposition, shas, urls)
        if d.decision == Decision.APPROVE:
            break
        if d.decision == Decision.REJECT:
            ctx.event("stage.entered", {"stage": RunStage.CLOSED.value}, stage="gate_1", actor="human")
            return RunStage.CLOSED
        feedback, prior = _feedback_from_design_mr(ctx, design_iid, d.comments), decomposition

    # ---------------- Stage 2 + Gate 2
    human_comments: dict[str, str] | None = None
    while True:
        specs, verdict = run_specify(ctx, outcome, decomposition, human_comments)
        spec_shas = {f"{sid}.json": ctx.store.put_artifact(ctx.run_id, "specify", f"final-{sid}.json",
                                                            json.dumps(s, indent=2).encode()) for sid, s in specs.items()}
        spec_shas["verdict.json"] = ctx.store.put_artifact(ctx.run_id, "judge", "final-verdict.json",
                                                           json.dumps(verdict, indent=2).encode())
        design_iid, urls = _design_mr(ctx, outcome, {
            **{f"specs/{sid}.json": json.dumps(s, indent=2).encode() for sid, s in specs.items()},
            "verdict.json": json.dumps(verdict, indent=2).encode()}, "specifications")
        d = gate_2(ctx, outcome, specs, verdict, spec_shas, urls)
        if d.decision == Decision.REJECT:
            ctx.event("stage.entered", {"stage": RunStage.CLOSED.value}, stage="gate_2", actor="human")
            return RunStage.CLOSED
        if d.decision in (Decision.APPROVE, Decision.APPROVE_SUBSET):
            approved = set(d.approved_subset) if d.decision == Decision.APPROVE_SUBSET else set(specs)
            for sid in approved:
                ctx.event("work_item.stage", {"stage": WorkItemStage.SPEC_APPROVED.value, "spec_id": sid},
                          work_item_id=specs[sid]["work_item_id"], spec_id=sid, actor="human")
            specs = {sid: s for sid, s in specs.items() if sid in approved}
            break
        # request_changes: comments keyed by spec id when the human used "spec_x: ..." lines, else to all.
        fb = _feedback_from_design_mr(ctx, design_iid, d.comments)
        human_comments = {s["work_item_id"]: fb for s in specs.values()}

    # ---------------- Stage 3 + 4 per spec, in dependency waves
    ctx.event("stage.entered", {"stage": RunStage.IMPLEMENTING.value}, stage="implement")
    auth_url = repo["url"].replace("https://", f"https://oauth2:{ctx.cfg.gitlab_token}@") if ctx.cfg.gitlab_token else repo["url"]
    verifier = LocalVerifier(auth_url, ctx.cfg.runs_dir / ctx.run_id / "verify")
    merged_or_open: set[str] = set()
    remaining = dict(specs)
    while remaining:
        ready = [s for s in remaining.values()
                 if all(dep in merged_or_open or dep not in specs for dep in s["dependencies"]["specs"])]
        if not ready:
            ctx.event("run.escalated", {"reason": f"unsatisfiable dependencies among {sorted(remaining)}"})
            return RunStage.ESCALATED
        for spec in ready[: ctx.policy.parallel_implementations]:
            sid = spec["spec_id"]
            deps = {dsid: specs[dsid] for dsid in spec["dependencies"]["specs"] if dsid in specs}
            verdict = run_implement_one(ctx, spec, repo["url"], repo["default_branch"], verifier, deps)
            if verdict["result"] != "ready":
                ctx.event("run.escalated", {"reason": f"{sid}: merge judge said {verdict['result']}",
                                            "summary": verdict.get("summary_for_human", verdict.get("reason"))},
                          spec_id=sid)
                ctx.notifier.escalated(ctx.run_id, f"{sid}: {verdict['result']}")
                remaining.pop(sid)
                continue
            findings = json.loads(ctx.store.get_artifact(ctx.run_id, "review", _latest(ctx, "review", f"{sid}-", "-findings.merged.json")))
            response = _latest_json_or(ctx, "implement", f"{sid}-", "-review_response.json", {"responses": []})
            test_results = json.loads(ctx.store.get_artifact(ctx.run_id, "implement", _latest(ctx, "implement", f"{sid}-", "-test_results.json")))
            summary = {"run_id": ctx.run_id, "outcome_title": outcome["title"], "reviewers": [outcome["owner"]["handle"]],
                       "sessions": _sessions_for(ctx, sid), "list_cost_usd": ctx.store.state(ctx.run_id).list_cost_usd}
            run_integrate(ctx, spec, verdict, findings, response, test_results, summary, repo["default_branch"])
            merged_or_open.add(sid)   # Gate 3 (human review + merge) is tracked by the MR watcher, not this loop
            remaining.pop(sid)

    ctx.event("run.completed", {"specs_with_mrs": sorted(merged_or_open)})
    return RunStage.COMPLETED


def _latest(ctx: RunContext, stage: str, prefix: str, suffix: str) -> str:
    d = ctx.cfg.runs_dir / ctx.run_id / "artifacts" / stage
    cands = sorted(p.name for p in d.glob(f"{prefix}*{suffix}") if not p.name.endswith(".sha256"))
    if not cands:
        raise FileNotFoundError(f"no {prefix}*{suffix} under {d}")
    return cands[-1]


def _latest_json_or(ctx: RunContext, stage: str, prefix: str, suffix: str, default: Any) -> Any:
    try:
        return json.loads(ctx.store.get_artifact(ctx.run_id, stage, _latest(ctx, stage, prefix, suffix)))
    except FileNotFoundError:
        return default


def _sessions_for(ctx: RunContext, spec_id: str) -> list[dict[str, Any]]:
    out = []
    for ev in ctx.store.events(ctx.run_id):
        if ev.kind == "session.finished" and (ev.spec_id == spec_id or ev.stage in ("decompose", "judge")):
            out.append({"stage": ev.stage, "agent_name": ev.payload.get("agent_id"), "agent_version": ev.payload.get("agent_version"),
                        "session_id": ev.payload["session_id"], "console_url": ctx.cfg.console_session_url(ev.payload["session_id"]),
                        "list_cost_usd": ev.payload.get("list_cost_usd")})
    return out
