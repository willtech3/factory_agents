"""Top-level run loop: wires the stages together and handles gate decisions.

This is the deterministic reducer at the heart of the factory. Everything a human sees in
the Factory Console is derived from the events this file appends.

Gate presentation depends on `cfg.gate_surface` (ADR-0007):
- every surface gets an artifact page per gate (`surfaces/artifact_pages.py`) when `ctx.pages` is set;
- `forge`: the design artifacts are also committed to a design branch and opened as a draft change
  request (PR or MR), so the owner can comment inline; the decision still comes through `ctx.queue`;
- `tracker`: no design CR; `ctx.tracker` (a `TrackerQueue`, also part of `ctx.queue`) opens the gate
  on the run's tracker issue and harvests the decision and comments from there;
- `file`: pages plus the CLI's file queue only.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .gates import Decision, GateDecision
from .stages import RunContext
from .stages.decompose import gate_1, run_decompose
from .stages.implement import LocalVerifier, run_implement_one
from .stages.integrate import run_integrate
from .stages.specify import gate_2, run_specify
from .state import RunStage, WorkItemStage
from .surfaces.artifact_pages import render_gate1_page, render_gate2_page


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:10]}"


def _design_change_request(ctx: RunContext, outcome: dict[str, Any], files: dict[str, bytes], title: str
                           ) -> tuple[int | None, dict[str, str]]:
    """Commit design artifacts under .factory/runs/<run_id>/ on a branch and open (or update) a draft CR.

    Only for the `forge` gate surface. Returns (cr_id, {artifact_name: url}) so gate notifications can
    deep-link to the files.
    """
    if ctx.forge is None or ctx.cfg.gate_surface != "forge":
        return None, {}
    repo = outcome["target_repositories"][0]
    branch = f"factory/{ctx.run_id}/design"
    ctx.forge.create_branch(branch, repo["default_branch"])
    paths = {f".factory/runs/{ctx.run_id}/{name}": content for name, content in files.items()}
    ctx.forge.commit_files(branch, f"factory({ctx.run_id}): {title}", paths)
    existing = next((ev.payload for ev in ctx.store.events(ctx.run_id) if ev.kind == "cr.opened" and ev.payload.get("design")), None)
    if existing:
        cr_id = existing["id"]
    else:
        cr = ctx.forge.create_change_request(source_branch=branch, target_branch=repo["default_branch"],
                                             title=f"[factory design] {outcome['title']}", description=outcome["statement"],
                                             labels=["factory", "design", f"run:{ctx.run_id}"],
                                             reviewers=[outcome["owner"]["handle"]], draft=True)
        cr_id = cr.id
        ctx.event("cr.opened", {"id": cr.id, "url": cr.url, "branch": branch, "design": True, "forge": ctx.forge.kind})
    urls = {name: ctx.forge.file_url(branch, f".factory/runs/{ctx.run_id}/{name}") for name in files}
    return cr_id, urls


def _present(ctx: RunContext, outcome: dict[str, Any], files: dict[str, bytes], title: str, page_name: str,
             page_html: str, anchors: dict[str, str]) -> tuple[int | None, dict[str, str]]:
    """Publish the gate's reading surfaces. Returns (design_cr_id, artifact_name -> url)."""
    urls: dict[str, str] = {}
    if ctx.pages is not None:
        page_url = ctx.pages.publish(ctx.run_id, page_name, page_html)
        ctx.event("artifact.produced", {"artifact_type": "gate_page", "path": f"pages/{page_name}.html", "url": page_url},
                  stage=page_name)
        urls = {name: f"{page_url}#{anchors.get(name, 'summary')}" for name in files}
    cr_id, cr_urls = _design_change_request(ctx, outcome, files, title)
    urls.update(cr_urls)     # inline-commentable files win over the page when both exist
    return cr_id, urls


def notes_by_item(comments: str, item_ids: list[str]) -> dict[str, str]:
    """Split a decision's comments into per-item guidance. Lines like `wi-x: text` go to that item; other lines go
    to every item. Used when a gate is approved with notes: approval does not re-run the stage, but the next
    stage's writers should still hear what the human said about each item."""
    general, per_item = [], {i: [] for i in item_ids}
    for line in (comments or "").splitlines():
        line = line.strip()
        if not line:
            continue
        head, sep, rest = line.partition(":")
        if sep and head.strip() in per_item:
            if rest.strip():
                per_item[head.strip()].append(rest.strip())     # an empty "wi-x:" line says nothing
        else:
            general.append(line)
    out = {}
    for i in item_ids:
        parts = general + per_item[i]
        if parts:
            out[i] = "# Notes from the owner at Gate 1\n" + "\n".join(f"- {p}" for p in parts)
    return out


def _feedback(ctx: RunContext, design_cr_id: int | None, d: GateDecision) -> str:
    parts = [d.comments] if d.comments.strip() else []
    if ctx.forge is not None and design_cr_id is not None:
        parts.append(ctx.forge.render_feedback_md(ctx.forge.change_request_comments(design_cr_id),
                                                  ctx.forge.change_request_discussions(design_cr_id)))
    if ctx.tracker is not None:
        fb = ctx.tracker.render_feedback_md(d.gate_id)
        if fb and "(no comments)" not in fb:
            parts.append(fb)
    return "\n\n".join(parts) or "# Human feedback\n(no comments provided)"


