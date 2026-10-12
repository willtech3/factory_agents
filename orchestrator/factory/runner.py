"""Top-level run loop: wires the stages together and handles gate decisions.

The run is a pipeline of durable steps (ADR-0013, `steps.py`): each decomposition, gate, specification pass, approval,
implementation and integration records its result. `run` starts the pipeline; `resume` runs the same pipeline again
from the top, and every step that finished returns its recorded result, so the run continues at the first step that
did not: a session still working is followed, a gate still open is waited on, a decided gate's decision stands. There
is no resume path per stage.

Everything a human sees in the Factory Console is derived from the events the steps append.

Gate presentation depends on `cfg.gate_surface` (ADR-0007):
- every surface gets an artifact page per gate (`surfaces/artifact_pages.py`) when `ctx.pages` is set;
- nothing is committed to the target repository and no change request is opened before Gate 3 (ADR-0011);
- `tracker`: `ctx.tracker` (a `TrackerQueue`, also part of `ctx.queue`) opens the gate
  on the run's tracker issue and harvests the decision and comments from there;
- `web`: pages plus the CLI's file queue, which the Factory Console reads and writes.
"""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import json
import re
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from . import steps
from .approvals import approval_guidance, route_lines, subset_problems
from .budget import BudgetPaused, last_pause
from .config import managed_gaps
from .gates import Decision, GateDecision, GateKind, parse_budget
from .stages import RunContext
from .stages.common import artifact_json_or_none, decision_from_event, orphaned_sessions, public, queued_gate
from .stages.common import hosted_run, recorded_preview_mode  # noqa: F401  the Console imports recorded_preview_mode from here
from .stages.decompose import gate_1, run_decompose
from .stages.implement import LocalVerifier, ResumePoint, run_implement_one
from .stages.integrate import run_integrate
from .stages.specify import gate_2, run_specify
from .state import RunStage, RunState, WorkItemStage, reduce
from .surfaces.artifact_pages import render_gate1_page, render_gate2_page


def new_run_id() -> str:
    return f"run_{uuid.uuid4().hex[:10]}"


def _present(ctx: RunContext, files: dict[str, bytes], page_name: str, page_html: str,
             anchors: dict[str, str]) -> dict[str, str]:
    """Publish the gate's reading page. Returns artifact_name -> url (empty without a page publisher). Gates 1 and 2
    are read here and decided on the configured surface; nothing is committed to the target repository (ADR-0011)."""
    if ctx.pages is None:
        return {}
    page_url = ctx.pages.publish(ctx.run_id, page_name, page_html)
    ctx.event("artifact.produced", {"artifact_type": "gate_page", "path": f"pages/{page_name}.html", "url": page_url},
              stage=page_name)
    return {name: f"{page_url}#{anchors.get(name, 'summary')}" for name in files}


def policy_snapshot(ctx: RunContext) -> dict[str, Any]:
    """What this run was tuned with, recorded on run.created so the Console can show it beside the run."""
    p = ctx.policy
    return {"run_budget_usd": p.run_budget_usd, "reviewer_passes": p.reviewer_passes, "review_max_rounds": p.review_max_rounds,
            "auto_approve": list(getattr(p, "auto_approve", ()) or ()),
            "review_single_pass_lines": getattr(p, "review_single_pass_lines", None),
            "reviewer_passes_followup": getattr(p, "reviewer_passes_followup", None),
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
    general, per_item = route_lines(comments, {i: i for i in item_ids})
    out = {}
    for i in item_ids:
        parts = general + per_item.get(i, [])
        if parts:
            out[i] = "# Notes from the owner at Gate 1\n" + "\n".join(f"- {p}" for p in parts)
    return out


def _feedback(ctx: RunContext, d: GateDecision) -> str:
    parts = [d.comments] if d.comments.strip() else []
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
    return until_budget_pause(lambda: pipeline(ctx, outcome))


def until_budget_pause(step: Callable[[], RunStage]) -> RunStage:
    """Run a pipeline step; a budget pause ends the process cleanly in PAUSED_BUDGET. By the time `BudgetPaused`
    reaches here the run is already parked (`budget.reached`), the budget gate is open on the gate surface, and no
    session is in flight (`budget.py`), so there is nothing to fail or interrupt."""
    try:
        return step()
    except BudgetPaused as e:
        print(f"[budget] {e}")
        return RunStage.PAUSED_BUDGET


# ---------------------------------------------------------------- the pipeline, as durable steps

def pipeline(ctx: RunContext, outcome: dict[str, Any]) -> RunStage:
    """The run from the outcome to its change requests. Between steps it only computes from earlier steps' results, so
    running it again (`resume`) reaches the same steps with the same inputs and gets their recorded results back."""
    decomposed = _decomposition_approved(ctx, outcome)
    if decomposed is None:
        return RunStage.CLOSED
    decomposition, notes = decomposed
    contracts = _contracts_approved(ctx, outcome, decomposition, notes)
    if contracts is None:
        return RunStage.CLOSED
    specs, spec_md, verdict, approval = contracts
    if ctx.policy.design_only:
        ctx.event("run.completed", {"design_only": True, "approved_specs": sorted(specs),
                                    "note": "design-only run: no implementation stage was requested"}, stage="gate_2")
        return RunStage.COMPLETED
    _check_approved_contracts(ctx, approval)
    return implement_and_integrate(ctx, outcome, specs, spec_md, verdict, guidance=approval["guidance"])


def _decomposition_approved(ctx: RunContext, outcome: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str] | None] | None:
    """Stage 1 and Gate 1, one step each per pass, until the owner approves (the decomposition and their notes per work
    item) or rejects (None). A request for changes sends the next pass their feedback and the decomposition it was about."""
    feedback: str | None = None
    prior: dict[str, Any] | None = None
    n = 0
    while True:
        n += 1
        decomposition, shas = _decompose_step(ctx, outcome, n, feedback, prior)
        gate = _gate_1_step(ctx, outcome, n, decomposition, shas)
        decision = gate["decision"]["decision"]
        if decision == Decision.APPROVE.value:
            return decomposition, gate["notes"] or None
        if decision == Decision.REJECT.value:
            _close(ctx, "gate_1")
            return None
        feedback, prior = gate["feedback"], decomposition


