"""Helpers shared by stage modules."""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any

from .. import steps
from ..budget import owner_handle, reserve_budget
from ..gates import Answer, ArtifactRef, Decision, Gate, GateDecision, GateKind, Question
from ..sessions import SessionContext, StageResult
from ..state import RunStage, WorkItemStage
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
    res = session_step(ctx, stage=stage, prefix=prefix, spec_id=spec_id, attempt=attempt, **kw)
    if res.stop_reason in {"terminated", "interrupted", "unreachable"} or res.outcome_result in {"interrupted", "failed"}:
        persist_outputs(ctx, stage, res, prefix=f"{prefix}unfinished{attempt}-",
                        work_item_id=kw.get("work_item_id"), spec_id=spec_id)
        raise SessionTruncated(f"{stage} session {res.session_id} did not finish: "
                               f"{res.outcome_result or res.stop_reason}; its outputs were retained, not accepted")
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
    # The re-run commits a cap the first reservation did not cover: check it against the run budget like any session
    # (budget.py); when it does not fit, the run pauses at its budget instead of starting the session.
    reserve_budget(ctx, stage, 1, cap_usd=raised)
    return run_session(ctx, stage=stage, prefix=prefix, spec_id=spec_id, attempt=attempt + 1, **{**kw, "budget_usd": raised})


def session_step_key(stage: str, prefix: str, title: str, attempt: int) -> str:
    """The step a session runs under. Titles name the run, the stage, the spec and the round; prefixes add what binds a
    session to its inputs (a reviewer pass's base and head, a judge's recheck number); `attempt` is the budget re-run.
    The pipeline step it runs in (`steps.scope`) comes first: the second decomposition pass asks the same agent under
    the same title, and is a different session."""
    scope = steps.scope()
    return f"session/{stage}/{scope + '/' if scope else ''}{prefix}{title}#{attempt}"


def would_reuse(ctx: RunContext, *, stage: str, prefix: str = "", attempt: int = 1, **kw: Any) -> bool:
    """`session_step` would answer this call from a session that already finished: nothing starts, so nothing needs
    reserving against the run budget (a replayed step that paused at its budget must not pay for its sessions twice)."""
    step = ctx.store.step(ctx.run_id, session_step_key(stage, prefix, str(kw.get("title", "")), attempt))
    return (step is not None and step.status == "done" and step.inputs_sha == session_inputs_sha(stage, kw)
            and _reused(ctx, step, str(kw.get("agent_key", ""))) is not None)


def session_inputs_sha(stage: str, kw: dict[str, Any]) -> str:
    """Everything that decides what a session is asked to do: who, with which instructions, model and limits, on
    which files and repository. Two calls with the same hash ask for the same work. The repository's credential is
    left out: it says how to reach the work, not what it is, and a rotated token must not supersede a session."""
    uploads = kw.get("uploads") or {}
    repository = kw.get("repository")
    if isinstance(repository, dict):
        repository = {k: v for k, v in repository.items() if k != "authorization_token"}
    # The dollar cap is left out too: it bounds the work, it does not change it, and a controller with other caps
    # must still follow (or reuse) the session it finds.
    doc = {"stage": stage, **{k: kw.get(k) for k in ("agent_key", "title", "description", "rubric_md", "max_iterations",
                                                     "model_override", "system_override", "environment_id",
                                                     "vault_ids", "metadata", "schema_names")},
           "repository": repository,
           "custom_tools": sorted(kw.get("custom_tools") or {}),
           "uploads": {name: hashlib.sha256(content).hexdigest() for name, content in sorted(uploads.items())}}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, default=str).encode()).hexdigest()


STOPPED = {"interrupted", "unreachable"}   # how `_drive` ends the sessions in flight when someone stops the run
UNFINISHED = {"terminated", "interrupted", "unreachable"}   # stop reasons `run_session` refuses as a result


