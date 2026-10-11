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

import contextlib
import dataclasses
import fcntl
import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from collections.abc import Callable, Iterator
from typing import Any

from .approvals import approval_guidance, subset_problems
from .budget import BudgetPaused, last_pause
from .config import managed_gaps
from .gates import Decision, Gate, GateDecision, GateKind, parse_budget
from .stages import RunContext
from .stages.decompose import gate_1, run_decompose
from .stages.common import artifact_json_or_none, gate_decision, public
from .stages.implement import LocalVerifier, ResumePoint, run_implement_one
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


def policy_snapshot(ctx: RunContext) -> dict[str, Any]:
    """What this run was tuned with, recorded on run.created so the Console can show it beside the run."""
    p = ctx.policy
    return {"run_budget_usd": p.run_budget_usd, "reviewer_passes": p.reviewer_passes, "review_max_rounds": p.review_max_rounds,
            "security_review": getattr(p, "security_review", True), "parallel_implementations": p.parallel_implementations,
            # the pass also needs its optional agent deployed; without one it is skipped whatever the policy says
            "security_review_available": "security_reviewer" in ctx.cfg.agents,
            "outcome_max_iterations": p.outcome_max_iterations, "stage_budget_usd": dict(p.stage_budget_usd),
            "preview_mode": ctx.cfg.preview_mode, "design_only": p.design_only,
            "size_budget_lines": dict(getattr(p, "size_budget_lines", {}) or {})}


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
    ctx.event("run.created", {"outcome_id": outcome["outcome_id"], "title": outcome["title"], "policy": policy_snapshot(ctx),
                              "forge": ctx.cfg.forge if ctx.forge is not None else None, "gate_surface": ctx.cfg.gate_surface,
                              "design_only": ctx.policy.design_only, "budget_usd": ctx.policy.run_budget_usd}, stage="intake")
    ctx.store.put_artifact(ctx.run_id, "intake", "outcome.json", json.dumps(outcome, indent=2).encode())
    if ctx.tracker is not None:
        ctx.tracker.register_run(ctx.run_id, outcome)
    return until_budget_pause(lambda: _from_decompose(ctx, outcome))


def until_budget_pause(step: Callable[[], RunStage]) -> RunStage:
    """Run a pipeline step; a budget pause ends the process cleanly in PAUSED_BUDGET. By the time `BudgetPaused`
    reaches here the run is already parked (`budget.reached`), the budget gate is open on the gate surface, and no
    session is in flight (`budget.py`), so there is nothing to fail or interrupt."""
    try:
        return step()
    except BudgetPaused as e:
        print(f"[budget] {e}")
        return RunStage.PAUSED_BUDGET


def _from_decompose(ctx: RunContext, outcome: dict[str, Any], feedback: str | None = None,
                    prior: dict[str, Any] | None = None) -> RunStage:
    """Stage 1 and Gate 1 (loop until approved or rejected), then the rest of the run. `feedback` and `prior` are
    the owner's request-changes and the decomposition it was about, when resuming a re-decomposition."""
    # ---------------- Stage 1 + Gate 1 (loop until approved or rejected)
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
    return _from_specify(ctx, outcome, decomposition, approval_notes or None, None)


def _from_specify(ctx: RunContext, outcome: dict[str, Any], decomposition: dict[str, Any],
                  human_comments: dict[str, str] | None, prior_specs: dict[str, dict] | None,
                  previous_verdict: dict[str, Any] | None = None, round_offset: int = 0,
                  pending: "PendingGate2 | None" = None) -> RunStage:
    """Stage 2 and Gate 2 (loop until approved or rejected), then implementation unless the run is design-only.
    `human_comments` are the owner's notes per work item (Gate 1 approval notes, or routed Gate 2 feedback with
    `prior_specs` as the drafts they were about). `previous_verdict` is the verdict the owner sent back, if any, and
    `round_offset` the last round already in the store: rounds keep counting across send-backs and resumes, so no
    artefact name is reused. `pending` (a recovered controller, `resume`) is a Gate 2 already open on the verified
    final artefacts: the first pass waits for the owner's decision on that very gate instead of writing and opening one."""
    # ---------------- Stage 2 + Gate 2
    while True:
        if pending is not None:
            specs, verdict, spec_md = pending.specs, pending.verdict, pending.spec_md
            design_cr_id = _design_cr_id(ctx)
            d = wait_for_gate(ctx, pending.gate_id, pending.kind)
            pending = None
        else:
            specs, verdict, spec_md, design_cr_id, d = _specify_and_gate(ctx, outcome, decomposition, human_comments, prior_specs,
                                                                         previous_verdict, round_offset)
        if d.decision == Decision.REJECT:
            ctx.event("stage.entered", {"stage": RunStage.CLOSED.value}, stage="gate_2", actor="human")
            return RunStage.CLOSED
        if d.decision in (Decision.APPROVE, Decision.APPROVE_SUBSET):
            approved = set(d.approved_subset) if d.decision == Decision.APPROVE_SUBSET else set(specs)
            problems = subset_problems(specs, d.approved_subset) if d.decision == Decision.APPROVE_SUBSET else []
            if problems:
                # The decision surfaces refuse such a selection; one that still arrives never starts any implementation.
                raise RuntimeError(f"Gate 2 {d.gate_id}: the approved selection is not dependency-valid ({'; '.join(problems)}); "
                                   f"nothing was implemented")
            selected = {sid: s for sid, s in specs.items() if sid in approved}
            guidance = record_approval(ctx, d, selected)
            for sid in sorted(approved):
                ctx.event("work_item.stage", {"stage": WorkItemStage.SPEC_APPROVED.value, "spec_id": sid},
                          work_item_id=specs[sid]["work_item_id"], spec_id=sid, actor="human")
            specs = selected
            break
        human_comments, prior_specs = _route_gate2_feedback(ctx, design_cr_id, d, specs)
        previous_verdict, round_offset = verdict, max(int(verdict.get("round") or round_offset + 1), round_offset)
    spec_verdict = verdict
    if ctx.policy.design_only:
        ctx.event("run.completed", {"design_only": True, "approved_specs": sorted(specs),
                                    "note": "design-only run: no implementation stage was requested"}, stage="gate_2")
        return RunStage.COMPLETED

    return implement_and_integrate(ctx, outcome, specs, spec_md, spec_verdict, guidance=guidance)


