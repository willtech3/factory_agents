"""Helpers shared by stage modules."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from ..gates import Answer, ArtifactRef, Decision, Gate, GateDecision, GateKind, Question
from ..sessions import SessionContext, StageResult
from ..state import RunStage
from . import RunContext

BUDGET_RAISE = 1.5      # the cap a session is re-run with after the owner approves a budget gate, relative to the one it hit


class SessionTruncated(RuntimeError):
    """A session stopped before finishing (its dollar cap, or the grader's iteration cap on a graded stage) and the
    owner declined to continue. Its outputs were kept for the audit trail but never read as the stage's result."""


def truncation_reason(res: StageResult, graded: bool) -> str | None:
    """Why a session's output must not be consumed as a result, or None when it finished on its own terms."""
    if res.stop_reason == "budget_reached":
        return "budget_reached"
    if graded and res.outcome_result == "max_iterations_reached":
        return "max_iterations_reached"
    return None


# Session stage -> the run stage a budget pause returns to. Sessions run from worker threads, so the pause/resume/close
# bookkeeping below (read the run state, append the transition) is serialised.
LIVE_STAGE = {"decompose": RunStage.DECOMPOSING, "specify": RunStage.SPECIFYING, "judge": RunStage.SPECIFYING}
TERMINAL = {RunStage.CLOSED, RunStage.ESCALATED, RunStage.FAILED, RunStage.COMPLETED}
_TRANSITIONS = threading.Lock()


def run_session(ctx: RunContext, *, stage: str, prefix: str = "", spec_id: str | None = None, attempt: int = 1,
                **kw: Any) -> StageResult:
    """`ctx.driver.run(stage=..., **kw)` plus the cut-short policy of post-mortem issue 17.

    A session that stopped on `budget_reached`, or on `max_iterations_reached` when it was graded (a rubric was
    defined), did not finish: its files are persisted under `<prefix>truncated<attempt>-` and its spend recorded, a
    `budget.reached` (run paused) or `session.truncated` event is appended, and a budget gate asks the owner.
    Approve on a budget cap re-runs the session with the cap raised by `BUDGET_RAISE`; approve on an iteration cap
    accepts the last output as it stands (the judges downstream still check it); reject closes the run and raises
    `SessionTruncated`. Before this, live run 1's cut-off judge verdict drove a writer round and a decomposer that
    exhausted its grader would have reached Gate 1 unflagged."""
    res = ctx.driver.run(stage=stage, **kw)
    reason = truncation_reason(res, graded=kw.get("rubric_md") is not None)
    if reason is None:
        return res
    wid = kw.get("work_item_id")
    kept = f"{prefix}truncated{attempt}-"
    persist_outputs(ctx, stage, res, prefix=kept, work_item_id=wid, spec_id=spec_id)
    cap = float(kw.get("budget_usd") or 0.0)
    spent = res.list_cost_usd or 0.0
    raised = round(cap * BUDGET_RAISE, 2)
    detail = {"session_id": res.session_id, "agent_key": kw.get("agent_key"), "title": kw.get("title"), "reason": reason,
              "budget_usd": cap, "list_cost_usd": res.list_cost_usd, "outcome_iterations": res.outcome_iterations,
              "attempt": attempt, "kept_as": f"{stage}/{kept}*"}
    live = LIVE_STAGE.get(stage, RunStage.IMPLEMENTING)
    with _TRANSITIONS:
        st = ctx.store.state(ctx.run_id).stage
        if reason == "budget_reached" and st not in TERMINAL:
            ctx.event("budget.reached", detail, stage=stage, actor="platform", work_item_id=wid, spec_id=spec_id)   # pauses the run
        else:
            ctx.event("session.truncated", detail, stage=stage, actor="platform", work_item_id=wid, spec_id=spec_id)
        if st in TERMINAL:   # another session's gate already closed or escalated the run: nothing left to ask
            raise SessionTruncated(f"{stage} session {res.session_id}: {reason}; the run is already {st.value}")
    if reason == "budget_reached":
        summary = (f"Session {res.session_id} ({kw.get('title')}) hit its ${cap:.2f} cap after ${spent:.2f} and stopped before "
                   f"finishing. Its files are kept under artifacts/{stage}/{kept}* but were not used as a result.\n\n"
                   f"Approve: run the session again with the cap raised to ${raised:.2f}. Reject: close the run.")
    else:
        summary = (f"Session {res.session_id} ({kw.get('title')}) stopped after {res.outcome_iterations} grader iterations without "
                   f"satisfying its rubric (max_iterations_reached, ${spent:.2f}). Its files are kept under artifacts/{stage}/{kept}* "
                   f"but were not used as a result.\n\nApprove: accept that last output as it stands; the judges and reviewers "
                   f"downstream still check it. Reject: close the run.")
    d = open_gate_and_wait(ctx, GateKind.BUDGET, {}, summary, assignees=_owner_handles(ctx),
                           options=(Decision.APPROVE, Decision.REJECT), work_item_id=wid)
    with _TRANSITIONS:
        st = ctx.store.state(ctx.run_id).stage
        if d.decision == Decision.APPROVE:
            if reason == "budget_reached" and st == RunStage.PAUSED_BUDGET:
                ctx.event("stage.entered", {"stage": live.value, "note": f"cap raised to ${raised:.2f} by {d.decided_by}"},
                          stage=stage, actor="human")
        elif st == RunStage.PAUSED_BUDGET:
            ctx.event("stage.entered", {"stage": RunStage.CLOSED.value}, stage=stage, actor="human")
        elif st not in TERMINAL:
            ctx.event("run.escalated", {"reason": f"{stage}: {reason}; the owner declined to continue"}, stage=stage, actor="human",
                      work_item_id=wid, spec_id=spec_id)
    if d.decision != Decision.APPROVE:
        raise SessionTruncated(f"{stage} session {res.session_id}: {reason}; the owner closed the run")
    if reason != "budget_reached":
        return res
    return run_session(ctx, stage=stage, prefix=prefix, spec_id=spec_id, attempt=attempt + 1, **{**kw, "budget_usd": raised})


def _owner_handles(ctx: RunContext) -> list[str]:
    try:
        return [json.loads(ctx.store.get_artifact(ctx.run_id, "intake", "outcome.json"))["owner"]["handle"]]
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        return []


def public(artifact: dict[str, Any]) -> dict[str, Any]:
    """The artefact without the orchestrator's private keys (`_markdown`, `_head_sha`, `_branch`, ...).

    Private keys travel with the object in memory between stages; what is persisted, uploaded to a session, validated
    against a schema or committed to the repository is always the public form."""
    return {k: v for k, v in artifact.items() if not k.startswith("_")}


def provenance(ctx: RunContext, res: StageResult, agent_name: str, model: str, inputs: dict[str, str]) -> dict[str, Any]:
    from datetime import datetime, timezone
    return {
        "agent_name": agent_name, "agent_id": res.agent_id, "agent_version": res.agent_version or 1,
        "session_id": res.session_id, "thread_id": None, "model": model, "effort": None,
        "outcome_iterations": res.outcome_iterations or None, "list_cost_usd": res.list_cost_usd,
        "produced_at": datetime.now(timezone.utc).isoformat(),
        "inputs": [{"artifact": k, "sha256": v} for k, v in inputs.items()],
    }


NO_REPO_RUBRIC_NOTE = """> **No repository is mounted in this session.** Criteria that require inspecting the code (paths or commands
> exist, grounding in the codebase, questions the repository would answer) cannot be verified here: score them on
> internal consistency and plausibility instead of 0, and say so in the evidence. Everything else applies in full.

"""


def rubric_text(ctx: RunContext, name: str, repo_available: bool) -> str:
    """The stage rubric, prefixed with a note when the session has no repository (design-only runs without a forge)."""
    text = ctx.rubric(name)
    return text if repo_available else NO_REPO_RUBRIC_NOTE + text


def persist_outputs(ctx: RunContext, stage: str, res: StageResult, prefix: str = "", *, work_item_id: str | None = None,
                    spec_id: str | None = None) -> dict[str, str]:
    """Store every file the session wrote; return name -> sha256. The `artifact.produced` and `session.finished`
    events carry the work item and spec the session served, so a change request's provenance table and the Console
    can attribute sessions and cost per spec (post-mortem issue 16: every session.finished had spec_id null)."""
    shas = {}
    for name, content in res.outputs.items():
        shas[name] = ctx.store.put_artifact(ctx.run_id, stage, prefix + name, content)
        ctx.event("artifact.produced", {"artifact_type": Path(name).stem, "path": f"{stage}/{prefix}{name}",
                                        "sha256": shas[name]}, stage=stage, work_item_id=work_item_id, spec_id=spec_id)
    ctx.event("session.finished", {"session_id": res.session_id, "agent_id": res.agent_id,
                                   "agent_version": res.agent_version, "list_cost_usd": res.list_cost_usd,
                                   "outcome_result": res.outcome_result, "stop_reason": res.stop_reason,
                                   "outcome_iterations": res.outcome_iterations},
              stage=stage, actor="platform", work_item_id=work_item_id, spec_id=spec_id)
    return shas


def stamp_provenance(artifact: dict[str, Any], ctx: RunContext, res: StageResult, agent_name: str, model: str,
                     inputs: dict[str, str] | None = None) -> dict[str, Any]:
    """Set `produced_by` from what the orchestrator knows (agent id and version, session id, cost, iterations, time).

    Agents cannot know their own session id or cost, so the schemas' required `produced_by` made them invent
    placeholders ('sesn_unknown', model 'claude', a round timestamp) that `setdefault` then kept (post-mortem issue
    16). The agent's `inputs` (when well-formed), `thread_id` and `effort` are kept; everything else is overwritten."""
    theirs = artifact.get("produced_by") if isinstance(artifact.get("produced_by"), dict) else {}
    ours = provenance(ctx, res, agent_name, model, inputs or {})
    if not inputs:
        ours["inputs"] = [i for i in (theirs.get("inputs") or []) if isinstance(i, dict) and isinstance(i.get("sha256"), str)
                          and len(i["sha256"]) == 64 and isinstance(i.get("artifact"), str)]
    for key in ("thread_id", "effort"):
        if theirs.get(key) is not None:
            ours[key] = theirs[key]
    artifact["produced_by"] = ours
    return artifact


def artifact_json_or_none(ctx: RunContext, stage: str, name: str) -> Any:
    """A persisted artefact parsed as JSON, or None when the store does not hold it (used by `factory resume`)."""
    try:
        return json.loads(ctx.store.get_artifact(ctx.run_id, stage, name))
    except FileNotFoundError:
        return None


def require_json(res: StageResult, name: str) -> dict[str, Any]:
    if name not in res.outputs:
        raise RuntimeError(f"session {res.session_id} did not produce {name}; outputs: {sorted(res.outputs)}")
    return json.loads(res.outputs[name])


def make_ask_human(ctx: RunContext, role: str, poll_seconds: float = 5.0, timeout_hours: float = 48.0):
    """Custom tool handler: surface the question, block until answered, return the answer text.

    Non-blocking questions return their default immediately and are logged for the gate.
    """
    def handler(tool_input: dict[str, Any], sctx: SessionContext) -> str:
        q = Question(run_id=ctx.run_id, asked_by=role, question=tool_input["question"],
                     why_it_matters=tool_input.get("why_it_matters", ""),
                     default_if_unanswered=tool_input.get("default_if_unanswered", ""),
                     blocking=bool(tool_input.get("blocking", False)),
                     options=tuple(tool_input.get("options") or ()),
                     session_id=sctx.session_id, custom_tool_use_id=sctx.custom_tool_use_id,
                     work_item_id=sctx.work_item_id)
        ctx.queue.ask(q)
        ctx.notifier.question_asked(q)
        ctx.event("question.asked", {"question_id": q.question_id, "question": q.question, "blocking": q.blocking,
                                     "default": q.default_if_unanswered, "asked_by": role}, stage=sctx.stage,
                  actor="agent")
        if not q.blocking:
            return json.dumps({"answer": q.default_if_unanswered, "note": "non-blocking: default applied; "
                               "the human will see this question at the next gate"})
        deadline = time.time() + timeout_hours * 3600
        while time.time() < deadline:
            a: Answer | None = ctx.queue.answer(q.question_id)
            if a is not None:
                ctx.event("question.answered", {"question_id": q.question_id, "answer": a.answer,
                                                "answered_by": a.answered_by}, stage=sctx.stage, actor="human")
                return json.dumps({"answer": a.answer, "answered_by": a.answered_by})
            time.sleep(poll_seconds)
        return json.dumps({"answer": q.default_if_unanswered, "note": "no human answer within SLA; default applied"})
    return handler


def open_gate_and_wait(ctx: RunContext, kind: GateKind, artifacts: dict[str, str], summary: str,
                       assignees: list[str], options: tuple[Decision, ...], poll_seconds: float = 10.0,
                       urls: dict[str, str] | None = None, work_item_id: str | None = None) -> GateDecision:
    refs = tuple(ArtifactRef(name=n, sha256=s, url=(urls or {}).get(n)) for n, s in sorted(artifacts.items()))
    gate = Gate(run_id=ctx.run_id, kind=kind, artifacts=refs, assignees=tuple(assignees), summary=summary, options=options,
                work_item_id=work_item_id)
    ctx.queue.open(gate)
    ctx.notifier.gate_opened(gate)
    ctx.event("gate.opened", {"gate_id": gate.gate_id, "kind": kind.value, "artifacts": [r.__dict__ for r in refs],
                              "assignees": assignees, "summary": summary}, stage=kind.value)
    while True:
        d = ctx.queue.decision(gate.gate_id)
        if d is not None:
            ctx.event("gate.decided", {"gate_id": gate.gate_id, "kind": kind.value, "decision": d.decision.value,
                                       "comments": d.comments, "approved_subset": list(d.approved_subset),
                                       "artifact_shas": list(d.artifact_shas)}, stage=kind.value, actor="human")
            return d
        time.sleep(poll_seconds)