def session_step(ctx: RunContext, *, stage: str, prefix: str = "", spec_id: str | None = None, attempt: int = 1,
                 fresh: bool = False, **kw: Any) -> StageResult:
    """`ctx.driver.run` as a durable step (ADR-0013). The step records the session's id as soon as it exists, so a
    controller that dies leaves a session its successor can follow: a call with the same step and the same inputs
    reattaches to a session still running under it instead of starting (and paying for) another. A running session
    whose inputs have since changed is interrupted and recorded as superseded, and one a stop interrupted is not
    followed; a new session starts in both cases.

    A session that finished is reused when the same step asks for the same work again: its recorded outputs come back
    without a session, which is how a replayed pipeline step that did not finish gets past the sessions that did. Not
    reused: `fresh` callers (hosted verification: a resume always verifies again), a session that did not finish, and
    one whose output the stage refused (`reject_output`: its step is failed, so the next call starts a new session).
    `session.finished` is recorded before the step closes, so the spend of a session that returned is counted once
    whichever side of that boundary a crash falls on."""
    store, run_id = ctx.store, ctx.run_id
    wid = kw.get("work_item_id")
    key = session_step_key(stage, prefix, str(kw.get("title", "")), attempt)
    inputs = session_inputs_sha(stage, kw)
    step = store.step(run_id, key)
    if step is not None and step.status == "done" and step.inputs_sha == inputs and not fresh:
        reused = _reused(ctx, step, str(kw.get("agent_key", "")))
        if reused is not None:
            return _rejectable(ctx, key, reused)
    if step is not None and step.status == "running" and step.session_id:
        ended = store.state(run_id).finished_sessions.get(step.session_id)
        if ended in STOPPED:
            store.fail_step(run_id, key, f"{ended}: the run was stopped while this session worked")
        elif step.inputs_sha == inputs:
            ctx.event("session.reattached", {"session_id": step.session_id, "step": key, "attempt": step.attempt},
                      stage=stage, actor="platform", work_item_id=wid, spec_id=spec_id)
            res = ctx.driver.run(stage=stage, attach_session_id=step.session_id, **kw)
            return _rejectable(ctx, key, _finish_step(ctx, stage, key, res, wid, spec_id))
        else:
            _supersede(ctx, stage, key, step.session_id, wid, spec_id)
    store.begin_step(run_id, key, "session", inputs)
    created: list[str] = []

    def on_created(session_id: str) -> None:
        created.append(session_id)
        store.set_step_session(run_id, key, session_id)

    try:
        res = ctx.driver.run(stage=stage, on_created=on_created, **kw)
    except BaseException as e:
        if not created:
            store.fail_step(run_id, key, f"{type(e).__name__}: {str(e)[:500]}")
        raise          # a session that exists stays `running` under its step, for the next controller to reattach to
    return _rejectable(ctx, key, _finish_step(ctx, stage, key, res, wid, spec_id))


def _rejectable(ctx: RunContext, key: str, res: StageResult) -> StageResult:
    try:
        res.on_reject = lambda why: ctx.store.fail_step(ctx.run_id, key, f"output rejected: {why}")
    except AttributeError:            # a test double that is not a StageResult
        pass
    return res


def reject_output(res: StageResult, why: str) -> RuntimeError:
    """Refuse a session's output: its step is failed, so a replay starts a new session for this work rather than
    reusing what was refused. Returns the error for the caller to raise."""
    reject = getattr(res, "on_reject", None)
    if callable(reject):
        reject(why)
    return RuntimeError(why)


def _finish_step(ctx: RunContext, stage: str, key: str, res: StageResult, work_item_id: str | None,
                 spec_id: str | None) -> StageResult:
    res.finished_at = getattr(res, "finished_at", None) or datetime.now(UTC).isoformat()
    record_session_finished(ctx, stage, res, work_item_id=work_item_id, spec_id=spec_id)
    ctx.store.finish_step(ctx.run_id, key, _step_result(ctx, res))
    return res


def _step_result(ctx: RunContext, res: StageResult) -> dict[str, Any]:
    """What a finished session step records: how the session ended and the files it returned, kept as blobs and named
    by sha256, so a step's result can be read back without the stage code that persisted it (replays, evals)."""
    return {"session_id": res.session_id, "agent_id": res.agent_id, "agent_version": res.agent_version,
            "stop_reason": res.stop_reason, "outcome_result": res.outcome_result,
            "outcome_iterations": res.outcome_iterations, "list_cost_usd": res.list_cost_usd,
            "events_seen": getattr(res, "events_seen", 0), "custom_tool_calls": list(getattr(res, "custom_tool_calls", None) or []),
            "last_message": getattr(res, "last_message", None), "finished_at": res.finished_at,
            "outputs": {name: ctx.store.put_blob(ctx.run_id, content) for name, content in sorted((res.outputs or {}).items())}}


