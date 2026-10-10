"""Run state machine, derived from the append-only event log.

The orchestrator never mutates state directly. It appends `RunEvent`s and re-derives
`RunState` by folding the log through `reduce`. That makes every run replayable (for
evals and audits) and makes the Factory Console a pure view over the log.

Stage names here are the ones the UX and docs use. Keep them in sync with
`docs/01-pipeline.md`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
import uuid


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
    MR_OPEN = "mr_open"                    # Gate 3: human review in GitLab
    MERGED = "merged"
    CLOSED = "closed"
    ESCALATED = "escalated"
    BLOCKED = "blocked"                    # implementer wrote blocked.md


# Legal run-level transitions. Anything else raises IllegalTransition.
RUN_TRANSITIONS: dict[RunStage, set[RunStage]] = {
    RunStage.INTAKE: {RunStage.DECOMPOSING, RunStage.CLOSED},
    RunStage.DECOMPOSING: {RunStage.GATE_1_DECOMPOSITION, RunStage.ESCALATED, RunStage.PAUSED_BUDGET, RunStage.FAILED},
    RunStage.GATE_1_DECOMPOSITION: {RunStage.SPECIFYING, RunStage.DECOMPOSING, RunStage.CLOSED},
    RunStage.SPECIFYING: {RunStage.GATE_2_SPEC_READINESS, RunStage.ESCALATED, RunStage.PAUSED_BUDGET, RunStage.FAILED},
    RunStage.GATE_2_SPEC_READINESS: {RunStage.IMPLEMENTING, RunStage.SPECIFYING, RunStage.GATE_1_DECOMPOSITION, RunStage.CLOSED},
    RunStage.IMPLEMENTING: {RunStage.COMPLETED, RunStage.ESCALATED, RunStage.PAUSED_BUDGET, RunStage.FAILED},
    RunStage.ESCALATED: {RunStage.DECOMPOSING, RunStage.SPECIFYING, RunStage.IMPLEMENTING, RunStage.CLOSED, RunStage.FAILED},
    RunStage.PAUSED_BUDGET: {RunStage.DECOMPOSING, RunStage.SPECIFYING, RunStage.IMPLEMENTING, RunStage.CLOSED},
    RunStage.COMPLETED: set(),
    RunStage.FAILED: set(),
    RunStage.CLOSED: set(),
}

WORK_ITEM_TRANSITIONS: dict[WorkItemStage, set[WorkItemStage]] = {
    WorkItemStage.PENDING: {WorkItemStage.SPEC_DRAFTING, WorkItemStage.CLOSED},
    WorkItemStage.SPEC_DRAFTING: {WorkItemStage.SPEC_JUDGING, WorkItemStage.ESCALATED},
    WorkItemStage.SPEC_JUDGING: {WorkItemStage.SPEC_DRAFTING, WorkItemStage.SPEC_APPROVED, WorkItemStage.ESCALATED},
    WorkItemStage.SPEC_APPROVED: {WorkItemStage.IMPLEMENTING, WorkItemStage.SPEC_DRAFTING, WorkItemStage.CLOSED},
    WorkItemStage.IMPLEMENTING: {WorkItemStage.REVIEWING, WorkItemStage.BLOCKED, WorkItemStage.ESCALATED},
    WorkItemStage.REVIEWING: {WorkItemStage.RESPONDING, WorkItemStage.MERGE_JUDGING, WorkItemStage.ESCALATED},
    WorkItemStage.RESPONDING: {WorkItemStage.REVIEWING, WorkItemStage.ESCALATED},
    WorkItemStage.MERGE_JUDGING: {WorkItemStage.INTEGRATING, WorkItemStage.IMPLEMENTING, WorkItemStage.ESCALATED},
    WorkItemStage.INTEGRATING: {WorkItemStage.MR_OPEN, WorkItemStage.ESCALATED},
    WorkItemStage.MR_OPEN: {WorkItemStage.MERGED, WorkItemStage.CLOSED, WorkItemStage.IMPLEMENTING},
    WorkItemStage.BLOCKED: {WorkItemStage.IMPLEMENTING, WorkItemStage.SPEC_DRAFTING, WorkItemStage.ESCALATED},
    WorkItemStage.ESCALATED: {WorkItemStage.SPEC_DRAFTING, WorkItemStage.IMPLEMENTING, WorkItemStage.CLOSED},
    WorkItemStage.MERGED: set(),
    WorkItemStage.CLOSED: set(),
}


class IllegalTransition(Exception):
    pass


@dataclass(frozen=True)
class Policy:
    """Loop limits and budgets. Tune per repository; defaults are conservative."""
    spec_judge_max_rounds: int = 3
    review_max_rounds: int = 2           # one fresh review per round; see docs/research (Song 2603.16244)
    outcome_max_iterations: int = 4      # platform grader iterations inside a writing session
    run_budget_usd: float = 150.0
    stage_budget_usd: dict[str, float] = field(default_factory=lambda: {
        "decompose": 15.0, "specify": 10.0, "judge": 8.0,
        "implement": 40.0, "review": 15.0, "merge_judge": 8.0, "integrate": 2.0,
    })
    parallel_implementations: int = 3    # independent work items implemented at once (separate branches)
    reviewer_passes: int = 2             # independent reviewer copies per round, findings merged


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
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
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
    mr_iid: int | None = None
    depends_on: list[str] = field(default_factory=list)


@dataclass
class RunState:
    run_id: str
    stage: RunStage = RunStage.INTAKE
    outcome_id: str | None = None
    decomposition_revision: int = 0
    work_items: dict[str, WorkItemState] = field(default_factory=dict)
    open_gates: dict[str, dict[str, Any]] = field(default_factory=dict)
    open_questions: dict[str, dict[str, Any]] = field(default_factory=dict)
    list_cost_usd: float = 0.0
    last_seq: int = -1

    def ready_work_items(self) -> list[WorkItemState]:
        """Work items whose dependencies are merged and which are approved to implement."""
        out = []
        for wi in self.work_items.values():
            if wi.stage != WorkItemStage.SPEC_APPROVED:
                continue
            if all(self.work_items[d].stage == WorkItemStage.MERGED for d in wi.depends_on if d in self.work_items):
                out.append(wi)
        return out


def _move_run(state: RunState, to: RunStage) -> None:
    if to not in RUN_TRANSITIONS[state.stage]:
        raise IllegalTransition(f"run {state.run_id}: {state.stage.value} -> {to.value}")
    state.stage = to


def _move_item(state: RunState, work_item_id: str, to: WorkItemStage) -> None:
    wi = state.work_items[work_item_id]
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
    elif k == "stage.entered":
        _move_run(state, RunStage(p["stage"]))
    elif k == "artifact.produced" and p.get("artifact_type") == "decomposition":
        state.decomposition_revision = p["revision"]
        # (Re)register work items; a re-decomposition replaces pending items but keeps merged ones.
        for item in p["work_items"]:
            existing = state.work_items.get(item["work_item_id"])
            if existing is None or existing.stage not in {WorkItemStage.MERGED, WorkItemStage.MR_OPEN}:
                state.work_items[item["work_item_id"]] = WorkItemState(
                    work_item_id=item["work_item_id"], depends_on=list(item.get("depends_on", []))
                )
    elif k == "gate.opened":
        state.open_gates[p["gate_id"]] = p
    elif k == "gate.decided":
        state.open_gates.pop(p["gate_id"], None)
    elif k == "question.asked":
        state.open_questions[p["question_id"]] = p
    elif k == "question.answered":
        state.open_questions.pop(p["question_id"], None)
    elif k == "session.finished":
        state.list_cost_usd += float(p.get("list_cost_usd") or 0.0)
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
        if "mr_iid" in p:
            wi.mr_iid = p["mr_iid"]
    elif k == "budget.reached":
        _move_run(state, RunStage.PAUSED_BUDGET)
    elif k == "run.escalated":
        _move_run(state, RunStage.ESCALATED)
    elif k == "run.failed":
        _move_run(state, RunStage.FAILED)
    elif k == "run.completed":
        _move_run(state, RunStage.COMPLETED)
    # Other kinds (mr.opened, judge.verdict, review.findings, ...) are informational for the timeline.
    return state


def replay(run_id: str, events: list[RunEvent]) -> RunState:
    state = RunState(run_id=run_id)
    for ev in sorted(events, key=lambda e: e.seq):
        reduce(state, ev)
    return state