def record_approval(ctx: RunContext, d: GateDecision, approved: dict[str, dict]) -> dict[str, str]:
    """Keep what Gate 2 approved and the owner's notes with it: the sha256 of each approved spec as stored for the gate
    (`specify/final-<spec_id>.json`) and the bounded per-spec guidance from the decision's comments. The implementer gets
    the guidance; a resume re-reads both and refuses a spec whose bytes no longer match what was approved."""
    hashes = {sid: hashlib.sha256(ctx.store.get_artifact(ctx.run_id, "specify", f"final-{sid}.json")).hexdigest()
              for sid in sorted(approved)}
    if any(sha not in d.artifact_shas for sha in hashes.values()):
        raise RuntimeError(f"Gate 2 {d.gate_id}: stored specs no longer match the approved artifacts")
    record = {"gate_id": d.gate_id, "decision": d.decision.value, "decided_by": d.decided_by,
              "approved": hashes,
              "guidance": approval_guidance(d.comments, approved)}
    name = f"approval-{d.gate_id}.json"
    ctx.store.put_artifact(ctx.run_id, "gate_2", name, json.dumps(record, indent=2).encode())
    ctx.event("artifact.produced", {"artifact_type": "gate2_approval", "path": f"gate_2/{name}", "gate_id": d.gate_id,
                                    "approved": sorted(approved), "guidance_for": sorted(record["guidance"])}, stage="gate_2")
    return record["guidance"]


def approved_record(ctx: RunContext, events: list[Any], specs: dict[str, dict]) -> dict[str, str]:
    """The guidance a resumed implementation carries, after checking each approved spec is still the approved bytes. Runs
    approved before approvals were recorded resume without guidance, as they did before."""
    produced = next((ev.payload for ev in reversed(events) if ev.kind == "artifact.produced"
                     and ev.payload.get("artifact_type") == "gate2_approval"), None)
    if produced is None:
        return {}
    record = json.loads(ctx.store.get_artifact(ctx.run_id, "gate_2", Path(produced["path"]).name))
    if set(record["approved"]) != set(specs):
        raise RuntimeError(f"the specs marked approved ({sorted(specs)}) differ from Gate 2 {record['gate_id']}'s approval "
                           f"({sorted(record['approved'])})")
    for sid, sha in record["approved"].items():
        if hashlib.sha256(ctx.store.get_artifact(ctx.run_id, "specify", f"final-{sid}.json")).hexdigest() != sha:
            raise RuntimeError(f"specify/final-{sid}.json no longer matches the spec Gate 2 {record['gate_id']} approved")
    return {sid: text for sid, text in record.get("guidance", {}).items() if sid in specs}


def _specify_and_gate(ctx: RunContext, outcome: dict[str, Any], decomposition: dict[str, Any], human_comments: dict[str, str] | None,
                      prior_specs: dict[str, dict] | None, previous_verdict: dict[str, Any] | None, round_offset: int
                      ) -> tuple[dict[str, dict], dict[str, Any], dict[str, str], int | None, GateDecision]:
    """One specification pass and its Gate 2: write and judge the specs, publish them as the gate's `final-*`
    artefacts, present them, and wait for the owner's decision."""
    specs, verdict = run_specify(ctx, outcome, decomposition, human_comments, prior_specs,
                                 previous_verdict=previous_verdict, round_offset=round_offset)
    # `final-*` point at the current candidate for the gate (replaced on every cycle); the per-round artefacts the
    # owner decided on keep their names and are never overwritten.
    spec_shas = {f"{sid}.json": ctx.store.put_artifact(ctx.run_id, "specify", f"final-{sid}.json",
                                                        json.dumps(public(s), indent=2).encode(), replace=True)
                 for sid, s in specs.items()}
    spec_md = {sid: _spec_markdown(ctx, s) for sid, s in specs.items()}
    for sid, md in spec_md.items():
        if md:
            ctx.store.put_artifact(ctx.run_id, "specify", f"final-{sid}.md", md.encode(), replace=True)
    spec_shas["verdict.json"] = ctx.store.put_artifact(ctx.run_id, "judge", "final-verdict.json",
                                                       json.dumps(verdict, indent=2).encode(), replace=True)
    files = {**{f"specs/{sid}.json": json.dumps(public(s), indent=2).encode() for sid, s in specs.items()},
             **{f"specs/{sid}.md": spec_md[sid].encode() for sid in specs if spec_md[sid]},
             "verdict.json": json.dumps(verdict, indent=2).encode()}
    page = render_gate2_page(ctx.run_id, outcome, {sid: public(s) for sid, s in specs.items()}, verdict, spec_shas, spec_md)
    anchors = {f"specs/{sid}.json": f"spec-{sid}" for sid in specs}
    anchors.update({f"specs/{sid}.md": f"spec-{sid}" for sid in specs})
    design_cr_id, urls = _present(ctx, outcome, files, "specifications", "gate_2", page, anchors)
    urls = {**urls, **{f"{sid}.json": urls.get(f"specs/{sid}.json", "") for sid in specs},
            "verdict.json": urls.get("verdict.json", "")}
    d = gate_2(ctx, outcome, specs, verdict, spec_shas, urls)
    return specs, verdict, spec_md, design_cr_id, d


def _route_gate2_feedback(ctx: RunContext, design_cr_id: int | None, d: GateDecision, specs: dict[str, dict]
                          ) -> tuple[dict[str, str], dict[str, dict]]:
    """request_changes at Gate 2: per-item lines (`spec_x: ...` or `wi-x: ...`) go to that item, the rest to every
    item; the writers also get the draft the owner commented on."""
    fb = _feedback(ctx, design_cr_id, d)
    ids = [s["work_item_id"] for s in specs.values()] + list(specs)
    routed = notes_by_item(fb, ids)
    human_comments = {s["work_item_id"]: (routed.get(s["work_item_id"]) or routed.get(sid) or fb) for sid, s in specs.items()}
    prior_specs = {s["work_item_id"]: s for s in specs.values()}
    return human_comments, prior_specs


def _design_cr_id(ctx: RunContext) -> int | None:
    return next((ev.payload["id"] for ev in ctx.store.events(ctx.run_id) if ev.kind == "cr.opened" and ev.payload.get("design")), None)