def _reused(ctx: RunContext, step: Any, agent_key: str) -> StageResult | None:
    """The StageResult a finished session step recorded, or None when it must not stand in for a new session: it did
    not finish, or its outputs are not kept (a step recorded before they were)."""
    r = step.result or {}
    if (r.get("stop_reason") in UNFINISHED or r.get("outcome_result") in {"interrupted", "failed"}
            or not isinstance(r.get("outputs"), dict) or not r.get("session_id")):
        return None
    try:
        outputs = {name: ctx.store.get_blob(ctx.run_id, sha) for name, sha in r["outputs"].items()}
    except (FileNotFoundError, ValueError):
        return None
    agent = ctx.cfg.agents.get(agent_key) if agent_key else None
    return StageResult(session_id=r["session_id"], agent_id=r.get("agent_id") or getattr(agent, "id", ""),
                       agent_version=r.get("agent_version") or getattr(agent, "version", None), outputs=outputs,
                       outcome_result=r.get("outcome_result"), outcome_iterations=int(r.get("outcome_iterations") or 0),
                       list_cost_usd=r.get("list_cost_usd"), stop_reason=r.get("stop_reason"),
                       events_seen=int(r.get("events_seen") or 0), custom_tool_calls=list(r.get("custom_tool_calls") or []),
                       finish_recorded=True, last_message=r.get("last_message"),
                       finished_at=r.get("finished_at") or step.updated_at)


CHANGED_INPUTS = "its inputs changed before a controller followed it to the end"


def _supersede(ctx: RunContext, stage: str, key: str, session_id: str, work_item_id: str | None,
               spec_id: str | None, reason: str = CHANGED_INPUTS) -> None:
    """Stop a session nobody will follow (its inputs changed while no controller watched it), charge what it spent
    and record why."""
    stop = getattr(ctx.driver, "interrupt_session", None)
    reached = bool(stop(session_id)) if callable(stop) else False
    ctx.event("session.superseded", {"session_id": session_id, "step": key, "interrupted": reached, "reason": reason},
              stage=stage, actor="platform", work_item_id=work_item_id, spec_id=spec_id)
    if session_id not in ctx.store.state(ctx.run_id).finished_sessions:
        cost = getattr(ctx.driver, "list_cost", None)
        ctx.event("session.finished", {"session_id": session_id, "list_cost_usd": cost(session_id) if callable(cost) else None,
                                       "outcome_result": "interrupted", "stop_reason": "superseded" if reached else "unreachable"},
                  stage=stage, actor="platform", work_item_id=work_item_id, spec_id=spec_id)
    ctx.store.fail_step(ctx.run_id, key, f"superseded: {reason}")


def orphaned_sessions(ctx: RunContext) -> list[str]:
    """Sessions working under a step no controller follows: what a dead controller left running. Read before the
    resumed pipeline starts, they are the sessions it will reattach to (or supersede) when it reaches their steps."""
    return [s.session_id for s in ctx.store.steps(ctx.run_id) if s.kind == "session" and s.status == "running" and s.session_id]


def close_orphaned_sessions(ctx: RunContext) -> list[str]:
    """Once a run has ended, stop and charge every session still running under a step it never reached again (a
    resumed run took another path: an escalation, a different decision at a gate). A run that only paused (at its
    budget, at a gate) keeps them for the resume that follows. Returns their ids."""
    if ctx.store.state(ctx.run_id).stage not in TERMINAL:
        return []
    closed = []
    for s in ctx.store.steps(ctx.run_id):
        if s.kind == "session" and s.status == "running" and s.session_id:
            _supersede(ctx, s.key.split("/")[1], s.key, s.session_id, None, None,
                       reason="the run ended without reaching its step again")
            closed.append(s.session_id)
    return closed


def _owner_handles(ctx: RunContext) -> list[str]:
    handle = owner_handle(ctx)
    return [handle] if handle else []


def recorded_preview_modes(events: list[Any]) -> list[str]:
    """Every preview mode the run was driven in, in order: run.created's policy, then each resume that recorded one."""
    modes = []
    for ev in events:
        if ev.kind == "run.created":
            mode = (ev.payload.get("policy") or {}).get("preview_mode")
        elif ev.kind == "run.resumed":
            mode = ev.payload.get("preview_mode")
        else:
            continue
        if mode:
            modes.append(mode)
    return modes


def recorded_preview_mode(events: list[Any]) -> str | None:
    """The preview mode the run was last driven in, or None when none was recorded."""
    modes = recorded_preview_modes(events)
    return modes[-1] if modes else None


def hosted_run(events: list[Any]) -> bool:
    """The run has been verified in a hosted sandbox (preview mode managed) at some point, at creation or after a recovery.
    Such a run never continues with execution on the controller's host, whatever this process is configured with."""
    return "managed" in recorded_preview_modes(events)