def _decompose_step(ctx: RunContext, outcome: dict[str, Any], n: int, feedback: str | None,
                    prior: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, str]]:
    def body() -> dict[str, Any]:
        decomposition, shas = run_decompose(ctx, outcome, feedback, prior)
        if ctx.tracker is not None:
            ctx.tracker.register_work_items(ctx.run_id, decomposition)
        return {"decomposition": steps.put_json(ctx, decomposition), "shas": shas}
    result = steps.run_step(ctx, f"decompose#{n}", {"outcome": _sha(outcome), "feedback": feedback, "prior": _sha(prior)}, body)
    return steps.get_json(ctx, result["decomposition"]), result["shas"]


def _gate_1_step(ctx: RunContext, outcome: dict[str, Any], n: int, decomposition: dict[str, Any],
                 shas: dict[str, str]) -> dict[str, Any]:
    """Gate 1 on pass `n`'s decomposition. The result keeps what the next step needs from the decision: the feedback as
    the owner wrote it then (tracker comments can change later), or the approval notes per work item."""
    def body() -> dict[str, Any]:
        dec_md = ctx.store.get_artifact(ctx.run_id, "decompose", f"r{decomposition['revision']}-decomposition.md")
        files = {"decomposition.md": dec_md, "decomposition.json": json.dumps(decomposition, indent=2).encode()}
        page = render_gate1_page(ctx.run_id, outcome, decomposition, dec_md.decode(errors="replace"), shas)
        d = gate_1(ctx, outcome, decomposition, shas,
                   lambda: _present(ctx, files, "gate_1", page, {"decomposition.md": "full", "decomposition.json": "work-items"}))
        return {"decision": _decision_record(d),
                "feedback": _feedback(ctx, d) if d.decision == Decision.REQUEST_CHANGES else None,
                "notes": notes_by_item(d.comments, [w["work_item_id"] for w in decomposition["work_items"]])
                if d.decision == Decision.APPROVE else None}
    return steps.run_step(ctx, f"gate_1#{n}", {"decomposition": _sha(decomposition), "shas": shas}, body, kind="gate")


def _contracts_approved(ctx: RunContext, outcome: dict[str, Any], decomposition: dict[str, Any], notes: dict[str, str] | None
                        ) -> tuple[dict[str, dict], dict[str, str], dict[str, Any], dict[str, Any]] | None:
    """Stage 2 and Gate 2, one step each per pass, until the owner approves (the approved specs, their spec.md, the
    judge's verdict and the approval) or rejects (None). Rounds keep counting across passes, so no artefact name the
    owner decided on is reused; a send-back gives the writers the routed feedback and the drafts it was about, and the
    first judge of the next pass the verdict the owner saw."""
    human_comments: dict[str, str] | None = notes
    prior_specs: dict[str, dict] | None = None
    previous_verdict: dict[str, Any] | None = None
    round_offset = 0
    n = 0
    while True:
        n += 1
        written = _specify_step(ctx, outcome, decomposition, n, human_comments, prior_specs, previous_verdict, round_offset)
        specs, verdict, spec_md = written["specs"], written["verdict"], written["spec_md"]
        gate = _gate_2_step(ctx, outcome, n, written)
        d = decision_from_event(gate["decision"])
        if d.decision == Decision.REJECT:
            _close(ctx, "gate_2")
            return None
        if d.decision in (Decision.APPROVE, Decision.APPROVE_SUBSET):
            approval = _approve_step(ctx, n, d, specs)
            selected = {sid: s for sid, s in specs.items() if sid in approval["approved"]}
            return selected, spec_md, verdict, approval
        human_comments, prior_specs = _route_gate2_feedback(gate["feedback"], specs)
        previous_verdict, round_offset = verdict, max(int(verdict.get("round") or round_offset + 1), round_offset)


def _specify_step(ctx: RunContext, outcome: dict[str, Any], decomposition: dict[str, Any], n: int,
                  human_comments: dict[str, str] | None, prior_specs: dict[str, dict] | None,
                  previous_verdict: dict[str, Any] | None, round_offset: int) -> dict[str, Any]:
    """Pass `n` of the writers and the judge. The result is the specs (with each writer's spec.md), the verdict, and the
    spec.md per spec, kept as one blob."""
    def body() -> str:
        specs, verdict = run_specify(ctx, outcome, decomposition, human_comments, prior_specs,
                                     previous_verdict=previous_verdict, round_offset=round_offset)
        return steps.put_json(ctx, {"specs": specs, "verdict": verdict,
                                    "spec_md": {sid: _spec_markdown(ctx, s) for sid, s in specs.items()}})
    inputs = {"decomposition": _sha(decomposition), "comments": human_comments, "previous_verdict": _sha(previous_verdict),
              "prior": {w: _sha(public(s)) for w, s in sorted((prior_specs or {}).items())}, "round_offset": round_offset}
    blob = steps.run_step(ctx, f"specify#{n}", inputs, body)
    return {**steps.get_json(ctx, blob), "blob": blob}