def _decision_from_event(p: dict[str, Any]) -> GateDecision:
    """The GateDecision a `gate.decided` event recorded, for feeding a resumed stage the same feedback."""
    return GateDecision(gate_id=p["gate_id"], decision=Decision(p["decision"]), decided_by=p.get("decided_by", "human"),
                        comments=p.get("comments", ""), approved_subset=tuple(p.get("approved_subset", [])),
                        artifact_shas=tuple(p.get("artifact_shas", [])))


def _spec_markdown(ctx: RunContext, spec: dict[str, Any]) -> str:
    """The writer's spec.md for this spec: carried on the object by `run_specify`, else the `final-<spec_id>.md` the
    runner stored at Gate 2 (what `factory resume` reads), else the latest round in the store, else ''."""
    if spec.get("_markdown"):
        return str(spec["_markdown"])
    try:
        return ctx.store.get_artifact(ctx.run_id, "specify", f"final-{spec['spec_id']}.md").decode(errors="replace")
    except FileNotFoundError:
        pass
    try:
        name = _latest(ctx, "specify", f"{spec['work_item_id']}-", "-spec.md")
        return ctx.store.get_artifact(ctx.run_id, "specify", name).decode(errors="replace")
    except FileNotFoundError:
        return ""


def _latest(ctx: RunContext, stage: str, prefix: str, suffix: str) -> str:
    """The highest-numbered `<prefix>r<n>...<suffix>` artefact name (numeric, so r10 follows r9)."""
    d = ctx.cfg.runs_dir / ctx.run_id / "artifacts" / stage
    cands = [p.name for p in d.glob(f"{prefix}*{suffix}") if not p.name.endswith(".sha256")]
    if not cands:
        raise FileNotFoundError(f"no {prefix}*{suffix} under {d}")

    def round_of(name: str) -> int:
        mid = name[len(prefix):-len(suffix)]
        digits = mid[1:].split("-")[0] if mid.startswith("r") else ""
        return int(digits) if digits.isdigit() else -1
    return max(cands, key=lambda n: (round_of(n), n))


def _latest_json_or(ctx: RunContext, stage: str, prefix: str, suffix: str, default: Any) -> Any:
    try:
        return json.loads(ctx.store.get_artifact(ctx.run_id, stage, _latest(ctx, stage, prefix, suffix)))
    except FileNotFoundError:
        return default


def _sessions_for(ctx: RunContext, spec_id: str, work_item_id: str | None = None) -> list[dict[str, Any]]:
    """Every session behind this spec's change request, for the provenance table: the decomposer and the spec judge
    rounds (shared by every spec), and the writer, implementer, reviewer, merge-judge and integrator sessions of this
    spec or its work item, each with its cost and its duration from session.started to session.finished. Live run 2's
    table listed 5 of 17 sessions with no duration because session.finished carried no spec id (post-mortem issue 16)."""
    events = ctx.store.events(ctx.run_id)
    started = {ev.payload.get("session_id"): ev for ev in events if ev.kind == "session.started"}
    out = []
    for ev in events:
        if ev.kind != "session.finished":
            continue
        mine = ev.spec_id == spec_id or (work_item_id is not None and ev.work_item_id == work_item_id)
        if not (mine or ev.stage in ("decompose", "judge")):
            continue
        sid = ev.payload["session_id"]
        s = started.get(sid)
        seconds = _seconds_between(s.at, ev.at) if s is not None else None
        out.append({"stage": ev.stage, "agent_name": ev.payload.get("agent_id"), "agent_version": ev.payload.get("agent_version"),
                    "agent_key": s.payload.get("agent_key") if s is not None else None,
                    "session_id": sid, "console_url": ctx.cfg.console_session_url(sid),
                    "list_cost_usd": ev.payload.get("list_cost_usd"), "stop_reason": ev.payload.get("stop_reason"),
                    "started_at": s.at if s is not None else None, "finished_at": ev.at,
                    "duration_seconds": seconds, "duration": _hms(seconds)})
    return out


def _seconds_between(start_iso: str, end_iso: str) -> int | None:
    from datetime import datetime
    try:
        return max(0, int((datetime.fromisoformat(end_iso) - datetime.fromisoformat(start_iso)).total_seconds()))
    except (TypeError, ValueError):
        return None


def _hms(seconds: int | None) -> str:
    if seconds is None:
        return "n/a"
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def comparison_branch(ctx: RunContext, repo: dict[str, Any], verifier: LocalVerifier) -> str:
    """Use the ordinary target, or the owner's recorded and pinned evaluation target."""
    # A pipeline evaluation may compare an already-merged implementation with its original baseline.
    # This is an explicit owner override; the approved outcome/spec bytes remain unchanged.
    recorded = next((e.payload for e in reversed(ctx.store.events(ctx.run_id))
                     if e.kind == "artifact.produced" and e.payload.get("artifact_type") == "evaluation_target"), None)
    if recorded:
        target = (recorded["branch"], recorded["base_sha"])
        if ctx.evaluation_target and ctx.evaluation_target != target:
            raise RuntimeError("evaluation target differs from the recorded owner instruction")
        ctx.evaluation_target = target
    default_branch = repo["default_branch"]
    if ctx.evaluation_target:
        default_branch, pinned_base = ctx.evaluation_target
        import subprocess
        checked = verifier.checkout(default_branch)
        actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checked, check=True,
                                capture_output=True, text=True).stdout.strip()
        if actual != pinned_base:
            raise RuntimeError("evaluation comparison branch must remain pinned to the approved baseline")
        if recorded is None:
            ctx.event("artifact.produced", {"artifact_type": "evaluation_target", "branch": default_branch,
                                            "base_sha": pinned_base, "normal_target": repo["default_branch"],
                                            "purpose": "owner-authorized duplicate pipeline evaluation; draft PR only"}, actor="human")
    return default_branch