def git(repo: Path, *args: str) -> str:
    """A git command in `repo` (the orchestrator's own checkout of the target repository); its stdout."""
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def public(artifact: dict[str, Any]) -> dict[str, Any]:
    """The artefact without the orchestrator's private keys (`_markdown`, `_head_sha`, `_branch`, ...).

    Private keys travel with the object in memory between stages; what is persisted, uploaded to a session, validated
    against a schema or committed to the repository is always the public form."""
    return {k: v for k, v in artifact.items() if not k.startswith("_")}


def enter_item_stage(ctx: RunContext, work_item_id: str, spec_id: str | None, stage: WorkItemStage, **payload: Any) -> None:
    """Emit `work_item.stage` unless the item is already there. A resumed loop (`factory resume`, after a crash or
    a budget pause) re-walks steps whose outputs it reuses; re-emitting their stage would be an illegal
    self-transition on replay."""
    current = ctx.store.state(ctx.run_id).work_items.get(work_item_id)
    if current is not None and current.stage == stage:
        return
    ctx.event("work_item.stage", {"stage": stage.value, **payload}, work_item_id=work_item_id, spec_id=spec_id)


def provenance(ctx: RunContext, res: StageResult, agent_name: str, model: str, inputs: dict[str, str]) -> dict[str, Any]:
    from datetime import datetime
    return {
        "agent_name": agent_name, "agent_id": res.agent_id, "agent_version": res.agent_version or 1,
        "session_id": res.session_id, "thread_id": None, "model": model, "effort": None,
        "outcome_iterations": res.outcome_iterations or None, "list_cost_usd": res.list_cost_usd,
        "produced_at": getattr(res, "finished_at", None) or datetime.now(UTC).isoformat(),
        "inputs": [{"artifact": k, "sha256": v} for k, v in inputs.items()],
    }


SKILL_UPLOAD_PATH = "skills/platform-contract/SKILL.md"
SKILL_CLAUSE = ("The platform-contract skill is at /mnt/session/uploads/skills/platform-contract/SKILL.md (a repository that "
                "carries it also has it at /workspace/repo/.claude/skills/platform-contract/SKILL.md). Read it first; do not "
                "search the filesystem for it.")
NO_SKILL_CLAUSE = ("No platform-contract skill is available for this run (the orchestrator has none to upload). Say so once "
                   "in your assumptions and do not search the filesystem for one.")


def size_clause(ctx: RunContext) -> str:
    """The run's size budget, for every agent that sizes work (decomposer, spec writer, spec judge)."""
    from ..size import DEFAULT_BUDGET, describe_budget
    budget = getattr(ctx.policy, "size_budget_lines", None) or DEFAULT_BUDGET
    return (f"\nSize budget for this run, in hand-written changed lines (added plus removed, tests included; generated files "
            f"excluded: linguist-generated in .gitattributes, lockfiles, files regenerated by a named command): {describe_budget(budget)}. "
            "Above M is split.")


def hosted_planning_clause(ctx: RunContext) -> str:
    if ctx.cfg.preview_mode != "managed":
        return ""
    return ("\nThis app uses managed verification. Every application, database and browser test must run inside an "
            "Anthropic-hosted test sandbox built from the repository's approved test_environment recipe. Read "
            "/mnt/session/uploads/schemas/test_environment.schema.json for the exact recipe and secret declaration "
            "fields; do not invent credential_ref or other fields. Never depend "
            "on a person's localhost, Claude in Chrome, factory:browser, or an externally reachable preview URL. "
            "The controller provides BASE_URL inside that sandbox. Both local and preview e2e targets execute there. "
            "Give UI criteria a journey e2e entry ({\"journey\": \"<name>\", ...}) with explicit steps and a viewport: the factory's own "
            "browser runner executes the journey the implementer writes as a run artifact, so no browser-test code is "
            "committed (platform-contract skill; post-mortem 2026-10-11 §1). The recipe's baseline is only a "
            "readiness check; each changed behavior needs its own evidence. A change to the recipe's trusted part "
            "(network, secrets, data, command_environment) requires re-onboarding and must not be smuggled into the "
            "implementation; its commands, services and checks may change with the code and are verified like it. Production authentication, "
            "paid inference or unsupported external services must be described as coverage gaps. Post-merge live "
            "deployment checks require a separate hosted adapter and currently remain blocked in managed mode. "
            "Automated criteria require machine-readable test evidence: write every test run as a structured "
            "test_plan.commands entry, {\"runner\": \"pytest\" or \"bun\", \"select\": [test files or ids]}; the controller "
            "renders the command and adds its own reporters. Bun test IDs are "
            "path/to/file.test.ts::outer describe > inner describe > test name. An exit code alone or an unsupported "
            "framework cannot verify an automated criterion; state that limitation instead of assuming support. "
            "An automated criterion proved by an e2e journey itself can map to e2e:<zero-based index> instead of a "
            "unit-test ID. The entry must name that criterion in covers_ac and pass with its required current browser "
            "evidence; the controller validates this mapping against the approved specification.")