def _gate_2_step(ctx: RunContext, outcome: dict[str, Any], n: int, written: dict[str, Any]) -> dict[str, Any]:
    """Gate 2 on pass `n`'s specs: publish them as the gate's `final-*` artefacts, present them, wait for the decision."""
    specs, verdict, spec_md = written["specs"], written["verdict"], written["spec_md"]

    def body() -> dict[str, Any]:
        # `final-*` point at the current candidate for the gate (replaced on every pass); the per-round artefacts the
        # owner decided on keep their names and are never overwritten.
        spec_shas = {f"{sid}.json": ctx.store.put_artifact(ctx.run_id, "specify", f"final-{sid}.json",
                                                            json.dumps(public(s), indent=2).encode(), replace=True)
                     for sid, s in specs.items()}
        for sid, md in spec_md.items():
            if md:
                ctx.store.put_artifact(ctx.run_id, "specify", f"final-{sid}.md", md.encode(), replace=True)
        spec_shas["verdict.json"] = ctx.store.put_artifact(ctx.run_id, "judge", "final-verdict.json",
                                                           json.dumps(verdict, indent=2).encode(), replace=True)

        def urls() -> dict[str, str]:
            files = {**{f"specs/{sid}.json": json.dumps(public(s), indent=2).encode() for sid, s in specs.items()},
                     **{f"specs/{sid}.md": spec_md[sid].encode() for sid in specs if spec_md.get(sid)},
                     "verdict.json": json.dumps(verdict, indent=2).encode()}
            page = render_gate2_page(ctx.run_id, outcome, {sid: public(s) for sid, s in specs.items()}, verdict, spec_shas, spec_md)
            anchors = {f"specs/{sid}.json": f"spec-{sid}" for sid in specs}
            anchors.update({f"specs/{sid}.md": f"spec-{sid}" for sid in specs})
            published = _present(ctx, files, "gate_2", page, anchors)
            return {**published, **{f"{sid}.json": published.get(f"specs/{sid}.json", "") for sid in specs},
                    "verdict.json": published.get("verdict.json", "")}
        d = gate_2(ctx, outcome, specs, verdict, spec_shas, urls)
        return {"decision": _decision_record(d), "feedback": _feedback(ctx, d) if d.decision == Decision.REQUEST_CHANGES else None}
    return steps.run_step(ctx, f"gate_2#{n}", {"specs": written["blob"]}, body, kind="gate")


def _approve_step(ctx: RunContext, n: int, d: GateDecision, specs: dict[str, dict]) -> dict[str, Any]:
    """Record what Gate 2 approved: each approved spec's sha256, the owner's bounded notes per spec, and the work items'
    move to spec_approved. A selection that is not dependency-valid never starts any implementation."""
    def body() -> dict[str, Any]:
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
        hashes = {sid: hashlib.sha256(ctx.store.get_artifact(ctx.run_id, "specify", f"final-{sid}.json")).hexdigest()
                  for sid in sorted(approved)}
        return {"gate_id": d.gate_id, "approved": hashes, "guidance": guidance}
    return steps.run_step(ctx, f"approve#{n}", {"gate": d.gate_id}, body)


def _check_approved_contracts(ctx: RunContext, approval: dict[str, Any]) -> None:
    """Nothing is built from a spec whose stored bytes differ from what Gate 2 approved."""
    for sid, sha in approval["approved"].items():
        if hashlib.sha256(ctx.store.get_artifact(ctx.run_id, "specify", f"final-{sid}.json")).hexdigest() != sha:
            raise RuntimeError(f"specify/final-{sid}.json no longer matches the spec Gate 2 {approval['gate_id']} approved")


def _close(ctx: RunContext, stage: str) -> None:
    if ctx.store.state(ctx.run_id).stage != RunStage.CLOSED:
        ctx.event("stage.entered", {"stage": RunStage.CLOSED.value}, stage=stage, actor="human")


def _decision_record(d: GateDecision) -> dict[str, Any]:
    return {"gate_id": d.gate_id, "decision": d.decision.value, "decided_by": d.decided_by, "comments": d.comments,
            "approved_subset": list(d.approved_subset), "artifact_shas": list(d.artifact_shas)}


def _sha(value: Any) -> str | None:
    return None if value is None else steps.inputs_sha(value)


def record_approval(ctx: RunContext, d: GateDecision, approved: dict[str, dict]) -> dict[str, str]:
    """Keep what Gate 2 approved and the owner's notes with it: the sha256 of each approved spec as stored for the gate
    (`specify/final-<spec_id>.json`) and the bounded per-spec guidance from the decision's comments. The implementer gets
    the guidance; implementation refuses a spec whose bytes no longer match what was approved."""
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


def _route_gate2_feedback(feedback: str, specs: dict[str, dict]) -> tuple[dict[str, str], dict[str, dict]]:
    """request_changes at Gate 2: per-item lines (`spec_x: ...` or `wi-x: ...`) go to that item, the rest to every
    item; the writers also get the draft the owner commented on."""
    ids = [s["work_item_id"] for s in specs.values()] + list(specs)
    routed = notes_by_item(feedback, ids)
    human_comments = {s["work_item_id"]: (routed.get(s["work_item_id"]) or routed.get(sid) or feedback) for sid, s in specs.items()}
    prior_specs = {s["work_item_id"]: s for s in specs.values()}
    return human_comments, prior_specs