def implement_and_integrate(ctx: RunContext, outcome: dict[str, Any], specs: dict[str, dict], spec_md: dict[str, str],
                            spec_verdict: dict[str, Any] | None, resume: dict[str, ResumePoint] | None = None,
                            guidance: dict[str, str] | None = None) -> RunStage:
    """Stage 3 + 4 per approved spec, in dependency waves. `resume` (from `factory resume`) says where each spec's
    implement/review loop continues; specs whose change request is already open are not touched again. `guidance` is the
    owner's bounded Gate 2 approval notes per spec (`record_approval`), handed to that spec's implementer."""
    repo = outcome["target_repositories"][0]
    if resume is None:
        ctx.event("stage.entered", {"stage": RunStage.IMPLEMENTING.value}, stage="implement")
    auth_url = ctx.forge.authenticated_url(repo["url"]) if ctx.forge is not None else repo["url"]
    verifier = LocalVerifier(auth_url, ctx.cfg.runs_dir / ctx.run_id / "verify")
    default_branch = comparison_branch(ctx, repo, verifier)
    cr_by_spec: dict[str, int] = _open_change_requests(ctx)                       # finished before a resume
    merged_or_open: set[str] = set(cr_by_spec)
    remaining = {sid: s for sid, s in specs.items() if sid not in merged_or_open}
    while remaining:
        ready = [s for s in remaining.values()
                 if all(dep in merged_or_open or dep not in specs for dep in s["dependencies"]["specs"])]
        if not ready:
            ctx.event("run.escalated", {"reason": f"unsatisfiable dependencies among {sorted(remaining)}"})
            return RunStage.ESCALATED
        for spec in ready[: ctx.policy.parallel_implementations]:
            sid = spec["spec_id"]
            deps = {dsid: specs[dsid] for dsid in spec["dependencies"]["specs"] if dsid in specs}
            base_branch, blocker = base_branch_for(ctx, spec, cr_by_spec, default_branch)
            if blocker:
                ctx.event("run.escalated", {"reason": f"{sid}: {blocker}"}, spec_id=sid)
                ctx.notifier.escalated(ctx.run_id, f"{sid}: {blocker}")
                remaining.pop(sid)
                continue
            if base_branch != default_branch:
                ctx.event("artifact.produced", {"artifact_type": "stacked_base", "base_branch": base_branch,
                                                "note": "the dependency's change request is open but not merged; this item is built on its branch"},
                          work_item_id=spec["work_item_id"], spec_id=sid, stage="implement")
            verdict = run_implement_one(ctx, spec, repo["url"], base_branch, verifier, deps,
                                        spec_md=spec_md.get(sid, ""), resume_from=(resume or {}).get(sid),
                                        guidance=(guidance or {}).get(sid, ""))
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
            sessions = _sessions_for(ctx, sid, spec["work_item_id"])
            summary = {"run_id": ctx.run_id, "outcome_title": outcome["title"], "reviewers": [outcome["owner"]["handle"]],
                       "sessions": sessions, "list_cost_usd": ctx.store.state(ctx.run_id).list_cost_usd,
                       "sessions_cost_usd": round(sum(s["list_cost_usd"] or 0.0 for s in sessions), 2),
                       "sessions_duration_seconds": sum(s["duration_seconds"] or 0 for s in sessions),
                       "sessions_duration": _hms(sum(s["duration_seconds"] or 0 for s in sessions))}
            cr = run_integrate(ctx, spec, verdict, findings, response, test_results, summary, base_branch,
                               verifier=verifier, spec_md=spec_md.get(sid, ""), spec_verdict=spec_verdict)
            cr_by_spec[sid] = int(cr.get("id", cr.get("iid")))
            merged_or_open.add(sid)   # Gate 3 (human review + merge) is tracked by the CR watcher, not this loop
            remaining.pop(sid)

    ctx.event("run.completed", {"specs_with_change_requests": sorted(merged_or_open)})
    return RunStage.COMPLETED


def _cr_already_open(ctx: RunContext, spec_id: str) -> bool:
    return spec_id in _open_change_requests(ctx)


def _open_change_requests(ctx: RunContext) -> dict[str, int]:
    """spec_id -> change request id for every (non-design) change request this run has opened."""
    out: dict[str, int] = {}
    for ev in ctx.store.events(ctx.run_id):
        if ev.kind == "cr.opened" and ev.spec_id and not ev.payload.get("design"):
            out[ev.spec_id] = int(ev.payload["id"])
    return out


def base_branch_for(ctx: RunContext, spec: dict[str, Any], cr_by_spec: dict[str, int], default_branch: str) -> tuple[str, str | None]:
    """The branch a spec is implemented from, given where its dependencies stand on the forge.

    No dependencies, or every dependency's change request already merged: the default branch. One dependency
    whose change request is open but not merged: that dependency's branch, so the item is built on the code it
    needs and its change request targets that branch (a stacked change request; the forge retargets it to the
    default branch when the dependency merges). A dependency closed without merging, or two unmerged
    dependencies (nothing to stack on cleanly): a blocker the run escalates with. Dependencies outside this run's
    specs are assumed already on the default branch."""
    from .stages.implement import _branch_for

    unmerged: list[str] = []
    for dep in spec["dependencies"]["specs"]:
        cr_id = cr_by_spec.get(dep)
        if cr_id is None:
            continue                                    # not opened by this run: on the default branch already
        state = ctx.forge.change_request_state(cr_id)["state"] if ctx.forge is not None else "opened"
        if state == "merged":
            continue
        if state == "closed":
            return default_branch, f"dependency {dep}'s change request #{cr_id} was closed without merging"
        unmerged.append(dep)
    if not unmerged:
        return default_branch, None
    if len(unmerged) > 1:
        return default_branch, f"depends on {len(unmerged)} unmerged change requests ({', '.join(unmerged)}); merge them first and resume"
    return _branch_for(ctx.run_id, unmerged[0]), None


def resume_points(ctx: RunContext, specs: dict[str, dict]) -> dict[str, ResumePoint]:
    """Per spec, the implementer round to continue at, read from what the dead orchestrator persisted.

    The latest persisted implementation_report (round n) means that round's session finished: continue at round n
    with its report (and its review_response from round 2 on), answering round n-1's merged findings; the
    orchestrator's own verification of that head is redone, and reviewer passes or a merge verdict already stored
    for the round are reused. A round-n report without the response its findings required, or no report at all,
    means that round's implementer runs (again)."""
    out: dict[str, ResumePoint] = {}
    for sid in specs:
        rounds = _rounds(ctx, "implement", f"{sid}-r", "-implementation_report.json")
        if not rounds:
            out[sid] = ResumePoint(round_no=1)
            continue
        n = max(rounds)
        findings = artifact_json_or_none(ctx, "review", f"{sid}-r{n - 1}-findings.merged.json") if n > 1 else None
        if n > 1 and findings is None:
            raise RuntimeError(f"cannot resume {sid}: round {n} report exists but round {n - 1} has no merged findings")
        report = json.loads(ctx.store.get_artifact(ctx.run_id, "implement", f"{sid}-r{n}-implementation_report.json"))
        response = artifact_json_or_none(ctx, "implement", f"{sid}-r{n}-review_response.json")
        if n > 1 and response is None:
            out[sid] = ResumePoint(round_no=n, findings=findings)            # rerun round n's implementer
        else:
            out[sid] = ResumePoint(round_no=n, findings=findings, report=report, response=response)
    return out