def platform_skill(ctx: RunContext) -> tuple[dict[str, bytes], str]:
    """The upload (`skills/platform-contract/SKILL.md`) and the kickoff sentence that names it, for every stage.

    Sessions on an existing repository have no other copy of the skill until the forge seeds it, and a brief that
    says "read the skill first" without a path sends agents hunting the filesystem (post-mortem 2026-10-10, issue 12).
    An absent skill file yields no upload and a sentence saying so."""
    f = ctx.cfg.skills_dir / "platform-contract" / "SKILL.md"
    if not f.is_file():
        return {}, NO_SKILL_CLAUSE
    return {SKILL_UPLOAD_PATH: f.read_bytes()}, SKILL_CLAUSE


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
    record_session_finished(ctx, stage, res, work_item_id=work_item_id, spec_id=spec_id)
    return shas


def record_session_finished(ctx: RunContext, stage: str, res: StageResult, *, work_item_id: str | None = None,
                            spec_id: str | None = None) -> None:
    """Charge returned sessions before parsing outputs, including missing/malformed artifacts."""
    # A truncated result is saved before its gate, then saved under the normal prefix
    # if the owner accepts it. Both artifact sets are useful; the spend is counted once.
    # A controller that reattached to a session its predecessor saw finish reports the same end again (ADR-0013).
    if getattr(res, "finish_recorded", False) or res.session_id in ctx.store.state(ctx.run_id).finished_sessions:
        res.finish_recorded = True
        return
    ctx.event("session.finished", {"session_id": res.session_id, "agent_id": res.agent_id,
                                   "agent_version": res.agent_version, "list_cost_usd": res.list_cost_usd,
                                   "outcome_result": res.outcome_result, "stop_reason": res.stop_reason,
                                   "outcome_iterations": res.outcome_iterations},
              stage=stage, actor="platform", work_item_id=work_item_id, spec_id=spec_id)
    res.finish_recorded = True


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
    """The JSON output `name`. A session that did not write it, or wrote something that is not JSON, has its output
    rejected (`reject_output`)."""
    if name not in res.outputs:
        raise reject_output(res, f"session {res.session_id} did not produce {name}; outputs: {sorted(res.outputs)}")
    try:
        return json.loads(res.outputs[name])
    except ValueError as e:
        raise reject_output(res, f"session {res.session_id} wrote {name} that is not JSON: {e}") from e


def schema_problems(ctx: RunContext, artifact: dict[str, Any], schema: str) -> list[str]:
    """`artifact`'s schema errors, without the orchestrator's private keys, at most eight."""
    from .. import checks
    errors = checks.schema_errors(public(artifact), ctx.cfg.schemas_dir / schema, ctx.cfg.schemas_dir)
    return [f"{'/'.join(str(x) for x in e.path) or '$'}: {e.message[:300]}" for e in errors[:8]]


def require_valid(ctx: RunContext, res: StageResult, name: str, artifact: dict[str, Any], schema: str) -> dict[str, Any]:
    """`artifact`, the session's `name` as the stage completed it, validated on receipt (rule `artifact-schema`). An
    invalid verdict or set of findings is refused (`reject_output`): nothing the schema does not hold reaches a gate,
    the merge judge or the change request."""
    problems = schema_problems(ctx, artifact, schema)
    if problems:
        raise reject_output(res, f"session {res.session_id} wrote {name} that does not validate against {schema} "
                                 f"(rule artifact-schema): {'; '.join(problems)}")
    return artifact