def _spec_markdown(ctx: RunContext, spec: dict[str, Any]) -> str:
    """The writer's spec.md for this spec: carried on the object by `run_specify`, else the `final-<spec_id>.md` the
    runner stored at Gate 2 (what `factory resume` reads; absent when the markdown was empty), else ''."""
    if spec.get("_markdown"):
        return str(spec["_markdown"])
    try:
        return ctx.store.get_artifact(ctx.run_id, "specify", f"final-{spec['spec_id']}.md").decode(errors="replace")
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
    """Stage 3 + 4 per approved spec, in dependency waves, each spec an implementation step and an integration step.
    A replay gets the specs that finished back from their steps; a spec whose implementation did not finish continues
    where its artefacts stop (`resume_points`). Specs whose change request is already open are not touched again.
    `guidance` is the owner's bounded Gate 2 approval notes per spec (`record_approval`), handed to that spec's
    implementer. `resume` overrides the resume points (tests)."""
    repo = outcome["target_repositories"][0]
    if ctx.store.state(ctx.run_id).stage != RunStage.IMPLEMENTING:
        ctx.event("stage.entered", {"stage": RunStage.IMPLEMENTING.value}, stage="implement")
    auth_url = ctx.forge.authenticated_url(repo["url"]) if ctx.forge is not None else repo["url"]
    verifier = LocalVerifier(auth_url, ctx.cfg.runs_dir / ctx.run_id / "verify")
    default_branch = comparison_branch(ctx, repo, verifier)
    cr_by_spec: dict[str, int] = _open_change_requests(ctx)                       # finished before a resume
    merged_or_open: set[str] = set(cr_by_spec)
    stopped: dict[str, str] = {}                                                  # spec -> why it did not reach a change request
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
            verdict = _implement_step(ctx, spec, repo["url"], cr_by_spec, default_branch, verifier, deps,
                                      spec_md.get(sid, ""), (guidance or {}).get(sid, ""), (resume or {}).get(sid))
            remaining.pop(sid)
            if verdict["result"] != "ready":
                stopped[sid] = str(verdict.get("reason") or verdict["result"])
                continue
            cr = _integrate_step(ctx, outcome, spec, verdict, spec_md.get(sid, ""), spec_verdict, verifier)
            cr_by_spec[sid] = int(cr.get("id", cr.get("iid")))
            merged_or_open.add(sid)   # Gate 3 (human review + merge) is tracked by the CR watcher, not this loop

    if stopped or ctx.store.state(ctx.run_id).stage == RunStage.ESCALATED:
        if ctx.store.state(ctx.run_id).stage != RunStage.ESCALATED:
            # every escalation came back from a recorded step, which reported it when it ran: the run waits again
            sid = sorted(stopped)[0]
            ctx.event("run.escalated", {"reason": f"{sid}: {stopped[sid]}"}, spec_id=sid)
        from . import build_recovery
        recovery = build_recovery.snapshot(ctx.store, ctx.run_id)
        if recovery:
            build_recovery.open_gate(ctx.store, ctx.queue, ctx.run_id, recovery)
        return RunStage.ESCALATED          # a spec escalated: the run waits for a person, it has not completed
    ctx.event("run.completed", {"specs_with_change_requests": sorted(merged_or_open)})
    return RunStage.COMPLETED


def _implement_step(ctx: RunContext, spec: dict[str, Any], repo_url: str, cr_by_spec: dict[str, int], default_branch: str,
                    verifier: LocalVerifier, deps: dict[str, dict], spec_md: str, guidance: str,
                    resume_from: ResumePoint | None) -> dict[str, Any]:
    """One spec's implement/review/judge loop as a step; its result is the merge verdict, or why it stopped. The latest
    build-recovery decision on the spec is one of its inputs, so the owner's recovery runs it again: a recheck verifies
    the last head afresh and asks a new merge judge, a request for changes opens the next implementer round with the
    owner's instructions."""
    sid, wid = spec["spec_id"], spec["work_item_id"]
    recovery = _latest_recovery(ctx, sid)
    corrective = _latest_recovery(ctx, sid, Decision.REQUEST_CHANGES.value)   # stays authorized through later rechecks

    def body() -> dict[str, Any]:
        base_branch, blocker = base_branch_for(ctx, spec, cr_by_spec, default_branch)
        if blocker:
            ctx.event("run.escalated", {"reason": f"{sid}: {blocker}"}, spec_id=sid)
            ctx.notifier.escalated(ctx.run_id, f"{sid}: {blocker}")
            return {"result": "blocked", "reason": blocker}
        if base_branch != default_branch:
            ctx.event("artifact.produced", {"artifact_type": "stacked_base", "base_branch": base_branch,
                                            "note": "the dependency's change request is open but not merged; this item is built on its branch"},
                      work_item_id=wid, spec_id=sid, stage="implement")
        point, notes = resume_from, guidance
        item = ctx.store.state(ctx.run_id).work_items.get(wid)
        if point is None and item is not None and item.stage != WorkItemStage.SPEC_APPROVED:
            point = resume_points(ctx, {sid: spec})[sid]           # started before: continue where its artefacts stop
        if corrective is not None:
            n = int(corrective["round"]) + 1
            notes = notes + "\n# Build recovery instructions from the owner\n" + corrective.get("comments", "")
            point = point or ResumePoint()
            if point.round_no < n:
                point = ResumePoint(round_no=n, findings=corrective.get("review_findings") or point.findings)
            point.review_through = n
        verdict = run_implement_one(ctx, spec, repo_url, base_branch, verifier, deps, spec_md=spec_md, resume_from=point,
                                    guidance=notes)
        if verdict["result"] != "ready":
            ctx.event("run.escalated", {"reason": f"{sid}: merge judge said {verdict['result']}",
                                        "summary": verdict.get("summary_for_human", verdict.get("reason"))}, spec_id=sid)
            ctx.notifier.escalated(ctx.run_id, f"{sid}: {verdict['result']}")
        return {**verdict, "_base_branch": base_branch}
    marker = None if recovery is None else {k: recovery.get(k) for k in ("event_id", "revision", "decision", "gate_id")}
    return steps.run_step(ctx, f"implement/{sid}", {"spec": _sha(public(spec)), "guidance": guidance, "recovery": marker}, body)