def _rounds(ctx: RunContext, stage: str, prefix: str, suffix: str) -> list[int]:
    d = ctx.cfg.runs_dir / ctx.run_id / "artifacts" / stage
    out = []
    for p in d.glob(f"{prefix}*{suffix}"):
        if p.name.endswith(".sha256"):
            continue
        mid = p.name[len(prefix):-len(suffix)]
        if mid.isdigit():
            out.append(int(mid))
    return sorted(out)


# ---------------------------------------------------------------- a controller stopped while idle at Gate 2

GATE_POLL_SECONDS = 10.0
GATE2_KINDS = (GateKind.SPEC_READINESS, GateKind.ESCALATION)
GATE2_OPTIONS = {Decision.APPROVE, Decision.APPROVE_SUBSET, Decision.REQUEST_CHANGES, Decision.REJECT}
_SAFE_SPEC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_HELD_LOCKS: dict[str, Any] = {}


class RunLocked(RuntimeError):
    """Another controller holds the run's lock."""


@contextlib.contextmanager
def controller_lock(runs_dir: Path, run_id: str) -> Iterator[None]:
    """A nonblocking exclusive lock on `runs/<run_id>/controller.lock`, held for the whole resumed drive, so two
    recovering controllers never wait on (and consume) the same pending gate. Re-entrant within one process: the CLI
    takes it before the drive and `resume` finds it held. Raises `RunLocked`, before anything is written, when another
    process holds it. Controllers started by older code take no lock: stop them first (`run.failed` proves it)."""
    if run_id in _HELD_LOCKS:
        yield
        return
    path = runs_dir / run_id / "controller.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RunLocked(f"another controller holds run {run_id} ({path}); stop it before resuming") from None
    _HELD_LOCKS[run_id] = handle
    try:
        yield
    finally:
        _HELD_LOCKS.pop(run_id, None)
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


@dataclasses.dataclass(frozen=True)
class PendingGate2:
    """A Gate 2 still waiting for its owner when its controller stopped, with the exact artefacts it presented."""
    gate_id: str
    kind: GateKind
    presented: dict[str, str]                  # artefact name as presented -> sha256
    specs: dict[str, dict]
    spec_md: dict[str, str]
    verdict: dict[str, Any]
    decomposition: dict[str, Any]


def _queued_gate(queue: Any, gate_id: str) -> Gate | None:
    """The gate as the decision surface holds it (the record decisions are validated against), from any composed queue."""
    for q in getattr(queue, "queues", [queue]):
        finder = getattr(q, "gate", None)
        gate = finder(gate_id) if callable(finder) else getattr(q, "gates", {}).get(gate_id)
        if isinstance(gate, Gate):
            return gate
    return None


def pending_gate2(ctx: RunContext, events: list[Any] | None = None) -> PendingGate2:
    """The run's pending Gate 2 when its controller was stopped while idle at it, verified, or RuntimeError saying why
    not. Nothing is written. The run must be `failed` with the last gate a still-undecided specification-readiness or
    escalation gate, the only open gate; after it only the stop (`run.failed`, no session interrupted) and earlier
    recoveries of this same gate. Every artefact the gate presented must be stored with exactly the presented sha256
    (`specify/final-<spec_id>.json`, `judge/final-verdict.json`), and the gate surface's record of the gate must be the
    one the log opened, so a decision is checked against what the owner was shown."""
    st = ctx.store.state(ctx.run_id)
    events = events if events is not None else ctx.store.events(ctx.run_id)
    if st.stage != RunStage.FAILED:
        raise RuntimeError(f"run {ctx.run_id} is {st.stage.value}; a pending Gate 2 can be recovered only after its controller "
                           f"was stopped (the stop records run.failed)")
    opened = [ev for ev in events if ev.kind == "gate.opened"]
    if not opened or opened[-1].payload.get("kind") not in {k.value for k in GATE2_KINDS}:
        raise RuntimeError(f"run {ctx.run_id} did not stop at Gate 2 (its last gate is {opened[-1].payload.get('kind') if opened else 'none'})")
    gate_ev = opened[-1]
    gate_id, kind = gate_ev.payload["gate_id"], GateKind(gate_ev.payload["kind"])
    if set(st.open_gates) != {gate_id}:
        raise RuntimeError(f"Gate 2 {gate_id} is not the run's only open gate (open: {sorted(st.open_gates) or 'none'}); "
                           f"a decided gate is not recovered")
    stage_before = next((ev.payload.get("stage") for ev in reversed(events) if ev.seq < gate_ev.seq and ev.kind == "stage.entered"), None)
    if stage_before != RunStage.GATE_2_SPEC_READINESS.value:
        raise RuntimeError(f"Gate 2 {gate_id} was opened outside the Gate 2 stage ({stage_before})")
    after = [ev for ev in events if ev.seq > gate_ev.seq]
    for ev in after:
        if ev.kind == "run.failed" and not (ev.payload.get("interrupted_sessions") or []):
            continue
        if ev.kind == "run.resumed" and ev.payload.get("gate_id") == gate_id and ev.payload.get("stage") == RunStage.GATE_2_SPEC_READINESS.value:
            continue
        raise RuntimeError(f"{ev.kind} (event {ev.seq}) follows Gate 2 {gate_id}; only a controller stopped while idle at the gate is recovered")
    if not after or after[-1].kind != "run.failed":
        raise RuntimeError(f"run {ctx.run_id} has no stop recorded after Gate 2 {gate_id}")
    presented = {a["name"]: a["sha256"] for a in gate_ev.payload.get("artifacts", [])}
    sids = [n[:-len(".json")] for n in presented if n != "verdict.json"]
    if "verdict.json" not in presented or not sids or any(not n.endswith(".json") or not _SAFE_SPEC_ID.match(n[:-len(".json")]) for n in presented if n != "verdict.json"):
        raise RuntimeError(f"Gate 2 {gate_id} did not present one judge verdict and the specs as <spec_id>.json")

    def verified(stage: str, name: str, presented_as: str) -> bytes:
        try:
            content = ctx.store.get_artifact(ctx.run_id, stage, name)
        except FileNotFoundError:
            raise RuntimeError(f"{stage}/{name}, presented at Gate 2 {gate_id} as {presented_as}, is missing") from None
        if hashlib.sha256(content).hexdigest() != presented[presented_as]:
            raise RuntimeError(f"{stage}/{name} no longer matches the sha256 Gate 2 {gate_id} presented as {presented_as}; "
                               f"the owner was shown different bytes")
        return content
    specs = {sid: json.loads(verified("specify", f"final-{sid}.json", f"{sid}.json")) for sid in sids}
    verdict = json.loads(verified("judge", "final-verdict.json", "verdict.json"))
    if any(s.get("spec_id") != sid for sid, s in specs.items()):
        raise RuntimeError(f"a spec stored for Gate 2 {gate_id} names a different spec_id")
    if (kind == GateKind.SPEC_READINESS) != ((verdict.get("overall") or {}).get("result") == "pass"):
        raise RuntimeError(f"Gate 2 {gate_id} is {kind.value} but the verdict it presented says {(verdict.get('overall') or {}).get('result')}")
    queued = _queued_gate(ctx.queue, gate_id)
    if queued is None:
        raise RuntimeError(f"the gate surface holds no record of Gate 2 {gate_id}; a decision could not be checked against what it presented")
    if queued.kind != kind or {a.name: a.sha256 for a in queued.artifacts} != presented or set(queued.options) != GATE2_OPTIONS:
        raise RuntimeError(f"the gate surface's record of Gate 2 {gate_id} differs from the gate the run opened")
    revs = _rounds(ctx, "decompose", "r", "-decomposition.json")
    if not revs:
        raise RuntimeError(f"run {ctx.run_id} holds no decomposition")
    decomposition = json.loads(ctx.store.get_artifact(ctx.run_id, "decompose", f"r{max(revs)}-decomposition.json"))
    items = {w["work_item_id"] for w in decomposition["work_items"]}
    if any(s.get("work_item_id") not in items for s in specs.values()):
        raise RuntimeError(f"a spec at Gate 2 {gate_id} belongs to no work item of the run's latest decomposition")
    return PendingGate2(gate_id=gate_id, kind=kind, presented=presented, specs=specs,
                        spec_md={sid: _spec_markdown(ctx, s) for sid, s in specs.items()}, verdict=verdict, decomposition=decomposition)