def make_ask_human(ctx: RunContext, role: str, poll_seconds: float = 5.0, timeout_hours: float = 48.0):
    """Custom tool handler: surface the question, block until answered, return the answer text.

    Non-blocking questions return their default immediately and are logged for the gate. A call the session already
    made (the same `custom_tool_use_id`, replayed to a controller that reattached to the session; ADR-0013) is not asked
    again: an answered question returns its answer, an open one is waited on where its first asking left off.
    """
    def wait(question_id: str, default: str, stage: str | None, work_item_id: str | None, deadline: float) -> str:
        while time.time() < deadline:
            a: Answer | None = ctx.queue.answer(question_id)
            if a is not None:
                ctx.event("question.answered", {"question_id": question_id, "answer": a.answer,
                                                "answered_by": a.answered_by}, stage=stage, actor="human")
                return json.dumps({"answer": a.answer, "answered_by": a.answered_by})
            time.sleep(poll_seconds)
        ctx.event("question.answered", {"question_id": question_id, "answer": default,
                                        "answered_by": "default", "reason": "timeout"},
                  stage=stage, work_item_id=work_item_id)
        return json.dumps({"answer": default, "note": "no human answer within SLA; default applied"})

    def handler(tool_input: dict[str, Any], sctx: SessionContext) -> str:
        asked = _asked_before(ctx, sctx.custom_tool_use_id)
        if asked is not None:
            p, answered = asked
            if answered is not None:
                return json.dumps({"answer": answered.get("answer"), "answered_by": answered.get("answered_by")})
            started = datetime.fromisoformat(p["_at"]).timestamp()
            return wait(p["question_id"], p.get("default", ""), sctx.stage, sctx.work_item_id, started + timeout_hours * 3600)
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
                                     "default": q.default_if_unanswered, "asked_by": role,
                                     "custom_tool_use_id": sctx.custom_tool_use_id}, stage=sctx.stage,
                  actor="agent")
        if not q.blocking:
            ctx.event("question.answered", {"question_id": q.question_id, "answer": q.default_if_unanswered,
                                            "answered_by": "default", "reason": "non_blocking"},
                      stage=sctx.stage, work_item_id=sctx.work_item_id)
            return json.dumps({"answer": q.default_if_unanswered, "note": "non-blocking: default applied; "
                               "the human will see this question at the next gate"})
        return wait(q.question_id, q.default_if_unanswered, sctx.stage, sctx.work_item_id, time.time() + timeout_hours * 3600)
    return handler


def _asked_before(ctx: RunContext, custom_tool_use_id: str | None) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    """The `question.asked` payload (with its time as `_at`) a tool call already produced, and its `question.answered`
    payload when there is one; None for a call not seen before."""
    if not custom_tool_use_id:
        return None
    events = ctx.store.events(ctx.run_id)
    asked = next((ev for ev in events if ev.kind == "question.asked" and ev.payload.get("custom_tool_use_id") == custom_tool_use_id), None)
    if asked is None:
        return None
    qid = asked.payload["question_id"]
    answered = next((ev.payload for ev in events if ev.kind == "question.answered" and ev.payload.get("question_id") == qid), None)
    return {**asked.payload, "_at": asked.at}, answered


def open_gate(ctx: RunContext, kind: GateKind, artifacts: dict[str, str], summary: str, assignees: list[str],
              options: tuple[Decision, ...], urls: dict[str, str] | None = None, work_item_id: str | None = None,
              identity: str | None = None) -> Gate:
    """Open a gate on every surface and record `gate.opened`. Does not wait: `gate_decision` polls for the answer, so a
    process that must keep doing other work (the change-request watcher) can open a gate without blocking on it.
    `identity` (`gate_identity`) is how a replayed pipeline finds this gate again instead of opening another."""
    refs = tuple(ArtifactRef(name=n, sha256=s, url=(urls or {}).get(n)) for n, s in sorted(artifacts.items()))
    gate = Gate(run_id=ctx.run_id, kind=kind, artifacts=refs, assignees=tuple(assignees), summary=summary, options=options,
                work_item_id=work_item_id)
    ctx.queue.open(gate)
    ctx.notifier.gate_opened(gate)
    ctx.event("gate.opened", {"gate_id": gate.gate_id, "kind": kind.value, "artifacts": [r.__dict__ for r in refs],
                              "assignees": assignees, "summary": summary, "work_item_id": work_item_id,
                              "options": [o.value for o in options],
                              **({"identity": identity, "scope": steps.scope()} if identity else {})},
              stage=kind.value, work_item_id=work_item_id)
    return gate