def _integrate_step(ctx: RunContext, outcome: dict[str, Any], spec: dict[str, Any], verdict: dict[str, Any], spec_md: str,
                    spec_verdict: dict[str, Any] | None, verifier: LocalVerifier) -> dict[str, Any]:
    """Open the change request for the head the merge judge approved."""
    sid = spec["spec_id"]

    def body() -> dict[str, Any]:
        findings = json.loads(ctx.store.get_artifact(ctx.run_id, "review", _latest(ctx, "review", f"{sid}-", "-findings.merged.json")))
        response = _latest_json_or(ctx, "implement", f"{sid}-", "-review_response.json", {"responses": []})
        test_results = json.loads(ctx.store.get_artifact(ctx.run_id, "implement", _latest(ctx, "implement", f"{sid}-", "-test_results.json")))
        sessions = _sessions_for(ctx, sid, spec["work_item_id"])
        summary = {"run_id": ctx.run_id, "outcome_title": outcome["title"], "reviewers": [outcome["owner"]["handle"]],
                   "sessions": sessions, "list_cost_usd": ctx.store.state(ctx.run_id).list_cost_usd,
                   "sessions_cost_usd": round(sum(s["list_cost_usd"] or 0.0 for s in sessions), 2),
                   "sessions_duration_seconds": sum(s["duration_seconds"] or 0 for s in sessions),
                   "sessions_duration": _hms(sum(s["duration_seconds"] or 0 for s in sessions))}
        judged = {k: v for k, v in verdict.items() if k != "_base_branch"}
        return run_integrate(ctx, spec, judged, findings, response, test_results, summary, verdict["_base_branch"],
                             verifier=verifier, spec_md=spec_md, spec_verdict=spec_verdict)
    return steps.run_step(ctx, f"integrate/{sid}", {"head": verdict.get("_head_sha"), "base": verdict.get("_base_branch")}, body)


def _latest_recovery(ctx: RunContext, sid: str, decision: str | None = None) -> dict[str, Any] | None:
    """The owner's latest build-recovery decision on this spec (`build_recovery.apply_decision`), of `decision` when
    given, if any."""
    return next((e.payload for e in reversed(ctx.store.events(ctx.run_id))
                 if e.kind == "build.recovery" and e.spec_id == sid and decision in (None, e.payload.get("decision"))), None)


def _open_change_requests(ctx: RunContext) -> dict[str, int]:
    """spec_id -> change request id for every change request this run has opened."""
    out: dict[str, int] = {}
    for ev in ctx.store.events(ctx.run_id):
        if ev.kind == "cr.opened" and ev.spec_id:
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
            correction = next((e.payload for e in reversed(ctx.store.events(ctx.run_id))
                               if e.kind == "build.recovery" and e.spec_id == sid
                               and e.payload.get("decision") == "request_changes" and e.payload.get("round", 0) + 1 == n), None)
            if correction:
                findings = correction.get("review_findings") or {"findings": []}
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


# ---------------------------------------------------------------- one controller per run

_HELD_LOCKS: dict[str, Any] = {}


class RunLocked(RuntimeError):
    """Another controller holds the run's lock."""


@contextlib.contextmanager
def controller_lock(runs_dir: Path, run_id: str) -> Iterator[None]:
    """A nonblocking exclusive lock on `runs/<run_id>/controller.lock`, held for the whole resumed drive, so two
    recovering controllers never wait on (and consume) the same pending gate. Re-entrant within one process: the CLI
    takes it before the drive and `resume` finds it held. Raises `RunLocked`, before anything is written, when another
    process holds it."""
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



# ---------------------------------------------------------------- resume: decide what is pending, then replay

RESUMABLE = {RunStage.FAILED, RunStage.PAUSED_BUDGET, RunStage.INTAKE, RunStage.DECOMPOSING, RunStage.GATE_1_DECOMPOSITION,
             RunStage.SPECIFYING, RunStage.GATE_2_SPEC_READINESS, RunStage.IMPLEMENTING}


def stage_to_continue(st: Any, events: list[Any]) -> RunStage:
    """The stage a resumed run re-enters: the last one it worked in before it failed or paused at a budget (the run's
    cap, or one session's cap while its controller waited on that gate), implementation for a recovered build, or the
    stage it is in when its controller was killed before it could record anything. The replayed pipeline moves on from
    there with its own `stage.entered` events."""
    if st.stage == RunStage.ESCALATED:
        return RunStage.IMPLEMENTING                 # a recovered build (`resumable_build_escalation`)
    if st.stage not in (RunStage.FAILED, RunStage.PAUSED_BUDGET):
        return st.stage
    state, stage = RunState(run_id=st.run_id), RunStage.INTAKE
    for ev in events:
        reduce(state, ev)
        if state.stage not in (RunStage.FAILED, RunStage.PAUSED_BUDGET):
            stage = state.stage
    return stage


