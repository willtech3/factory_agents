"""The run budget: `outcome.constraints.budget_usd` enforced before every session.

Every session carries its own platform-enforced cap (`Policy.stage_budget_usd`), so no single session can run
away. The run cap is the orchestrator's: before a stage starts a session (or a batch of parallel sessions) it
compares what the run has spent (the sum of `session.finished` list costs in the event log) plus the caps it is
about to commit against `Policy.run_budget_usd`. When the next session could take the run past the cap, nothing
is started: the run moves to `PAUSED_BUDGET` (`budget.reached`), a `budget` gate opens on the configured surface
with the numbers bound by hash, and the stage code unwinds to the runner through `BudgetPaused`, which the runner
turns into a `RunStage.PAUSED_BUDGET` return. No session is in flight at that point, so there is nothing to
interrupt and the process exits cleanly.

The check is cap-based on purpose: a session may spend up to its cap, so "spent plus the next caps" is the only
number that guarantees the run never exceeds the budget. It is conservative; `FACTORY_BUDGET_SCALE` tightens the
caps, and the owner can raise the budget from the gate (`budget_usd: 80` in the decision) and run `factory resume`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .gates import ArtifactRef, Decision, Gate, GateKind
from .state import RunStage

if TYPE_CHECKING:  # pragma: no cover
    from .stages import RunContext

STAGE_LABEL = {"decompose": "decomposer", "specify": "spec writer", "judge": "spec judge", "implement": "implementer",
               "review": "reviewer", "security_review": "security reviewer", "merge_judge": "merge judge", "integrate": "integrator"}


@dataclass
class BudgetPaused(Exception):
    """Control flow, not a failure: `reserve_budget` has already recorded `budget.reached` and opened the gate.
    Raised only from the orchestrator's main thread, before any session of the refused batch is created."""
    run_id: str
    gate_id: str
    budget_usd: float
    spent_usd: float
    stage: str
    sessions: int
    cap_usd: float

    def __str__(self) -> str:
        return (f"run {self.run_id} paused at its budget: ${self.spent_usd:.2f} spent of ${self.budget_usd:.2f}; the next step "
                f"({self.stage}, {self.sessions} session{'s' if self.sessions != 1 else ''}) could cost up to ${self.cap_usd:.2f}. "
                f"Decide gate {self.gate_id} (approve with `budget_usd: N`, or reject), then `factory resume {self.run_id}`.")


def spent_usd(ctx: RunContext) -> float:
    return ctx.store.state(ctx.run_id).list_cost_usd


def reserve_budget(ctx: RunContext, stage_key: str, sessions: int = 1, *, extra: dict[str, int] | None = None) -> None:
    """Call right before starting `sessions` sessions of stage `stage_key` (before the batch when they run in
    parallel, so that a refusal never leaves a sibling in flight). `extra` names sessions of other caps that start in
    the same batch (a review round's security pass: `{"security_review": 1}`); their caps count toward the check and
    the pause is recorded against `stage_key` with the batch's total. Returns when the run can afford them; otherwise
    pauses the run and raises `BudgetPaused`."""
    parts = {stage_key: sessions, **{k: n for k, n in (extra or {}).items() if n > 0}}
    total = sum(parts.values())
    if total <= 0:
        return
    cap = round(sum(float(ctx.policy.stage_budget_usd[k]) * n for k, n in parts.items()), 2)
    budget = float(ctx.policy.run_budget_usd)
    st = ctx.store.state(ctx.run_id)
    spent = round(st.list_cost_usd, 2)
    if spent + cap <= budget + 1e-9:
        return
    raise pause_for_budget(ctx, stage_key, total, cap, spent, budget, st.stage, st.sessions_finished)


def pause_for_budget(ctx: RunContext, stage_key: str, sessions: int, cap: float, spent: float, budget: float,
                     run_stage: RunStage, sessions_finished: int) -> BudgetPaused:
    """Record the pause and open the gate; returns the exception for the caller to raise."""
    n = 1 + sum(1 for ev in ctx.store.events(ctx.run_id) if ev.kind == "budget.reached")
    needed = round(spent + cap, 2)
    record = {"run_id": ctx.run_id, "pause": n, "budget_usd": budget, "spent_usd": spent, "sessions_finished": sessions_finished,
              "next": {"stage": stage_key, "label": STAGE_LABEL.get(stage_key, stage_key), "sessions": sessions, "cap_usd": cap},
              "needed_usd": needed, "run_stage": run_stage.value}
    name = f"paused-{n}.json"
    sha = ctx.store.put_artifact(ctx.run_id, "budget", name, json.dumps(record, indent=2).encode())
    summary = (f"Budget of ${budget:.2f} reached: ${spent:.2f} spent across {sessions_finished} session"
               f"{'s' if sessions_finished != 1 else ''}. The next step, {STAGE_LABEL.get(stage_key, stage_key)} "
               f"({sessions} session{'s' if sessions != 1 else ''}), could cost up to ${cap:.2f}, taking the run to ${needed:.2f}. "
               f"Approve with the new budget (`budget_usd: {int(needed) + 1}` or more) to continue, or reject to close the run. "
               f"Nothing is running while you decide.")
    owner = _owner_handle(ctx)
    gate = Gate(run_id=ctx.run_id, kind=GateKind.BUDGET, artifacts=(ArtifactRef(name=name, sha256=sha),),
                assignees=tuple([owner] if owner else []), summary=summary, options=(Decision.APPROVE, Decision.REJECT))
    ctx.event("budget.reached", {**record, "gate_id": gate.gate_id, "stage": run_stage.value}, stage="budget")
    ctx.queue.open(gate)
    ctx.notifier.gate_opened(gate)
    ctx.event("gate.opened", {"gate_id": gate.gate_id, "kind": GateKind.BUDGET.value, "artifacts": [r.__dict__ for r in gate.artifacts],
                              "assignees": list(gate.assignees), "summary": summary}, stage="budget")
    return BudgetPaused(run_id=ctx.run_id, gate_id=gate.gate_id, budget_usd=budget, spent_usd=spent, stage=stage_key,
                        sessions=sessions, cap_usd=cap)


def _owner_handle(ctx: RunContext) -> str | None:
    try:
        outcome = ctx.store.get_json(ctx.run_id, "intake", "outcome.json")
        return (outcome.get("owner") or {}).get("handle")
    except (FileNotFoundError, ValueError):
        return None


def last_pause(events: list[Any]) -> dict[str, Any] | None:
    """The payload of the latest `budget.reached` event, or None."""
    for ev in reversed(events):
        if ev.kind == "budget.reached":
            return ev.payload
    return None