def stopped_at_gate2(events: list[Any]) -> bool:
    """The run stopped while in the Gate 2 stage (its last stage.entered), whatever `pending_gate2` then finds."""
    last = next((ev.payload.get("stage") for ev in reversed(events) if ev.kind == "stage.entered"), None)
    return last == RunStage.GATE_2_SPEC_READINESS.value


def recorded_preview_mode(events: list[Any]) -> str | None:
    """The preview mode the run was last driven in: run.created's policy, or a later resume that recorded one."""
    mode = None
    for ev in events:
        if ev.kind == "run.created":
            mode = (ev.payload.get("policy") or {}).get("preview_mode", mode)
        elif ev.kind == "run.resumed" and ev.payload.get("preview_mode"):
            mode = ev.payload["preview_mode"]
    return mode


def recovery_config_problems(cfg: Any) -> list[str]:
    """What stops a recovered run from continuing into managed verification: the usual managed checks, plus the approved
    recipe fingerprint, which a recovery must name explicitly (no base-commit fallback for an upgraded controller)."""
    if cfg.preview_mode != "managed":
        return []
    gaps = list(managed_gaps(cfg))
    if not cfg.test_profile_sha256:
        gaps.append("the approved recipe fingerprint (FACTORY_TEST_PROFILE_SHA256 or --test-profile-sha256)")
    return gaps


def preflight(ctx: RunContext, events: list[Any] | None = None, budget_usd: float | None = None) -> None:
    """Refusals `resume` would raise, checked before anything is written (the CLI calls it before the drive, so a
    refusal never records run.failed): a managed run continued without managed verification, and every condition of
    a pending Gate 2 recovery."""
    events = events if events is not None else ctx.store.events(ctx.run_id)
    if recorded_preview_mode(events) == "managed" and ctx.cfg.preview_mode != "managed":
        raise RuntimeError(f"run {ctx.run_id} verifies in a hosted sandbox (preview mode managed); resume it with the managed "
                           f"configuration, never with local or platform execution")
    st = ctx.store.state(ctx.run_id)
    if st.stage == RunStage.FAILED and stopped_at_gate2(events):
        if budget_usd is not None:
            raise RuntimeError("recovering a pending Gate 2 does not change the run budget; resume without --budget")
        pending_gate2(ctx, events)
        problems = recovery_config_problems(ctx.cfg)
        if problems:
            raise RuntimeError("cannot continue into managed verification: " + "; ".join(problems))


def wait_for_gate(ctx: RunContext, gate_id: str, kind: GateKind) -> GateDecision:
    """Poll an already open gate for the owner's decision; each surface validates it against the gate's presented hashes."""
    while True:
        d = gate_decision(ctx, gate_id, kind)
        if d is not None:
            return d
        time.sleep(GATE_POLL_SECONDS)


def _resume_pending_gate2(ctx: RunContext, outcome: dict[str, Any]) -> RunStage:
    """Continue the same run at its pending Gate 2 on this controller: the gate is not reopened, nothing is regenerated,
    no agent runs before the owner decides, and the decision then takes Gate 2's usual path."""
    with controller_lock(ctx.cfg.runs_dir, ctx.run_id):
        preflight(ctx)                                   # again, under the lock
        pending = pending_gate2(ctx)
        _, round_offset = _persisted_specify_rounds(ctx, pending.decomposition)
        ctx.event("run.resumed", {"stage": RunStage.GATE_2_SPEC_READINESS.value, "from": RunStage.FAILED.value,
                                  "gate_id": pending.gate_id, "kind": pending.kind.value, "artifacts": dict(sorted(pending.presented.items())),
                                  "preview_mode": ctx.cfg.preview_mode, "app_id": ctx.cfg.app_id,
                                  "test_profile_sha256": ctx.cfg.test_profile_sha256, "test_vaults": len(ctx.cfg.test_vault_ids)},
                  stage="gate_2")
        return _from_specify(ctx, outcome, pending.decomposition, None, None, round_offset=round_offset, pending=pending)