def stopped_at_gate2(events: list[Any]) -> bool:
    """The run stopped while in the Gate 2 stage (its last stage.entered)."""
    last = next((ev.payload.get("stage") for ev in reversed(events) if ev.kind == "stage.entered"), None)
    return last == RunStage.GATE_2_SPEC_READINESS.value


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
    refusal never records run.failed): a run that has ended or escalated outside a build, a managed run continued
    without managed verification, a pending build-recovery decision that cannot be applied, and a run an older
    controller left in a state its log cannot be replayed from (`adopt_legacy_run`)."""
    events = events if events is not None else ctx.store.events(ctx.run_id)
    st = ctx.store.state(ctx.run_id)
    if hosted_run(events) and ctx.cfg.preview_mode != "managed":
        raise RuntimeError(f"run {ctx.run_id} verifies in a hosted sandbox (preview mode managed); resume it with the managed "
                           f"configuration, never with local or platform execution")
    if any(g.get("kind") == "build_recovery" for g in st.open_gates.values()):
        from . import build_recovery
        try:
            if not build_recovery.snapshot(ctx.store, ctx.run_id):
                raise ValueError("The pending build recovery gate needs a human decision before resuming.")
            build_recovery.apply_decision(ctx.store, ctx.queue, ctx.run_id, dry_run=True)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
    if st.stage not in RESUMABLE and not resumable_build_escalation(st, events):
        raise RuntimeError(f"run {ctx.run_id} is {st.stage.value}; a run that has ended cannot be resumed, and an escalated one "
                           f"only after an approved build's recovery decision: only a stopped, failed, paused or recovered run "
                           f"can be resumed")
    if st.stage == RunStage.FAILED and stopped_at_gate2(events):
        problems = recovery_config_problems(ctx.cfg)
        if problems:
            raise RuntimeError("cannot continue into managed verification: " + "; ".join(problems))
    _check_gate_records(ctx, st)
    legacy_adoption(ctx, events)          # raises when an older controller's log cannot be continued; writes nothing
    approvals = [s for s in ctx.store.steps(ctx.run_id) if s.key.startswith("approve#") and s.status == "done"]
    if approvals:
        _check_approved_contracts(ctx, max(approvals, key=lambda s: int(s.key.split("#")[1])).result)


def _check_gate_records(ctx: RunContext, st: Any) -> None:
    """Every open gate the replay may wait on again must be held by the gate surface as the log opened it: a decision is
    checked against the surface's record, so a record that differs could pass a decision on something else."""
    for gate_id, opened in st.open_gates.items():
        if opened.get("kind") in (GateKind.BUDGET.value, GateKind.BUILD_RECOVERY.value):
            continue
        held = queued_gate(ctx.queue, gate_id)
        if held is None:
            continue
        logged = {a["name"]: a["sha256"] for a in opened.get("artifacts", [])}
        options = opened.get("options")
        if (held.kind.value != opened.get("kind") or {a.name: a.sha256 for a in held.artifacts} != logged
                or (options is not None and sorted(o.value for o in held.options) != sorted(options))):
            raise RuntimeError(f"the gate surface's record of gate {gate_id} differs from the gate the run opened; a decision "
                               f"could not be checked against what it presented")


def resume(ctx: RunContext, budget_usd: float | None = None) -> RunStage:
    """`factory resume <run_id>`: continue a run that stopped, failed, paused at its budget, or escalated in a build the
    owner has since decided to recover.

    Whatever is pending is decided first: the build-recovery decision is applied (reject closes the run), a budget pause
    is settled from its gate or `budget_usd` (reject closes the run, otherwise the cap is raised), and `budget_usd` for a
    run that did not pause replaces its cap. Then `run.resumed` returns the run to the stage it stopped in and the
    pipeline runs again from the top: every step that finished returns its recorded result, so the run continues at the
    first step that did not. A session still working is followed, a gate still open is waited on (none is opened twice),
    and a build recovery runs the recovered spec's implementation again: verification is fresh, and an escalation
    verdict never stands in for the recovery decision.

    The run's controller lock is held for the whole drive, so two controllers never replay (and wait on) the same run;
    another process holding it is a `RunLocked` refusal before anything is written."""
    with controller_lock(ctx.cfg.runs_dir, ctx.run_id):
        return _resume(ctx, budget_usd)


def _resume(ctx: RunContext, budget_usd: float | None) -> RunStage:
    st = ctx.store.state(ctx.run_id)
    events = ctx.store.events(ctx.run_id)
    outcome = json.loads(ctx.store.get_artifact(ctx.run_id, "intake", "outcome.json"))
    preflight(ctx, events, budget_usd)
    from . import build_recovery
    recovery = build_recovery.apply_decision(ctx.store, ctx.queue, ctx.run_id)
    if recovery and recovery["decision"] == "reject":
        return RunStage.CLOSED
    st, events = ctx.store.state(ctx.run_id), ctx.store.events(ctx.run_id)
    preflight(ctx, events, budget_usd)
    adopt_legacy_run(ctx, events)
    stage = stage_to_continue(st, events)
    if st.stage == RunStage.PAUSED_BUDGET and last_pause(events) is not None:
        if not _settle_budget_pause(ctx, st, events, budget_usd):
            return RunStage.CLOSED
    elif budget_usd is not None and budget_usd != st.budget_usd:
        _raise_budget(ctx, st, float(budget_usd), by="cli", gate_id=None)
    ctx.event("run.resumed", {"stage": stage.value, "from": st.stage.value, "sessions_to_follow": orphaned_sessions(ctx),
                              **_pending_gate(ctx), "preview_mode": ctx.cfg.preview_mode, "app_id": ctx.cfg.app_id,
                              "test_profile_sha256": ctx.cfg.test_profile_sha256, "test_vaults": len(ctx.cfg.test_vault_ids)},
              stage=_EVENT_STAGE.get(stage, stage.value))
    return until_budget_pause(lambda: pipeline(ctx, outcome))


_EVENT_STAGE = {RunStage.DECOMPOSING: "decompose", RunStage.GATE_1_DECOMPOSITION: "gate_1", RunStage.SPECIFYING: "specify",
                RunStage.GATE_2_SPEC_READINESS: "gate_2", RunStage.IMPLEMENTING: "implement"}


def _pending_gate(ctx: RunContext) -> dict[str, Any]:
    """The gate the resumed run will wait on again, when one is open: its id, kind and the artefacts it presented."""
    open_gates = [g for g in ctx.store.state(ctx.run_id).open_gates.values() if g.get("kind") != GateKind.BUDGET.value]
    if len(open_gates) != 1:
        return {}
    g = open_gates[0]
    return {"gate_id": g["gate_id"], "kind": g.get("kind"),
            "artifacts": dict(sorted((a["name"], a["sha256"]) for a in g.get("artifacts", [])))}


