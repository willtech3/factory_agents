"""Helpers shared by stage modules."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..gates import Answer, ArtifactRef, Decision, Gate, GateDecision, GateKind, Question
from ..sessions import SessionContext, StageResult
from . import RunContext


def provenance(ctx: RunContext, res: StageResult, agent_name: str, model: str, inputs: dict[str, str]) -> dict[str, Any]:
    from datetime import datetime, timezone
    return {
        "agent_name": agent_name, "agent_id": res.agent_id, "agent_version": res.agent_version or 1,
        "session_id": res.session_id, "thread_id": None, "model": model, "effort": None,
        "outcome_iterations": res.outcome_iterations or None, "list_cost_usd": res.list_cost_usd,
        "produced_at": datetime.now(timezone.utc).isoformat(),
        "inputs": [{"artifact": k, "sha256": v} for k, v in inputs.items()],
    }


def persist_outputs(ctx: RunContext, stage: str, res: StageResult, prefix: str = "") -> dict[str, str]:
    """Store every file the session wrote; return name -> sha256."""
    shas = {}
    for name, content in res.outputs.items():
        shas[name] = ctx.store.put_artifact(ctx.run_id, stage, prefix + name, content)
        ctx.event("artifact.produced", {"artifact_type": Path(name).stem, "path": f"{stage}/{prefix}{name}",
                                        "sha256": shas[name]}, stage=stage)
    ctx.event("session.finished", {"session_id": res.session_id, "agent_id": res.agent_id,
                                   "agent_version": res.agent_version, "list_cost_usd": res.list_cost_usd,
                                   "outcome_result": res.outcome_result, "stop_reason": res.stop_reason},
              stage=stage, actor="platform")
    return shas


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