def resume(ctx: RunContext, budget_usd: float | None = None) -> RunStage:
    """`factory resume <run_id>`: continue a run that stopped before reaching its pull requests.

    Two cases. A run that **failed or was killed during implementation** (the orchestrator crashed or lost a
    session's outputs): nothing before Gate 2 is re-run; the outcome, the approved specs and the judge's verdict
    are read back from the store, and each spec's loop continues at the round whose work the store holds
    (`resume_points`). A run **paused at its budget** (`PAUSED_BUDGET`, see `budget.py`): the owner's decision on
    the budget gate is applied first (reject closes the run; approve, or `budget_usd` from `--budget`, raises the
    cap and records `budget.raised`), then the run re-enters the stage the check interrupted. Implementation
    continues from its artefacts as above; a pause in decomposition or specification re-runs that stage from its
    start with the owner's last gate feedback, because the design stages are cheap and keep no partial state.
    `budget_usd` given for a failed run just replaces the cap for the rest of it.

    A third case: a run whose controller was **stopped while idle at Gate 2** (`run.failed` right after the gate opened,
    no session interrupted). The same run continues at that same gate (`pending_gate2` verifies it and every artefact
    it presented): this controller waits for the owner's decision, then approve, approve_subset, request_changes and
    reject take Gate 2's usual path. This is how a run moves to a newer controller (managed verification included,
    which then needs the approved recipe fingerprint) before anyone approves implementation."""
    st = ctx.store.state(ctx.run_id)
    events = ctx.store.events(ctx.run_id)
    outcome = json.loads(ctx.store.get_artifact(ctx.run_id, "intake", "outcome.json"))
    preflight(ctx, events, budget_usd)
    if st.stage == RunStage.PAUSED_BUDGET:
        return until_budget_pause(lambda: _resume_after_budget_pause(ctx, outcome, st, events, budget_usd))
    if st.stage == RunStage.FAILED and stopped_at_gate2(events):
        return until_budget_pause(lambda: _resume_pending_gate2(ctx, outcome))
    if st.stage not in (RunStage.FAILED, RunStage.IMPLEMENTING):
        raise RuntimeError(f"run {ctx.run_id} is {st.stage.value}; only a run that failed or stopped during implementation, "
                           f"or paused at its budget, can be resumed")
    if budget_usd is not None and budget_usd != st.budget_usd:
        _raise_budget(ctx, st, float(budget_usd), by="cli", gate_id=None)
    return until_budget_pause(lambda: _resume_implementation(ctx, outcome, events, st.stage))


def _resume_implementation(ctx: RunContext, outcome: dict[str, Any], events: list[Any], from_stage: RunStage) -> RunStage:
    if not any(ev.kind == "stage.entered" and ev.payload.get("stage") == RunStage.IMPLEMENTING.value for ev in events):
        raise RuntimeError(f"run {ctx.run_id} never reached the implementation stage; start a new run instead")
    approved = [ev.payload["spec_id"] for ev in events
                if ev.kind == "work_item.stage" and ev.payload.get("stage") == WorkItemStage.SPEC_APPROVED.value]
    specs = {sid: json.loads(ctx.store.get_artifact(ctx.run_id, "specify", f"final-{sid}.json")) for sid in dict.fromkeys(approved)}
    spec_verdict = artifact_json_or_none(ctx, "judge", "final-verdict.json")
    spec_md = {sid: _spec_markdown(ctx, s) for sid, s in specs.items()}
    guidance = approved_record(ctx, events, specs)
    points = resume_points(ctx, specs)
    ctx.event("run.resumed", {"stage": RunStage.IMPLEMENTING.value, "from": from_stage.value,
                              "points": {sid: {"round": pt.round_no, "implementer_done": pt.report is not None} for sid, pt in points.items()}},
              stage="implement")
    return implement_and_integrate(ctx, outcome, specs, spec_md, spec_verdict, resume=points, guidance=guidance)


# ---------------------------------------------------------------- budget pauses

def budget_decision(ctx: RunContext, st: Any, events: list[Any], budget_usd: float | None = None
                    ) -> tuple[str, float | None, GateDecision | None]:
    """How a run paused at its budget continues: ("close", None, decision) when the owner rejected the budget gate,
    ("raise", new_budget, decision_or_None) when they approved it with a new cap or `budget_usd` was given (the flag
    wins over the comment). Raises RuntimeError with the message for the human when nothing decides it yet; the CLI
    calls this before touching the run so a missing decision is a refusal, not a failed run."""
    pause = last_pause(events)
    if pause is None:
        raise RuntimeError(f"run {ctx.run_id} is paused_budget but its log has no budget.reached event")
    gate_id = pause.get("gate_id")
    decision: GateDecision | None = None
    if gate_id and gate_id in st.open_gates:
        try:
            decision = ctx.queue.decision(gate_id)
        except ValueError as e:                      # recorded, but unusable: an approve without the new budget
            raise RuntimeError(f"the decision on gate {gate_id} cannot be applied: {e}") from e
    if decision is not None and decision.decision == Decision.REJECT:
        return "close", None, decision
    new = budget_usd
    if new is None and decision is not None:
        new = parse_budget(decision.comments)
    if new is None and gate_id and gate_id not in st.open_gates and st.budget_usd and st.budget_usd > float(pause["budget_usd"]):
        new = st.budget_usd                          # decided and raised already; the previous resume died before continuing
    if new is None:
        raise RuntimeError(f"run {ctx.run_id} is paused at its budget (${pause['spent_usd']:.2f} spent of ${pause['budget_usd']:.2f}; "
                           f"the next step needs ${pause['needed_usd']:.2f}): approve gate {gate_id} with `budget_usd: N`, "
                           f"reject it to close the run, or pass --budget N")
    if float(new) <= float(pause["spent_usd"]):
        raise RuntimeError(f"a budget of ${float(new):.2f} is not above what the run has already spent (${pause['spent_usd']:.2f})")
    return "raise", float(new), decision


def _raise_budget(ctx: RunContext, st: Any, new_budget: float, by: str, gate_id: str | None) -> None:
    previous = st.budget_usd if st.budget_usd is not None else ctx.policy.run_budget_usd
    ctx.policy = dataclasses.replace(ctx.policy, run_budget_usd=new_budget)
    ctx.event("budget.raised", {"budget_usd": new_budget, "previous_budget_usd": previous, "spent_usd": round(st.list_cost_usd, 2),
                                "gate_id": gate_id, "decided_by": by}, stage="budget", actor="human")