def _settle_budget_pause(ctx: RunContext, st: Any, events: list[Any], budget_usd: float | None) -> bool:
    """Apply the owner's decision on the budget gate (or `budget_usd`): False when it closes the run."""
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
        return False
    if new_budget != st.budget_usd:
        _raise_budget(ctx, st, new_budget, by=by, gate_id=gate_id)
    else:
        ctx.policy = dataclasses.replace(ctx.policy, run_budget_usd=new_budget)
    return True


# ---------------------------------------------------------------- runs an older controller wrote

def legacy_adoption(ctx: RunContext, events: list[Any]) -> dict[str, Any] | None:
    """The steps to record for a run an older controller wrote (no pipeline steps in its database), so the pipeline can
    replay it, or None for a run that needs none. Adopted: what that controller presented at Gate 2 (each artefact
    checked against the sha256 the owner was shown), the Gate 2 approval and its record when there is one, else the
    approval decision it had not acted on yet, else nothing after the gate (a controller stopped while idle at it: the
    replay waits on that same gate). Writes nothing; raises RuntimeError for a log it cannot continue."""
    if any(s.key.startswith("decompose#") for s in ctx.store.steps(ctx.run_id)):
        return None
    decomposition = _latest_decomposition(ctx)
    if decomposition is None and not any(ev.kind == "gate.opened" for ev in events):
        return None                      # nothing was produced yet: the pipeline starts from the decomposition
    approval = next((ev.payload for ev in reversed(events) if ev.kind == "artifact.produced"
                     and ev.payload.get("artifact_type") == "gate2_approval"), None)
    if approval is not None:
        return _adopted_from_approval(ctx, events, approval, decomposition)
    gate1 = next((ev.payload for ev in reversed(events) if ev.kind == "gate.decided"
                  and ev.payload.get("kind") == GateKind.DECOMPOSITION_REVIEW.value), None)
    if decomposition is None or gate1 is None or gate1.get("decision") != Decision.APPROVE.value:
        raise RuntimeError(f"run {ctx.run_id} was written by an older controller and stopped before Gate 1 approved its "
                           f"decomposition; start a new run from its outcome")
    notes = notes_by_item(gate1.get("comments", ""), [w["work_item_id"] for w in decomposition["work_items"]])
    adopted: dict[str, Any] = {"decompose#1": {"decomposition": decomposition, "shas": {}},
                               "gate_1#1": {"decision": {**gate1, "decided_by": gate1.get("decided_by", "human")},
                                            "feedback": None, "notes": notes or None}}
    opened = next((ev for ev in reversed(events) if ev.kind == "gate.opened"
                   and ev.payload.get("kind") in (GateKind.SPEC_READINESS.value, GateKind.ESCALATION.value)), None)
    if opened is None:
        raise RuntimeError(f"run {ctx.run_id} was written by an older controller and stopped before Gate 2; start a new run "
                           f"from its outcome")
    gate2 = opened.payload
    adopted["specify#1"] = _presented_contracts(ctx, gate2)
    decided = next((ev.payload for ev in events if ev.kind == "gate.decided" and ev.payload.get("gate_id") == gate2["gate_id"]), None)
    if decided is None:
        st = ctx.store.state(ctx.run_id)
        if set(st.open_gates) != {gate2["gate_id"]}:
            raise RuntimeError(f"Gate 2 {gate2['gate_id']} is not the run's only open gate (open: {sorted(st.open_gates) or 'none'})")
        for ev in events:
            if ev.seq <= opened.seq or (ev.kind == "run.failed" and not (ev.payload.get("interrupted_sessions")
                                                                         or ev.payload.get("sessions_left_running"))):
                continue
            if not (ev.kind == "run.resumed" and ev.payload.get("gate_id") == gate2["gate_id"]):
                raise RuntimeError(f"{ev.kind} (event {ev.seq}) follows Gate 2 {gate2['gate_id']}; only a controller stopped "
                                   f"while idle at the gate is recovered")
        return adopted                   # the replay reaches Gate 2 with the presented bytes and waits on that same gate
    if decided.get("decision") not in (Decision.APPROVE.value, Decision.APPROVE_SUBSET.value):
        raise RuntimeError(f"run {ctx.run_id} was written by an older controller and stopped after its Gate 2 was decided "
                           f"({decided.get('decision')}); start a new run from its outcome")
    adopted["gate_2#1"] = {"decision": {**decided, "decided_by": decided.get("decided_by", "human")}, "feedback": None}
    return adopted                       # decided but not acted on: the replay records the approval


