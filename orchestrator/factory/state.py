"""Run state machine, derived from the append-only event log.

The orchestrator never mutates state directly. It appends `RunEvent`s and re-derives
`RunState` by folding the log through `reduce`. That makes every run replayable (for
evals and audits) and makes the Factory Console a pure view over the log.

Stage names here are the ones the UX and docs use. Keep them in sync with
`docs/01-pipeline.md`.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class RunStage(str, Enum):
    INTAKE = "intake"
    DECOMPOSING = "decomposing"
    GATE_1_DECOMPOSITION = "gate_1_decomposition"
    SPECIFYING = "specifying"
    GATE_2_SPEC_READINESS = "gate_2_spec_readiness"
    IMPLEMENTING = "implementing"          # per work item; see WorkItemStage
    COMPLETED = "completed"                # every work item merged or closed
    ESCALATED = "escalated"                # needs a human decision beyond a gate
    PAUSED_BUDGET = "paused_budget"
    FAILED = "failed"
    CLOSED = "closed"                      # rejected at a gate


class WorkItemStage(str, Enum):
    PENDING = "pending"                    # waiting on dependencies or Gate 2
    SPEC_DRAFTING = "spec_drafting"
    SPEC_JUDGING = "spec_judging"
    SPEC_APPROVED = "spec_approved"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    RESPONDING = "responding"
    MERGE_JUDGING = "merge_judging"
    INTEGRATING = "integrating"
    CR_OPEN = "cr_open"                    # Gate 3: human review of the change request (PR or MR) on the forge
    MERGED = "merged"
    DEPLOYING_DEV = "deploying_dev"        # Stage 5: platform pipeline deploying the default branch to dev; observed, not driven
    DEPLOYED_DEV = "deployed_dev"          # dev deploy succeeded and the spec's e2e checks passed against the dev URL
    CLOSED = "closed"
    ESCALATED = "escalated"
    BLOCKED = "blocked"                    # implementer wrote blocked.md


# Legal run-level transitions. Anything else raises IllegalTransition. The orchestrator can die in any live stage
# (a gate surface raising while a gate is open included), so run.failed is legal from every non-terminal stage.
RUN_TRANSITIONS: dict[RunStage, set[RunStage]] = {
    RunStage.INTAKE: {RunStage.DECOMPOSING, RunStage.CLOSED, RunStage.FAILED},
    RunStage.DECOMPOSING: {RunStage.GATE_1_DECOMPOSITION, RunStage.ESCALATED, RunStage.PAUSED_BUDGET, RunStage.FAILED},
    RunStage.GATE_1_DECOMPOSITION: {RunStage.SPECIFYING, RunStage.DECOMPOSING, RunStage.CLOSED, RunStage.FAILED},
    RunStage.SPECIFYING: {RunStage.GATE_2_SPEC_READINESS, RunStage.ESCALATED, RunStage.PAUSED_BUDGET, RunStage.FAILED},
    RunStage.GATE_2_SPEC_READINESS: {RunStage.IMPLEMENTING, RunStage.SPECIFYING, RunStage.GATE_1_DECOMPOSITION, RunStage.CLOSED,
                                     RunStage.COMPLETED, RunStage.FAILED},   # COMPLETED only for design-only runs (Policy.design_only)
    RunStage.IMPLEMENTING: {RunStage.COMPLETED, RunStage.ESCALATED, RunStage.PAUSED_BUDGET, RunStage.FAILED},
    RunStage.ESCALATED: {RunStage.DECOMPOSING, RunStage.SPECIFYING, RunStage.IMPLEMENTING, RunStage.CLOSED, RunStage.FAILED},
    RunStage.PAUSED_BUDGET: {RunStage.DECOMPOSING, RunStage.SPECIFYING, RunStage.IMPLEMENTING, RunStage.CLOSED, RunStage.FAILED},
    RunStage.COMPLETED: set(),
    # `run.resumed` only: a run that died during implementation picks up where its artefacts stop; one whose controller was
    # stopped while idle at Gate 2 continues waiting on that same gate (runner.pending_gate2 verifies it first)
    RunStage.FAILED: {RunStage.IMPLEMENTING, RunStage.GATE_2_SPEC_READINESS},
    RunStage.CLOSED: set(),
}

WORK_ITEM_TRANSITIONS: dict[WorkItemStage, set[WorkItemStage]] = {
    WorkItemStage.PENDING: {WorkItemStage.SPEC_DRAFTING, WorkItemStage.CLOSED},
    WorkItemStage.SPEC_DRAFTING: {WorkItemStage.SPEC_JUDGING, WorkItemStage.ESCALATED},
    WorkItemStage.SPEC_JUDGING: {WorkItemStage.SPEC_DRAFTING, WorkItemStage.SPEC_APPROVED, WorkItemStage.ESCALATED},
    WorkItemStage.SPEC_APPROVED: {WorkItemStage.IMPLEMENTING, WorkItemStage.SPEC_DRAFTING, WorkItemStage.CLOSED},
    WorkItemStage.IMPLEMENTING: {WorkItemStage.REVIEWING, WorkItemStage.BLOCKED, WorkItemStage.ESCALATED},
    WorkItemStage.REVIEWING: {WorkItemStage.RESPONDING, WorkItemStage.MERGE_JUDGING, WorkItemStage.ESCALATED},
    # RESPONDING -> MERGE_JUDGING: the orchestrator re-verified a response round and found no reason for another
    # review (the last round's response, or a response to deterministic-only findings whose checks now pass).
    WorkItemStage.RESPONDING: {WorkItemStage.REVIEWING, WorkItemStage.MERGE_JUDGING, WorkItemStage.ESCALATED},
    WorkItemStage.MERGE_JUDGING: {WorkItemStage.INTEGRATING, WorkItemStage.IMPLEMENTING, WorkItemStage.ESCALATED},
    WorkItemStage.INTEGRATING: {WorkItemStage.CR_OPEN, WorkItemStage.ESCALATED},
    WorkItemStage.CR_OPEN: {WorkItemStage.MERGED, WorkItemStage.CLOSED, WorkItemStage.IMPLEMENTING},
    WorkItemStage.BLOCKED: {WorkItemStage.IMPLEMENTING, WorkItemStage.SPEC_DRAFTING, WorkItemStage.ESCALATED},
    WorkItemStage.ESCALATED: {WorkItemStage.SPEC_DRAFTING, WorkItemStage.IMPLEMENTING, WorkItemStage.CLOSED},
    WorkItemStage.MERGED: {WorkItemStage.DEPLOYING_DEV},
    # A failed dev deploy or dev e2e opens a DEPLOY_FAILURE gate (-> ESCALATED); the human may route it back to
    # IMPLEMENTING (a follow-up branch and change request), or close the item. Promotion beyond dev is human and outside the factory.
    WorkItemStage.DEPLOYING_DEV: {WorkItemStage.DEPLOYED_DEV, WorkItemStage.ESCALATED},
    WorkItemStage.DEPLOYED_DEV: set(),
    WorkItemStage.CLOSED: set(),
}

# Stages at which a work item's code is on the default branch, so dependants may start.
MERGED_STAGES = {WorkItemStage.MERGED, WorkItemStage.DEPLOYING_DEV, WorkItemStage.DEPLOYED_DEV}


class IllegalTransition(Exception):
    pass


@dataclass(frozen=True)
class Policy:
    """Loop limits and budgets. Tune per repository; defaults are conservative."""
    spec_judge_max_rounds: int = 3
    review_max_rounds: int = 2           # one fresh review per round; see docs/research (Song 2603.16244)
    outcome_max_iterations: int = 4      # platform grader iterations inside a writing session
    run_budget_usd: float = 150.0        # the outcome's constraints.budget_usd: enforced before every session (budget.py)
    # Per-session dollar caps: observed p95 x ~3 over the first live runs (docs/postmortems/2026-10-10-first-live-runs.md,
    # issue 17). A session that hits its cap opens a budget gate; the owner raises it or closes the run.
    stage_budget_usd: dict[str, float] = field(default_factory=lambda: {
        "decompose": 4.0, "specify": 3.0, "judge": 6.0,
        "implement": 15.0, "review": 6.0, "security_review": 6.0, "merge_judge": 3.0, "integrate": 2.0,
    })
    # Observed p50 per stage: FACTORY_BUDGET_SCALE never takes a cap below one typical session (a 0.4 scale left the
    # judge's cap under one round on live run 1 and the verdict was cut off mid-write).
    stage_budget_floor_usd: dict[str, float] = field(default_factory=lambda: {
        "decompose": 0.9, "specify": 0.7, "judge": 2.5,
        "implement": 3.3, "review": 2.6, "security_review": 2.6, "merge_judge": 0.9, "integrate": 0.3,
    })
    parallel_implementations: int = 3    # independent work items implemented at once (separate branches)
    parallel_spec_writers: int = 8       # spec writer sessions at once; all items are written in one go by default
    spec_waves: bool = False             # True: write specs in dependency-order waves instead of all at once
    reviewer_passes: int = 2             # independent reviewer copies in a first round over a change larger than review_single_pass_lines
    # A first-round change of at most this many hand-written lines (size.py) gets one general pass: the second pass found
    # complementary defects on a 500-line change, but on small diffs both passes re-prove the same thing (live run 2).
    review_single_pass_lines: int = 150
    # Later rounds: passes that adjudicate the previous findings and review only what changed since the head they saw.
    # Two passes over the whole diff in round 2 of live run 2 re-proved the same four withdrawals (post-mortem issue 10).
    reviewer_passes_followup: int = 1
    security_review: bool = True         # one security reviewer pass per round beside the general reviewers (needs the agent deployed)
    design_only: bool = False            # stop after Gate 2: real agents for decompose/specify/judge, no code stage, no forge needed
    # Benchmark and evaluation runs only (FACTORY_AUTO_APPROVE; post-mortem issue 20): gates pre-approved for the run
    # ("gate_1", "gate_2"). A listed gate still opens (page and hashes kept) and is approved by "policy" only when nothing
    # in it needs a person: Gate 1 with no blocking question, Gate 2 with a passing verdict, no blocking issue and no
    # blocking question. Deliberately not offered in the Console: design and contracts are where a person's review has
    # the most leverage (design-principles.md 1, 14), and docs/roadmap.md defers automatic approval of real runs until
    # there is gate telemetry. Every policy decision is recorded as such, which is that telemetry.
    auto_approve: tuple[str, ...] = ()
    preview_wait_seconds: int = 1800     # how long verification waits for the platform to deploy head_sha to its preview
    dev_pipeline_timeout_seconds: int = 3600  # Stage 5: how long to watch the dev pipeline before a DEPLOY_FAILURE gate
    # Hand-written changed lines (tests included, generated files excluded) per size; FACTORY_SIZE_BUDGET tunes them.
    # Above M is split at planning; an overrun at merge is a flag for the owner (size.py, post-mortem 2026-10-11 §3).
    size_budget_lines: dict[str, int] = field(default_factory=lambda: {"XS": 50, "S": 150, "M": 400})


@dataclass
class RunEvent:
    run_id: str
    seq: int
    kind: str
    actor: dict[str, Any]
    payload: dict[str, Any] = field(default_factory=dict)
    stage: str | None = None
    work_item_id: str | None = None
    spec_id: str | None = None
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    event_id: str = field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:12]}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "run_id": self.run_id, "seq": self.seq, "at": self.at,
            "kind": self.kind, "actor": self.actor, "stage": self.stage,
            "work_item_id": self.work_item_id, "spec_id": self.spec_id, "payload": self.payload,
        }


@dataclass
class WorkItemState:
    work_item_id: str
    stage: WorkItemStage = WorkItemStage.PENDING
    spec_id: str | None = None
    spec_round: int = 0
    review_round: int = 0
    branch: str | None = None
    cr_id: int | None = None            # change request number (PR) or iid (MR)
    merge_sha: str | None = None        # the commit on the default branch once the change request is merged (Stage 5 watches its pipeline)
    depends_on: list[str] = field(default_factory=list)
    dev_pipeline_id: int | None = None
    dev_environment_url: str | None = None


@dataclass
class RunState:
    run_id: str
    stage: RunStage = RunStage.INTAKE
    outcome_id: str | None = None
    decomposition_revision: int = 0
    work_items: dict[str, WorkItemState] = field(default_factory=dict)
    open_gates: dict[str, dict[str, Any]] = field(default_factory=dict)
    open_questions: dict[str, dict[str, Any]] = field(default_factory=dict)
    list_cost_usd: float = 0.0           # sum of session.finished list_cost_usd: what the run has spent so far
    sessions_finished: int = 0
    finished_sessions: dict[str, str | None] = field(default_factory=dict)   # session id -> stop_reason; spend counts once
    budget_usd: float | None = None      # the run's cap (run.created), raised by budget.raised; None until run.created
    last_seq: int = -1

    def ready_work_items(self) -> list[WorkItemState]:
        """Work items whose dependencies are merged and which are approved to implement."""
        out = []
        for wi in self.work_items.values():
            if wi.stage != WorkItemStage.SPEC_APPROVED:
                continue
            if all(self.work_items[d].stage in MERGED_STAGES for d in wi.depends_on if d in self.work_items):
                out.append(wi)
        return out


def _move_run(state: RunState, to: RunStage) -> None:
    if to == state.stage:
        return                      # re-entering the stage you are in (a resume) is not a transition
    if to not in RUN_TRANSITIONS[state.stage]:
        raise IllegalTransition(f"run {state.run_id}: {state.stage.value} -> {to.value}")
    state.stage = to


def _move_item(state: RunState, work_item_id: str, to: WorkItemStage) -> None:
    wi = state.work_items[work_item_id]
    if to == wi.stage:
        return                      # same: `factory resume` re-enters the round the item was already in
    if to not in WORK_ITEM_TRANSITIONS[wi.stage]:
        raise IllegalTransition(f"work item {work_item_id}: {wi.stage.value} -> {to.value}")
    wi.stage = to


def reduce(state: RunState, ev: RunEvent) -> RunState:
    """Fold one event into the state. Pure apart from mutating `state` in place."""
    if ev.seq != state.last_seq + 1:
        raise ValueError(f"event seq {ev.seq} out of order (expected {state.last_seq + 1})")
    state.last_seq = ev.seq
    p = ev.payload
    k = ev.kind

    if k == "run.created":
        state.outcome_id = p["outcome_id"]
        state.budget_usd = float(p["budget_usd"])
    elif k == "stage.entered":
        _move_run(state, RunStage(p["stage"]))
    elif k == "artifact.produced" and p.get("artifact_type") == "decomposition" and "work_items" in p:
        # The registration event carries the work items; per-file artifact.produced events (same artifact_type,
        # emitted by persist_outputs for decomposition.json / decomposition.md) are informational only.
        state.decomposition_revision = p["revision"]
        # (Re)register work items; a re-decomposition replaces pending items but keeps merged ones.
        current_ids = {item["work_item_id"] for item in p["work_items"]}
        state.work_items = {wid: wi for wid, wi in state.work_items.items()
                            if wid in current_ids or wi.stage in MERGED_STAGES | {WorkItemStage.CR_OPEN}}
        for item in p["work_items"]:
            existing = state.work_items.get(item["work_item_id"])
            if existing is None or existing.stage not in MERGED_STAGES | {WorkItemStage.CR_OPEN}:
                state.work_items[item["work_item_id"]] = WorkItemState(
                    work_item_id=item["work_item_id"], depends_on=list(item.get("depends_on", []))
                )
    elif k == "gate.opened":
        state.open_gates[p["gate_id"]] = p
    elif k in ("gate.decided", "gate.superseded"):
        state.open_gates.pop(p["gate_id"], None)
    elif k == "question.asked":
        state.open_questions[p["question_id"]] = p
    elif k == "question.answered":
        state.open_questions.pop(p["question_id"], None)
    elif k == "session.finished":
        # A session finishes once. A controller that reattaches to a session its predecessor followed (ADR-0013) may
        # report its end again; its spend is not counted twice.
        sid = p.get("session_id")
        if not (sid and sid in state.finished_sessions):
            state.list_cost_usd += float(p.get("list_cost_usd") or 0.0)
            state.sessions_finished += 1
            if sid:
                state.finished_sessions[sid] = p.get("stop_reason")
    elif k == "work_item.stage":
        _move_item(state, ev.work_item_id, WorkItemStage(p["stage"]))
        wi = state.work_items[ev.work_item_id]
        if "spec_id" in p:
            wi.spec_id = p["spec_id"]
        if "spec_round" in p:
            wi.spec_round = p["spec_round"]
        if "review_round" in p:
            wi.review_round = p["review_round"]
        if "branch" in p:
            wi.branch = p["branch"]
        if "cr_id" in p:
            wi.cr_id = p["cr_id"]
        if "merge_sha" in p:
            wi.merge_sha = p["merge_sha"]
        if "pipeline_id" in p:
            wi.dev_pipeline_id = p["pipeline_id"]
        if "environment_url" in p:
            wi.dev_environment_url = p["environment_url"]
    elif k == "budget.reached":
        # Two ways here, both parking the run until a human decides: the run cap (budget.py: the next session would take
        # the run past constraints.budget_usd; the payload carries `next` and the run budget) and one session hitting its
        # own cap (stages/common.run_session; the payload names the session). Several sessions may hit their caps at
        # once, so a second pause is informational.
        if p.get("next") is not None and p.get("budget_usd") is not None:
            state.budget_usd = float(p["budget_usd"])
        if state.stage != RunStage.PAUSED_BUDGET:
            _move_run(state, RunStage.PAUSED_BUDGET)
    elif k == "budget.raised":
        state.budget_usd = float(p["budget_usd"])
    elif k == "run.escalated":
        if state.stage != RunStage.ESCALATED:       # one escalation per failing spec or session; the run escalates once
            _move_run(state, RunStage.ESCALATED)
    elif k == "run.failed":
        _move_run(state, RunStage.FAILED)
    elif k == "run.resumed":
        # `factory resume`: back into the stage the run died or paused in (no-op when the process was killed before
        # run.failed). From PAUSED_BUDGET the stage is the one the budget check interrupted.
        to = RunStage(p["stage"])
        if state.stage != to:
            _move_run(state, to)
    elif k == "run.completed":
        # An escalated run waits for a person. The runner used to append run.completed after a merge-judge escalation
        # (run_8d9b17b4f5), and replaying escalated -> completed raised, so the run could not be read at all.
        if state.stage != RunStage.ESCALATED:
            _move_run(state, RunStage.COMPLETED)
    elif k in ("deploy.started", "deploy.succeeded") and ev.work_item_id in state.work_items:
        wi = state.work_items[ev.work_item_id]
        wi.dev_pipeline_id = p.get("pipeline_id", wi.dev_pipeline_id)
        wi.dev_environment_url = p.get("environment_url", wi.dev_environment_url)
    # Other kinds (cr.opened, cr.merged, cr.closed, judge.verdict, review.findings, deploy.failed, e2e.dev_*) are informational
    # for the timeline; the watcher pairs cr.merged / cr.closed with the work_item.stage event that moves the item.
    return state


def replay(run_id: str, events: list[RunEvent]) -> RunState:
    state = RunState(run_id=run_id)
    for ev in sorted(events, key=lambda e: e.seq):
        reduce(state, ev)
    return state


def effective_run_events(events: list[RunEvent]) -> list[RunEvent]:
    """Presentation follows replay: ignore legacy completion events appended after escalation.

    The original append-only log remains available as evidence. A recovery may legitimately complete later.
    """
    stage = RunStage.INTAKE
    visible = []
    for event in events:
        if event.kind == "run.completed" and stage == RunStage.ESCALATED:
            continue
        visible.append(event)
        if event.kind in ("stage.entered", "run.resumed"):
            stage = RunStage(event.payload["stage"])
        elif event.kind in ("run.escalated", "run.failed", "run.completed"):
            stage = RunStage(event.kind.split(".")[1])
    return visible