def gate_identity(kind: GateKind, artifacts: dict[str, str], work_item_id: str | None, summary: str) -> str:
    """What makes two openings the same gate: the step that opens it (`steps.scope`), its kind, its work item and the
    exact artefacts it presents (the summary when it presents none: a session's budget gate names the session). A
    replayed step that reaches the same gate finds it; a loop's next iteration is another step, so another gate."""
    doc = {"scope": steps.scope(), "kind": kind.value, "work_item_id": work_item_id, "artifacts": dict(sorted(artifacts.items())),
           "summary": None if artifacts else summary}
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def find_gate(ctx: RunContext, identity: str, kind: GateKind, artifacts: dict[str, str],
              work_item_id: str | None) -> tuple[dict[str, Any], dict[str, Any] | None] | None:
    """The `gate.opened` payload of the gate with this identity and its `gate.decided` payload when it has one, or None.
    A gate an older controller opened (no identity recorded) matches on kind, work item and artefacts, as long as it
    presented any. A superseded gate is gone."""
    events = ctx.store.events(ctx.run_id)
    superseded = {ev.payload.get("gate_id") for ev in events if ev.kind == "gate.superseded"}

    def same(p: dict[str, Any]) -> bool:
        if p.get("identity") is not None:
            return p["identity"] == identity
        return (bool(artifacts) and p.get("kind") == kind.value and p.get("work_item_id") == work_item_id
                and {a["name"]: a["sha256"] for a in p.get("artifacts", [])} == artifacts)
    opened = next((ev.payload for ev in reversed(events) if ev.kind == "gate.opened" and same(ev.payload)
                   and ev.payload.get("gate_id") not in superseded), None)
    if opened is None:
        return None
    decided = next((ev.payload for ev in events if ev.kind == "gate.decided" and ev.payload.get("gate_id") == opened["gate_id"]), None)
    return opened, decided


def decision_from_event(p: dict[str, Any]) -> GateDecision:
    """The GateDecision a `gate.decided` event recorded."""
    return GateDecision(gate_id=p["gate_id"], decision=Decision(p["decision"]), decided_by=p.get("decided_by", "human"),
                        comments=p.get("comments", ""), approved_subset=tuple(p.get("approved_subset", [])),
                        artifact_shas=tuple(p.get("artifact_shas", [])))


def queued_gate(queue: Any, gate_id: str) -> Gate | None:
    """The gate as a decision surface holds it (the record decisions are validated against), from any composed queue."""
    for q in getattr(queue, "queues", [queue]):
        finder = getattr(q, "gate", None)
        gate = finder(gate_id) if callable(finder) else getattr(q, "gates", {}).get(gate_id)
        if isinstance(gate, Gate):
            return gate
    return None


def _rejoin_gate(ctx: RunContext, opened: dict[str, Any], kind: GateKind, options: tuple[Decision, ...]) -> Gate:
    """Wait again on a gate the log opened: the surface must hold the gate the log recorded (a decision is checked against
    that record), or, when it holds none (a surface that keeps no records across processes), gets it back as recorded."""
    refs = tuple(ArtifactRef(name=a["name"], sha256=a["sha256"], url=a.get("url")) for a in opened.get("artifacts", []))
    held = queued_gate(ctx.queue, opened["gate_id"])
    if held is not None:
        if (held.kind != kind or {a.name: a.sha256 for a in held.artifacts} != {r.name: r.sha256 for r in refs}
                or set(held.options) != set(options)):
            raise RuntimeError(f"the gate surface's record of gate {opened['gate_id']} differs from the gate the run opened; "
                               f"a decision could not be checked against what it presented")
        return held
    gate = Gate(run_id=ctx.run_id, kind=kind, artifacts=refs, assignees=tuple(opened.get("assignees") or ()),
                summary=opened.get("summary", ""), options=options, gate_id=opened["gate_id"],
                work_item_id=opened.get("work_item_id"))
    ctx.queue.open(gate)
    return gate


def _supersede_stale_gates(ctx: RunContext, kind: GateKind, work_item_id: str | None) -> None:
    """Gates of this kind and item that the current step opened before and that are still open present something else
    now (the step ran again and produced different artefacts): nobody should decide on them any more."""
    scope = steps.scope()
    if scope is None:
        return
    st = ctx.store.state(ctx.run_id)
    for gate_id, p in list(st.open_gates.items()):
        if p.get("kind") == kind.value and p.get("work_item_id") == work_item_id and p.get("scope") == scope:
            ctx.event("gate.superseded", {"gate_id": gate_id, "reason": "the step that opened it ran again and presents "
                                                                         "different artefacts; decide on the new gate"},
                      stage=kind.value, work_item_id=work_item_id)