def run(ctx: RunContext, outcome: dict[str, Any]) -> RunStage:
    ctx.event("run.created", {"outcome_id": outcome["outcome_id"], "title": outcome["title"],
                              "forge": ctx.cfg.forge if ctx.forge is not None else None, "gate_surface": ctx.cfg.gate_surface,
                              "design_only": ctx.policy.design_only}, stage="intake")
    ctx.store.put_artifact(ctx.run_id, "intake", "outcome.json", json.dumps(outcome, indent=2).encode())
    repo = outcome["target_repositories"][0]
    if ctx.tracker is not None:
        ctx.tracker.register_run(ctx.run_id, outcome)

    # ---------------- Stage 1 + Gate 1 (loop until approved or rejected)
    feedback, prior = None, None
    while True:
        decomposition, shas = run_decompose(ctx, outcome, feedback, prior)
        if ctx.tracker is not None:
            ctx.tracker.register_work_items(ctx.run_id, decomposition)
        dec_md = ctx.store.get_artifact(ctx.run_id, "decompose", f"r{decomposition['revision']}-decomposition.md")
        files = {"decomposition.md": dec_md, "decomposition.json": json.dumps(decomposition, indent=2).encode()}
        page = render_gate1_page(ctx.run_id, outcome, decomposition, dec_md.decode(errors="replace"), shas)
        design_cr_id, urls = _present(ctx, outcome, files, "decomposition", "gate_1", page,
                                      {"decomposition.md": "full", "decomposition.json": "work-items"})
        d = gate_1(ctx, outcome, decomposition, shas, urls)
        if d.decision == Decision.APPROVE:
            approval_notes = notes_by_item(d.comments, [w["work_item_id"] for w in decomposition["work_items"]])
            break
        if d.decision == Decision.REJECT:
            ctx.event("stage.entered", {"stage": RunStage.CLOSED.value}, stage="gate_1", actor="human")
            return RunStage.CLOSED
        feedback, prior = _feedback(ctx, design_cr_id, d), decomposition

    # ---------------- Stage 2 + Gate 2
    human_comments: dict[str, str] | None = approval_notes or None     # what the owner said while approving the plan
    while True:
        specs, verdict = run_specify(ctx, outcome, decomposition, human_comments)
        spec_shas = {f"{sid}.json": ctx.store.put_artifact(ctx.run_id, "specify", f"final-{sid}.json",
                                                            json.dumps(s, indent=2).encode()) for sid, s in specs.items()}
        spec_shas["verdict.json"] = ctx.store.put_artifact(ctx.run_id, "judge", "final-verdict.json",
                                                           json.dumps(verdict, indent=2).encode())
        spec_md = {sid: _spec_markdown(ctx, s) for sid, s in specs.items()}
        files = {**{f"specs/{sid}.json": json.dumps(s, indent=2).encode() for sid, s in specs.items()},
                 **{f"specs/{sid}.md": spec_md[sid].encode() for sid in specs},
                 "verdict.json": json.dumps(verdict, indent=2).encode()}
        page = render_gate2_page(ctx.run_id, outcome, specs, verdict, spec_shas, spec_md)
        anchors = {f"specs/{sid}.json": f"spec-{sid}" for sid in specs}
        anchors.update({f"specs/{sid}.md": f"spec-{sid}" for sid in specs})
        design_cr_id, urls = _present(ctx, outcome, files, "specifications", "gate_2", page, anchors)
        urls = {**urls, **{f"{sid}.json": urls.get(f"specs/{sid}.json", "") for sid in specs},
                "verdict.json": urls.get("verdict.json", "")}
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
        fb = _feedback(ctx, design_cr_id, d)
        human_comments = {s["work_item_id"]: fb for s in specs.values()}
    spec_verdict = verdict
    if ctx.policy.design_only:
        ctx.event("run.completed", {"design_only": True, "approved_specs": sorted(specs),
                                    "note": "design-only run: no implementation stage was requested"}, stage="gate_2")
        return RunStage.COMPLETED

    # ---------------- Stage 3 + 4 per spec, in dependency waves
    ctx.event("stage.entered", {"stage": RunStage.IMPLEMENTING.value}, stage="implement")
    auth_url = ctx.forge.authenticated_url(repo["url"]) if ctx.forge is not None else repo["url"]
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
            run_integrate(ctx, spec, verdict, findings, response, test_results, summary, repo["default_branch"],
                          verifier=verifier, spec_md=spec_md.get(sid, ""), spec_verdict=spec_verdict)
            merged_or_open.add(sid)   # Gate 3 (human review + merge) is tracked by the CR watcher, not this loop
            remaining.pop(sid)

    ctx.event("run.completed", {"specs_with_change_requests": sorted(merged_or_open)})
    return RunStage.COMPLETED


def _spec_markdown(ctx: RunContext, spec: dict[str, Any]) -> str:
    """The writer's spec.md for the spec's latest round, or '' when the session did not produce one."""
    if spec.get("_markdown"):
        return str(spec["_markdown"])
    try:
        name = _latest(ctx, "specify", f"{spec['work_item_id']}-", "-spec.md")
        return ctx.store.get_artifact(ctx.run_id, "specify", name).decode(errors="replace")
    except FileNotFoundError:
        return ""


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