def _adopted_from_approval(ctx: RunContext, events: list[Any], approval: dict[str, Any],
                           decomposition: dict[str, Any] | None) -> dict[str, Any]:
    """An older controller's run past its Gate 2 approval: the approval record names the approved contracts (each still
    the bytes it approved), so the replay goes straight to their implementation."""
    record = json.loads(ctx.store.get_artifact(ctx.run_id, "gate_2", Path(approval["path"]).name))
    for sid, sha in record["approved"].items():
        try:
            content = ctx.store.get_artifact(ctx.run_id, "specify", f"final-{sid}.json")
        except FileNotFoundError:
            raise RuntimeError(f"specify/final-{sid}.json, approved at Gate 2 {record['gate_id']}, is missing") from None
        if hashlib.sha256(content).hexdigest() != sha:
            raise RuntimeError(f"specify/final-{sid}.json no longer matches the spec Gate 2 {record['gate_id']} approved")
    if decomposition is None:
        registered = next((ev.payload for ev in reversed(events) if ev.kind == "artifact.produced"
                           and ev.payload.get("artifact_type") == "decomposition" and "work_items" in ev.payload), None)
        decomposition = {"revision": (registered or {}).get("revision", 1), "work_items": (registered or {}).get("work_items", [])}
    specs = {sid: json.loads(ctx.store.get_artifact(ctx.run_id, "specify", f"final-{sid}.json")) for sid in record["approved"]}
    spec_md = {sid: _spec_markdown(ctx, s) for sid, s in specs.items()}
    for sid, md in spec_md.items():
        if md:
            specs[sid]["_markdown"] = md
    verdict = artifact_json_or_none(ctx, "judge", "final-verdict.json") or {}
    decided = next((ev.payload for ev in events if ev.kind == "gate.decided" and ev.payload.get("gate_id") == record["gate_id"]), None)
    decision = decided or {"gate_id": record["gate_id"], "decision": record.get("decision", Decision.APPROVE.value),
                           "decided_by": record.get("decided_by", "human"), "comments": "",
                           "approved_subset": sorted(record["approved"]), "artifact_shas": sorted(record["approved"].values())}
    return {"decompose#1": {"decomposition": decomposition, "shas": {}},
            "gate_1#1": {"decision": {"gate_id": "adopted", "decision": Decision.APPROVE.value, "decided_by": "human"},
                         "feedback": None, "notes": None},
            "specify#1": {"specs": specs, "verdict": verdict, "spec_md": spec_md},
            "gate_2#1": {"decision": {**decision, "decided_by": decision.get("decided_by", "human")}, "feedback": None},
            "approve#1": {"gate_id": record["gate_id"], "approved": record["approved"], "guidance": record.get("guidance", {})}}


def adopt_legacy_run(ctx: RunContext, events: list[Any]) -> None:
    """Record `legacy_adoption`'s steps, once. The blobs hold the decomposition and the contracts as the log has them."""
    adopted = legacy_adoption(ctx, events)
    if not adopted:
        return
    for key, result in adopted.items():
        if key == "decompose#1":
            result = {"decomposition": steps.put_json(ctx, result["decomposition"]), "shas": result["shas"]}
        elif key == "specify#1":
            result = steps.put_json(ctx, result)
        steps.record_step(ctx, key, result, kind="gate" if key.startswith("gate") else "task")


def _latest_decomposition(ctx: RunContext) -> dict[str, Any] | None:
    revs = _rounds(ctx, "decompose", "r", "-decomposition.json")
    return json.loads(ctx.store.get_artifact(ctx.run_id, "decompose", f"r{max(revs)}-decomposition.json")) if revs else None


_SAFE_SPEC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _presented_contracts(ctx: RunContext, gate: dict[str, Any]) -> dict[str, Any]:
    """The specs, verdict and spec.md an older controller's Gate 2 presented, each checked against the sha256 the gate
    showed the owner."""
    gate_id = gate["gate_id"]
    presented = {a["name"]: a["sha256"] for a in gate.get("artifacts", [])}
    sids = [n[:-len(".json")] for n in presented if n != "verdict.json"]
    if "verdict.json" not in presented or not sids or any(not n.endswith(".json") or not _SAFE_SPEC_ID.match(n[:-len(".json")])
                                                          for n in presented if n != "verdict.json"):
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
    kind = GateKind(gate["kind"])
    if (kind == GateKind.SPEC_READINESS) != ((verdict.get("overall") or {}).get("result") == "pass"):
        raise RuntimeError(f"Gate 2 {gate_id} is {kind.value} but the verdict it presented says {(verdict.get('overall') or {}).get('result')}")
    decomposition = _latest_decomposition(ctx) or {"work_items": []}
    items = {w["work_item_id"] for w in decomposition["work_items"]}
    if any(s.get("work_item_id") not in items for s in specs.values()):
        raise RuntimeError(f"a spec at Gate 2 {gate_id} belongs to no work item of the run's latest decomposition")
    spec_md = {sid: _spec_markdown(ctx, s) for sid, s in specs.items()}
    for sid, md in spec_md.items():
        if md:
            specs[sid]["_markdown"] = md
    return {"specs": specs, "verdict": verdict, "spec_md": spec_md}


def resumable_build_escalation(st: Any, events: list[Any]) -> bool:
    """Only a settled build escalation with recorded contract approval can be rechecked.

    Specification and deployment escalations still require their own human gates.
    """
    last = next((ev for ev in reversed(events) if ev.kind == "run.escalated"), None)
    approvals = [ev.payload for ev in events if ev.kind == "artifact.produced"
                 and ev.payload.get("artifact_type") == "gate2_approval"]
    approved = set(approvals[-1].get("approved", [])) if approvals else set()
    return bool(st.stage == RunStage.ESCALATED and not (set(st.open_gates) - {e.payload["gate_id"] for e in events if e.kind == "gate.opened" and e.payload.get("kind") == "build_recovery"}) and not st.open_questions
                and last is not None and last.spec_id in approved
                and any(ev.kind == "stage.entered" and ev.payload.get("stage") == "implementing" for ev in events)
                and all(wi.cr_id is not None or wi.stage in (WorkItemStage.BLOCKED, WorkItemStage.MERGE_JUDGING,
                                                            WorkItemStage.ESCALATED, WorkItemStage.SPEC_APPROVED)
                        for wi in st.work_items.values()))


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
    previous = st.budget_usd
    ctx.policy = dataclasses.replace(ctx.policy, run_budget_usd=new_budget)
    ctx.event("budget.raised", {"budget_usd": new_budget, "previous_budget_usd": previous, "spent_usd": round(st.list_cost_usd, 2),
                                "gate_id": gate_id, "decided_by": by}, stage="budget", actor="human")

