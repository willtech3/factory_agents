"""What a run was for, where it stands, and what came out of it, derived from its event log.

`summarize(events, state)` folds one run's events into the record the run history shows: a coarse `status`
(needs_you, running, paused, completed, closed, failed, escalated, and stopped when the controller is known to be
gone), a one-line `headline` that says what the run produced or why it ended, and the details behind it (the pull
requests and how far each got, the human's gate decisions, the failure or escalation reason, spend against budget,
timing). The Console's Runs page and `factory runs` show the same record.

Nothing here is recorded. Every input is an event the orchestrator already appends (run.created, gate.decided,
cr.opened, cr.merged, deploy.succeeded, run.completed, run.failed, run.escalated, budget.reached), so a run's history
is readable long after its controller has exited, and a change in wording never touches the log.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .budget import last_pause
from .state import MERGED_STAGES, RunEvent, RunStage, RunState, WorkItemStage, effective_run_events, replay

TERMINAL_STATUS = {RunStage.COMPLETED: "completed", RunStage.CLOSED: "closed", RunStage.FAILED: "failed",
                   RunStage.ESCALATED: "escalated"}
STATUS_LABEL = {"needs_you": "Needs you", "running": "Running", "stopped": "Stopped", "paused": "Paused at budget",
                "completed": "Done", "closed": "Closed", "failed": "Failed", "escalated": "Escalated"}
STAGE_DOING = {"intake": "taking in the outcome", "decomposing": "decomposing the outcome", "gate_1_decomposition": "at Gate 1",
               "specifying": "writing and judging the contracts", "gate_2_spec_readiness": "at Gate 2", "implementing": "building"}
GATE_LABEL = {"gate_1_decomposition": "Gate 1", "gate_2_spec_readiness": "Gate 2", "escalation": "the escalation gate",
              "build_recovery": "Build recovery", "budget": "the budget gate", "deploy_failure": "the deploy-failure gate"}
PR_STATE = {WorkItemStage.CR_OPEN: "open", WorkItemStage.MERGED: "merged", WorkItemStage.DEPLOYING_DEV: "deploying to dev",
            WorkItemStage.DEPLOYED_DEV: "verified in dev", WorkItemStage.CLOSED: "closed without merging"}
ROLE_NAME = {"decomposer": "decomposer", "spec_writer": "spec writer", "spec_judge": "spec judge", "implementer": "implementer",
             "reviewer": "reviewer", "security_reviewer": "security reviewer", "merge_judge": "merge judge", "integrator": "integrator"}


def summarize(events: list[RunEvent], state: RunState, controller_alive: bool | None = None) -> dict[str, Any]:
    """One record per run for the history. `controller_alive` is what `controller_running` found for a run that is not
    finished (None when there is no lock file, as for a run driven in-process): False turns a run that looks
    in progress into `stopped`, because nothing will move it or pick up a decision on its open gate."""
    events = effective_run_events(events)
    created = next((ev.payload for ev in events if ev.kind == "run.created"), {})
    completed = next((ev.payload for ev in events if ev.kind == "run.completed"), None)
    design_only = bool(created.get("design_only") or (completed or {}).get("design_only"))
    titles: dict[str, str] = {}
    for ev in events:
        if ev.kind == "artifact.produced" and ev.payload.get("artifact_type") == "decomposition" and "work_items" in ev.payload:
            titles = {w["work_item_id"]: w.get("title", "") for w in ev.payload["work_items"]}
    pull_requests = _pull_requests(events, state, titles)
    decisions = [{"kind": ev.payload.get("kind"), "label": GATE_LABEL.get(ev.payload.get("kind"), ev.payload.get("kind")),
                  "decision": ev.payload.get("decision"), "by": ev.payload.get("decided_by") or ev.actor.get("id"), "at": ev.at,
                  "comments": ev.payload.get("comments") or "", "approved_subset": list(ev.payload.get("approved_subset") or [])}
                 for ev in events if ev.kind == "gate.decided"]
    answers = sum(1 for ev in events if ev.kind == "question.answered")
    approved = sorted({ev.spec_id or ev.payload.get("spec_id") for ev in events
                       if ev.kind == "work_item.stage" and ev.payload.get("stage") == WorkItemStage.SPEC_APPROVED.value} - {None})
    if not approved and completed:
        approved = sorted(completed.get("approved_specs") or completed.get("specs_with_change_requests") or [])
    failure = next((ev.payload.get("reason") for ev in reversed(events) if ev.kind == "run.failed"), None) if state.stage == RunStage.FAILED else None
    escalations = [ev.payload.get("reason") for ev in events if ev.kind == "run.escalated" and ev.payload.get("reason")]
    failed_in = _stage_before(events, "run.failed")
    pause = last_pause(events) if state.stage == RunStage.PAUSED_BUDGET else None
    # a finished run, or one whose controller is gone, has no live work, whatever sessions it left unfinished
    status = _status(state, controller_alive)
    live = {} if status in ("stopped", *TERMINAL_STATUS.values()) else _live_sessions(events)
    working = _working_roles(live)
    # when the oldest live session started: agent activity, which pipeline events (updated_at) do not track
    working_since = min((at for _, at in live.values()), default=None)
    counts = {
        "work_items": len(state.work_items), "specs_approved": len(approved), "pull_requests": len(pull_requests),
        "merged": sum(1 for p in pull_requests if p["stage"] in MERGED_STAGES),
        "verified_in_dev": sum(1 for p in pull_requests if p["stage"] == WorkItemStage.DEPLOYED_DEV),
        "closed": sum(1 for p in pull_requests if p["stage"] == WorkItemStage.CLOSED),
        "decisions": len(decisions), "answers": answers, "sessions": state.sessions_finished,
    }
    record = {
        "run_id": state.run_id, "title": created.get("title") or state.run_id, "outcome_id": created.get("outcome_id"),
        "mode": "design" if design_only else "live", "design_only": design_only, "forge": created.get("forge"),
        "stage": state.stage.value, "status": status, "status_label": STATUS_LABEL[status],
        "terminal": state.stage in TERMINAL_STATUS, "controller_alive": controller_alive,
        "cost_usd": round(state.list_cost_usd, 2), "budget_usd": state.budget_usd,
        "started_at": events[0].at if events else None, "updated_at": events[-1].at if events else None,
        "ended_at": _ended_at(events, state),
        "counts": counts, "pull_requests": [{**p, "stage": p["stage"].value} for p in pull_requests],
        "specs_approved": approved, "decisions": decisions,
        "failure": failure, "failed_in": failed_in, "escalated_in": _stage_before(events, "run.escalated"),
        "closed_in": next((ev.payload.get("stage") for ev in reversed(events) if ev.kind == "stage.entered" and ev.payload.get("stage") != RunStage.CLOSED.value), None) if state.stage == RunStage.CLOSED else None, "escalations": escalations, "pause": pause, "working": working,
        "working_since": working_since, "live_sessions": len(live),
        "open_gates": [GATE_LABEL.get(g.get("kind"), g.get("kind")) for g in state.open_gates.values()],
        "open_questions": len(state.open_questions),
        "open_gate_ids": list(state.open_gates), "open_question_ids": list(state.open_questions),
        "work_items": {"total": len(state.work_items), "by_stage": _by_stage(state)},
    }
    record["headline"] = _headline(record, state)
    return record


def summarize_run(store: Any, runs_dir: Path, run_id: str) -> dict[str, Any]:
    """`summarize` for one run in a store (one read of its log), with the controller lock probed."""
    events = store.events(run_id)
    return summarize(events, replay(run_id, events), controller_running(runs_dir, run_id))


def controller_running(runs_dir: Path, run_id: str) -> bool | None:
    """Whether a controller (`factory run` or `factory resume`) holds the run's lock right now: True when another
    process holds it, False when the lock file exists and nobody holds it (a controller that exited without recording
    why), None when there is no lock file (a run driven in-process, as by a test). Never creates the file: a read must not make a run directory look driven."""
    import fcntl

    path = runs_dir / run_id / "controller.lock"
    if not path.is_file():
        return None
    with path.open("rb") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


# ---------------------------------------------------------------------------------------------------- pieces

def _status(state: RunState, controller_alive: bool | None) -> str:
    if state.stage in TERMINAL_STATUS:
        return TERMINAL_STATUS[state.stage]
    if state.stage == RunStage.PAUSED_BUDGET:
        return "paused"
    if controller_alive is False:
        return "stopped"
    if state.open_gates or state.open_questions:
        return "needs_you"
    return "running"


def _pull_requests(events: list[RunEvent], state: RunState, titles: dict[str, str]) -> list[dict[str, Any]]:
    opened = {ev.work_item_id: ev.payload for ev in events if ev.kind == "cr.opened" and ev.work_item_id}
    out = []
    for wi in state.work_items.values():
        cr = opened.get(wi.work_item_id)
        if cr is None and wi.cr_id is None:
            continue
        cr = cr or {}
        out.append({"work_item_id": wi.work_item_id, "spec_id": wi.spec_id, "title": titles.get(wi.work_item_id, ""),
                    "cr_id": wi.cr_id if wi.cr_id is not None else cr.get("id"), "url": cr.get("url"), "stage": wi.stage,
                    "state": PR_STATE.get(wi.stage, wi.stage.value.replace("_", " ")), "merge_sha": wi.merge_sha,
                    "dev_url": wi.dev_environment_url if wi.stage == WorkItemStage.DEPLOYED_DEV else None})
    return out


def _by_stage(state: RunState) -> dict[str, int]:
    out: dict[str, int] = {}
    for wi in state.work_items.values():
        out[wi.stage.value] = out.get(wi.stage.value, 0) + 1
    return out


def _stage_before(events: list[RunEvent], kind: str) -> str | None:
    """The stage the run was in when `kind` was last recorded (the last stage.entered before it)."""
    stage = at_event = None
    for ev in events:
        if ev.kind == "stage.entered" and ev.payload.get("stage") != RunStage.CLOSED.value:
            stage = ev.payload.get("stage")
        elif ev.kind == "run.resumed" and ev.payload.get("stage"):
            stage = ev.payload["stage"]
        if ev.kind == kind:
            at_event = stage
    return at_event


def _ended_at(events: list[RunEvent], state: RunState) -> str | None:
    if state.stage in TERMINAL_STATUS:
        ending = {"run.completed", "run.failed", "run.escalated"}
        for ev in reversed(events):
            if ev.kind in ending or (ev.kind == "stage.entered" and ev.payload.get("stage") == RunStage.CLOSED.value):
                return ev.at
        return events[-1].at if events else None
    return None


def _live_sessions(events: list[RunEvent]) -> dict[str, tuple[str, str]]:
    """Sessions started and not yet finished: session id -> (role, started at), in the order they started. A resume
    never reattaches to a session its dead predecessor left open (it starts new ones), so `run.resumed` clears them."""
    live: dict[str, tuple[str, str]] = {}
    for ev in events:
        p = ev.payload
        if ev.kind == "session.started" and p.get("session_id"):
            live[p["session_id"]] = (p.get("agent_key") or p.get("agent_id") or "agent", ev.at)
        elif ev.kind == "session.finished":
            live.pop(p.get("session_id"), None)
        elif ev.kind == "run.failed":
            for sid in p.get("interrupted_sessions") or []:
                live.pop(sid, None)
        elif ev.kind == "run.resumed":
            live.clear()
    return live


def _working_roles(live: dict[str, tuple[str, str]]) -> list[str]:
    """Roles with a session started and not yet finished (`_live_sessions`), in the order they started."""
    seen: list[str] = []
    for role, _ in live.values():
        if role not in seen:
            seen.append(role)
    return seen


def _headline(r: dict[str, Any], state: RunState) -> str:
    status, c = r["status"], r["counts"]
    if status == "completed":
        if r["design_only"]:
            return f"Design run: {_plural(c['specs_approved'], 'contract')} approved, nothing built."
        prs = r["pull_requests"]
        if not prs:
            return "Completed with no pull requests."
        # each pull request counted once, by how far it got
        by_state: dict[str, int] = {}
        for p in prs:
            by_state[p["state"]] = by_state.get(p["state"], 0) + 1
        if len(prs) == 1:
            state = prs[0]["state"]
            return "1 pull request opened." if state == "open" else f"1 pull request opened, {state}."
        if by_state == {"open": len(prs)}:
            return f"{len(prs)} pull requests opened."
        order = ["verified in dev", "deploying to dev", "merged", "open", "closed without merging"]
        states = sorted(by_state, key=lambda s: order.index(s) if s in order else len(order))
        return f"{len(prs)} pull requests opened: " + ", ".join(f"{by_state[s]} {s}" for s in states) + "."
    if status == "closed":
        reject = next((d for d in reversed(r["decisions"]) if d["decision"] == "reject"), None)
        if reject is None:
            return "Closed."
        note = reject["comments"].strip()
        return f"Closed at {reject['label']}" + (f": “{_cut(note, 120)}”" if note else ".")
    if status == "failed":
        where = STAGE_DOING.get(r["failed_in"] or "", None)
        head = f"Failed while {where}" if where else "Failed"
        return f"{head}: {_cut(r['failure'], 160)}" if r["failure"] else f"{head}."
    if status == "escalated":
        if not r["escalations"]:
            return "Escalated: a human decision is needed beyond the gates."
        more = f" (and {len(r['escalations']) - 1} more)" if len(r["escalations"]) > 1 else ""
        return f"Escalated: {_cut(r['escalations'][-1], 160)}{more}"
    if status == "paused":
        p = r["pause"]
        if p and p.get("next"):
            n = p["next"]
            return (f"Paused at {_money(p.get('spent_usd', r['cost_usd']))} of {_money(p.get('budget_usd', r['budget_usd']))}: "
                    f"the next step ({n.get('label') or n.get('stage')}, {_plural(n.get('sessions') or 1, 'session')}) "
                    f"needs {_money(p.get('needed_usd'))}.")
        return "Paused: a session hit its own cap; decide the budget gate to continue or close the run."
    doing = STAGE_DOING.get(state.stage.value, state.stage.value.replace("_", " "))
    gates = ", ".join(r["open_gates"])
    if status == "stopped":
        note = f"; {gates} is open but nothing will pick up a decision" if gates else ""
        return f"The controller stopped without recording why, while {doing}{note}."
    if status == "needs_you":
        parts = [f"{gates} waiting on you" if gates else "",
                 f"{_plural(r['open_questions'], 'question')} from the crew waiting on you" if r["open_questions"] else ""]
        return "; ".join(p for p in parts if p).capitalize() + "."
    crew = ", ".join(ROLE_NAME.get(w, w.replace("_", " ")) for w in r["working"])
    return f"{doing.capitalize()}" + (f"; {crew} working." if crew else ".")


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _money(x: Any) -> str:
    return "an unknown amount" if x is None else f"${float(x):.2f}"


def _cut(s: Any, n: int) -> str:
    s = str(s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


__all__ = ["STATUS_LABEL", "controller_running", "summarize", "summarize_run"]