def _resume_after_budget_pause(ctx: RunContext, outcome: dict[str, Any], st: Any, events: list[Any],
                               budget_usd: float | None) -> RunStage:
    pause = last_pause(events)
    action, new_budget, decision = budget_decision(ctx, st, events, budget_usd)
    gate_id = pause.get("gate_id")
    by = decision.decided_by if decision is not None else "cli"
    if gate_id and gate_id in st.open_gates:
        ctx.event("gate.decided", {"gate_id": gate_id, "kind": GateKind.BUDGET.value,
                                   "decision": Decision.REJECT.value if action == "close" else Decision.APPROVE.value,
                                   "comments": decision.comments if decision is not None else f"budget_usd: {new_budget:g}",
                                   "approved_subset": [], "artifact_shas": list(decision.artifact_shas) if decision is not None else [],
                                   "decided_by": by}, stage="budget", actor="human")
    if action == "close":
        ctx.event("stage.entered", {"stage": RunStage.CLOSED.value}, stage="budget", actor="human")
        return RunStage.CLOSED
    if st.budget_usd is None or new_budget != st.budget_usd:
        _raise_budget(ctx, st, new_budget, by=by, gate_id=gate_id)
    else:
        ctx.policy = dataclasses.replace(ctx.policy, run_budget_usd=new_budget)
    paused_in = RunStage(pause.get("stage") or pause.get("run_stage"))
    events = ctx.store.events(ctx.run_id)
    if paused_in == RunStage.IMPLEMENTING:
        return _resume_implementation(ctx, outcome, events, from_stage=RunStage.PAUSED_BUDGET)
    if paused_in == RunStage.DECOMPOSING:
        feedback, prior = _pending_gate1_feedback(ctx, events)
        ctx.event("run.resumed", {"stage": RunStage.DECOMPOSING.value, "from": RunStage.PAUSED_BUDGET.value,
                                  "with_feedback": feedback is not None}, stage="decompose")
        return _from_decompose(ctx, outcome, feedback, prior)
    if paused_in == RunStage.SPECIFYING:
        revs = _rounds(ctx, "decompose", "r", "-decomposition.json")
        if not revs:
            raise RuntimeError(f"run {ctx.run_id} paused while specifying but holds no decomposition; start a new run instead")
        decomposition = json.loads(ctx.store.get_artifact(ctx.run_id, "decompose", f"r{max(revs)}-decomposition.json"))
        human_comments, prior_specs = _pending_specify_inputs(ctx, events, decomposition)
        previous_verdict, round_offset = _persisted_specify_rounds(ctx, decomposition)
        ctx.event("run.resumed", {"stage": RunStage.SPECIFYING.value, "from": RunStage.PAUSED_BUDGET.value,
                                  "with_feedback": prior_specs is not None}, stage="specify")
        return _from_specify(ctx, outcome, decomposition, human_comments, prior_specs, previous_verdict, round_offset)
    raise RuntimeError(f"run {ctx.run_id} paused in {paused_in.value}, which has no resume path")


def _persisted_specify_rounds(ctx: RunContext, decomposition: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
    """What a resumed specification stage continues from: the last judge verdict in the store (the writers revise
    against its issues, the judge sees it as previous_verdict.json) and the highest round any writer draft or verdict
    already carries. The store never overwrites an artefact, so the stage picks up at the next round."""
    judged = _rounds(ctx, "judge", "r", "-verdict.json")
    rounds = list(judged)
    for w in decomposition["work_items"]:
        rounds += _rounds(ctx, "specify", f"{w['work_item_id']}-r", "-spec.json")
    verdict = artifact_json_or_none(ctx, "judge", f"r{max(judged)}-verdict.json") if judged else None
    return verdict, max(rounds, default=0)


def _pending_gate1_feedback(ctx: RunContext, events: list[Any]) -> tuple[str | None, dict[str, Any] | None]:
    """When the pause interrupted a re-decomposition after request-changes at Gate 1: the owner's feedback (gate
    comments plus design-CR and tracker comments, as `_feedback` builds it) and the decomposition it was about."""
    last_dec = next((ev for ev in reversed(events) if ev.kind == "artifact.produced"
                     and ev.payload.get("artifact_type") == "decomposition" and "work_items" in ev.payload), None)
    last_gate = next((ev for ev in reversed(events) if ev.kind == "gate.decided"
                      and ev.payload.get("kind") == GateKind.DECOMPOSITION_REVIEW.value), None)
    if last_dec is None or last_gate is None or last_gate.seq < last_dec.seq \
            or last_gate.payload.get("decision") != Decision.REQUEST_CHANGES.value:
        return None, None
    prior = json.loads(ctx.store.get_artifact(ctx.run_id, "decompose", f"r{last_dec.payload['revision']}-decomposition.json"))
    return _feedback(ctx, _design_cr_id(ctx), _decision_from_event(last_gate.payload)), prior


def _pending_specify_inputs(ctx: RunContext, events: list[Any], decomposition: dict[str, Any]
                            ) -> tuple[dict[str, str] | None, dict[str, dict] | None]:
    """What the spec writers were about to receive when the pause hit: the Gate 1 approval notes on a first pass,
    or the routed Gate 2 request-changes feedback with the drafts it was about."""
    last_gate = next((ev for ev in reversed(events) if ev.kind == "gate.decided"), None)
    if last_gate is None:
        return None, None
    p = last_gate.payload
    if p.get("kind") == GateKind.DECOMPOSITION_REVIEW.value and p.get("decision") == Decision.APPROVE.value:
        notes = notes_by_item(p.get("comments", ""), [w["work_item_id"] for w in decomposition["work_items"]])
        return (notes or None), None
    if p.get("kind") in (GateKind.SPEC_READINESS.value, GateKind.ESCALATION.value) and p.get("decision") == Decision.REQUEST_CHANGES.value:
        opened = next((ev for ev in reversed(events) if ev.kind == "gate.opened" and ev.payload.get("gate_id") == p.get("gate_id")), None)
        names = [a["name"] for a in (opened.payload.get("artifacts", []) if opened else [])]
        sids = [n[:-len(".json")] for n in names if n.endswith(".json") and n != "verdict.json"]
        specs = {sid: json.loads(ctx.store.get_artifact(ctx.run_id, "specify", f"final-{sid}.json")) for sid in sids}
        if specs:
            return _route_gate2_feedback(ctx, _design_cr_id(ctx), _decision_from_event(p), specs)
    return None, None
