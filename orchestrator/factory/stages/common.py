"""Helpers shared by stage modules."""
from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import UTC
from pathlib import Path
from typing import Any

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
    res = ctx.driver.run(stage=stage, **kw)
    record_session_finished(ctx, stage, res, work_item_id=kw.get("work_item_id"), spec_id=spec_id)
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
        "produced_at": datetime.now(UTC).isoformat(),
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
            "Give UI criteria a `factory:journey <name>` e2e entry with explicit steps and a viewport: the factory's own "
            "browser runner executes the journey the implementer writes as a run artifact, so no browser-test code is "
            "committed (platform-contract skill; post-mortem 2026-10-11 §1). The recipe's baseline is only a "
            "readiness check; each changed behavior needs its own evidence. Recipe/network/credential-scope changes "
            "require re-onboarding and must not be smuggled into the implementation. Production authentication, "
            "paid inference or unsupported external services must be described as coverage gaps. Post-merge live "
            "deployment checks require a separate hosted adapter and currently remain blocked in managed mode. "
            "Automated criteria require machine-readable test evidence from pytest or a direct, literal `bun test ...` "
            "test_plan.commands entry. For Bun, avoid package scripts, shell wrappers/operators, environment prefixes "
            "and reporter flags: the controller adds its own JUnit reporter. Bun test IDs are "
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
    if not getattr(res, "finish_recorded", False):
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
            ctx.event("question.answered", {"question_id": q.question_id, "answer": q.default_if_unanswered,
                                            "answered_by": "default", "reason": "non_blocking"},
                      stage=sctx.stage, work_item_id=sctx.work_item_id)
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
        ctx.event("question.answered", {"question_id": q.question_id, "answer": q.default_if_unanswered,
                                        "answered_by": "default", "reason": "timeout"},
                  stage=sctx.stage, work_item_id=sctx.work_item_id)
        return json.dumps({"answer": q.default_if_unanswered, "note": "no human answer within SLA; default applied"})
    return handler


def open_gate(ctx: RunContext, kind: GateKind, artifacts: dict[str, str], summary: str, assignees: list[str],
              options: tuple[Decision, ...], urls: dict[str, str] | None = None, work_item_id: str | None = None) -> Gate:
    """Open a gate on every surface and record `gate.opened`. Does not wait: `gate_decision` polls for the answer, so a
    process that must keep doing other work (the change-request watcher) can open a gate without blocking on it."""
    refs = tuple(ArtifactRef(name=n, sha256=s, url=(urls or {}).get(n)) for n, s in sorted(artifacts.items()))
    gate = Gate(run_id=ctx.run_id, kind=kind, artifacts=refs, assignees=tuple(assignees), summary=summary, options=options,
                work_item_id=work_item_id)
    ctx.queue.open(gate)
    ctx.notifier.gate_opened(gate)
    ctx.event("gate.opened", {"gate_id": gate.gate_id, "kind": kind.value, "artifacts": [r.__dict__ for r in refs],
                              "assignees": assignees, "summary": summary, "work_item_id": work_item_id},
              stage=kind.value, work_item_id=work_item_id)
    return gate


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


def open_gate_and_wait(ctx: RunContext, kind: GateKind, artifacts: dict[str, str], summary: str,
                       assignees: list[str], options: tuple[Decision, ...], poll_seconds: float = 10.0,
                       urls: dict[str, str] | None = None, work_item_id: str | None = None,
                       auto_approve: str | None = None) -> GateDecision:
    """Open the gate and wait for its decision. `auto_approve` (from `auto_approval`) approves it at once, as "policy"."""
    gate = open_gate(ctx, kind, artifacts, summary, assignees, options, urls, work_item_id)
    if auto_approve and Decision.APPROVE in options:
        _record_policy_decision(ctx, gate, auto_approve)
    while True:
        d = gate_decision(ctx, gate.gate_id, kind, work_item_id)
        if d is not None:
            return d
        time.sleep(poll_seconds)