def gate_decision(ctx: RunContext, gate_id: str, kind: GateKind, work_item_id: str | None = None) -> GateDecision | None:
    """The human's decision on an open gate if one has been recorded on any surface, else None. Records `gate.decided`
    the first time it is seen; a caller that polls must act on the decision it gets back rather than ask again."""
    d = ctx.queue.decision(gate_id)
    if d is not None:
        ctx.event("gate.decided", {"gate_id": gate_id, "kind": kind.value, "decision": d.decision.value,
                                   "comments": d.comments, "approved_subset": list(d.approved_subset), "decided_by": d.decided_by,
                                   "artifact_shas": list(d.artifact_shas)}, stage=kind.value,
                  actor="policy" if d.decided_by == "policy" else "human",
                  work_item_id=work_item_id)
    return d


AUTO_GATES = ("gate_1", "gate_2")


def parse_auto_approve(raw: str | None) -> tuple[str, ...]:
    """FACTORY_AUTO_APPROVE: a comma list of AUTO_GATES, or "all"; empty means every gate waits for a person."""
    names = [n.strip().lower() for n in (raw or "").split(",") if n.strip()]
    if names == ["all"]:
        return AUTO_GATES
    unknown = [n for n in names if n not in AUTO_GATES]
    if unknown:
        raise ValueError(f"FACTORY_AUTO_APPROVE names {unknown}; only {', '.join(AUTO_GATES)} or all can be pre-approved")
    return tuple(dict.fromkeys(names))


def auto_approval(ctx: RunContext, gate: str, blockers: list[str]) -> str | None:
    """The comment a policy approval of `gate` records, or None when the run did not pre-approve it or something in it
    needs a person (`blockers`)."""
    if gate not in (getattr(ctx.policy, "auto_approve", ()) or ()) or blockers:
        return None
    return (f"Approved by policy: the owner pre-approved {gate} for this run (FACTORY_AUTO_APPROVE) and nothing in it "
            f"needs a person. Defaults apply to the non-blocking questions. The gate's page and hashes are kept for audit.")


def _record_policy_decision(ctx: RunContext, gate: Gate, comments: str) -> None:
    """Write a policy approval to the first surface that records decisions, so it takes the same validated path (and
    shows decided in the Console) as a person's."""
    shas = tuple(a.sha256 for a in gate.artifacts)
    for q in getattr(ctx.queue, "queues", [ctx.queue]):
        if hasattr(q, "record_decision"):
            q.record_decision(gate.gate_id, Decision.APPROVE.value, by="policy", comments=comments)
            return
        if hasattr(q, "decide"):
            q.decide(GateDecision(gate_id=gate.gate_id, decision=Decision.APPROVE, decided_by="policy", comments=comments,
                                  artifact_shas=shas))
            return
    raise RuntimeError(f"no gate surface can record a policy decision for {gate.gate_id}")


GATE_POLL_SECONDS = 10.0


def open_gate_and_wait(ctx: RunContext, kind: GateKind, artifacts: dict[str, str], summary: str,
                       assignees: list[str], options: tuple[Decision, ...], poll_seconds: float | None = None,
                       urls: dict[str, str] | Callable[[], dict[str, str]] | None = None, work_item_id: str | None = None,
                       auto_approve: str | None = None) -> GateDecision:
    """Open the gate and wait for its decision. `auto_approve` (from `auto_approval`) approves it at once, as "policy".

    A replayed step (ADR-0013) reaching a gate it already opened does not open another: a decided gate returns the
    recorded decision, an undecided one is waited on again. `urls` may be a callable, so a gate that is found rather than
    opened does not publish its page again."""
    identity = gate_identity(kind, artifacts, work_item_id, summary)
    found = find_gate(ctx, identity, kind, artifacts, work_item_id)
    if found is not None and found[1] is not None:
        return decision_from_event(found[1])
    if found is not None:
        gate = _rejoin_gate(ctx, found[0], kind, options)
    else:
        _supersede_stale_gates(ctx, kind, work_item_id)
        gate = open_gate(ctx, kind, artifacts, summary, assignees, options, urls() if callable(urls) else urls, work_item_id,
                         identity=identity)
    if auto_approve and Decision.APPROVE in options and ctx.queue.decision(gate.gate_id) is None:
        _record_policy_decision(ctx, gate, auto_approve)
    while True:
        d = gate_decision(ctx, gate.gate_id, kind, work_item_id)
        if d is not None:
            return d
        time.sleep(GATE_POLL_SECONDS if poll_seconds is None else poll_seconds)
