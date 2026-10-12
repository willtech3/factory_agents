"""The run as its steps table shows it (ADR-0017): what the Console and `factory steps` read instead of reconstructing
the run from its events.

- **The pipeline:** every pipeline step (decomposition and spec passes, gates, approvals, each spec's implementation,
  its rounds and judgements, integrations) in the order it started, nested under the step it runs in, with its status,
  attempts, sessions and what they cost.
- **Live sessions:** session steps still running with a session. With a controller alive they are being followed;
  without one they are working on their own, under their caps, and a resume follows them.
- **What a resume does:** the pipeline steps it continues (those not finished), the sessions it follows, those it starts
  again (a stop interrupted them), and how many finished sessions it reuses without asking anyone.
- **Cost per step:** the list cost of the sessions under each step, rolled up to the steps above it.

Nothing here writes; the projection is a function of the steps table, the run's state and its session events.
"""
from __future__ import annotations

from typing import Any

from .state import RunEvent

STOPPED = {"interrupted", "unreachable"}      # sessions a stop interrupted: a resume starts their work again
HIDDEN = ("/name",)                            # bookkeeping sub-steps (a judgement's artefact name) the reader never needs


def _scope(key: str) -> str:
    """The pipeline step a session step ran in: `session/<stage>/<scope>/<prefix><title>#<attempt>`."""
    rest = key.split("/", 2)[2] if key.count("/") >= 2 else ""
    return rest.rsplit("/", 1)[0] if "/" in rest else ""


def _parent(key: str, keys: list[str]) -> str | None:
    """The innermost pipeline step `key` runs in: the longest other key that is a path prefix of it."""
    best = None
    for k in keys:
        if k != key and key.startswith(k + "/") and (best is None or len(k) > len(best)):
            best = k
    return best


def project(steps: list[Any], finished_sessions: dict[str, str], events: list[RunEvent], *, controller_alive: bool) -> dict[str, Any]:
    started = {e.payload.get("session_id"): e for e in events if e.kind == "session.started" and e.payload.get("session_id")}
    pipeline_steps = [s for s in steps if s.kind != "session" and not s.key.endswith(HIDDEN)]
    keys = [s.key for s in pipeline_steps]
    rows: dict[str, dict[str, Any]] = {}
    for s in pipeline_steps:
        parent = _parent(s.key, keys)
        rows[s.key] = {"key": s.key, "kind": s.kind, "status": s.status, "attempt": s.attempt, "parent": parent,
                       "depth": 0, "error": s.error, "started_at": s.created_at, "updated_at": s.updated_at,
                       "sessions": [], "own_cost_usd": 0.0, "cost_usd": 0.0}
    for row in rows.values():
        depth, p = 0, row["parent"]
        while p is not None:
            depth, p = depth + 1, rows[p]["parent"]
        row["depth"] = depth

    sessions, live, follows, restarts, reuses = [], [], [], [], 0
    for s in steps:
        if s.kind != "session":
            continue
        r = s.result or {}
        session_id = s.session_id or r.get("session_id")
        ev = started.get(session_id)
        payload = ev.payload if ev is not None else {}
        session = {"key": s.key, "status": s.status, "attempt": s.attempt, "session_id": session_id,
                   "stage": s.key.split("/")[1] if s.key.count("/") >= 1 else "", "pipeline_step": _scope(s.key),
                   "agent_key": payload.get("agent_key"), "title": payload.get("title") or s.key.rsplit("/", 1)[-1],
                   "cost_usd": r.get("list_cost_usd"), "stop_reason": r.get("stop_reason"), "error": s.error,
                   "started_at": ev.at if ev is not None else s.created_at, "updated_at": s.updated_at}
        sessions.append(session)
        if s.status == "done":
            reuses += 1
        elif s.status == "running" and s.session_id:
            ended = finished_sessions.get(s.session_id)
            if ended in STOPPED:
                restarts.append(session["key"])          # a stop interrupted it: its work starts again in a new session
            else:
                follows.append(session["session_id"])
                live.append({**session, "following": controller_alive, "finished_unrecorded": ended is not None})
        # attach to the innermost pipeline step the session ran in (its scope, or the nearest step above it)
        owner = session["pipeline_step"] if session["pipeline_step"] in rows else _parent(session["pipeline_step"], keys)
        if owner is not None:
            rows[owner]["sessions"].append(session["key"])
            rows[owner]["own_cost_usd"] += float(session["cost_usd"] or 0.0)
    for row in rows.values():                          # roll each step's own cost up through the steps above it
        cost, p = row["own_cost_usd"], row["key"]
        while p is not None:
            rows[p]["cost_usd"] += cost
            p = rows[p]["parent"]
    for row in rows.values():
        row["own_cost_usd"], row["cost_usd"] = round(row["own_cost_usd"], 2), round(row["cost_usd"], 2)
    unfinished = [r for r in rows.values() if r["status"] != "done"]
    # the deepest unfinished steps: where a replay picks up (their parents are unfinished because they are)
    continues_at = [r["key"] for r in unfinished if not any(o["parent"] == r["key"] for o in unfinished)]
    total = round(sum(float(s["cost_usd"] or 0.0) for s in sessions), 2)
    by_stage: dict[str, float] = {}
    for s in sessions:
        by_stage[s["stage"]] = round(by_stage.get(s["stage"], 0.0) + float(s["cost_usd"] or 0.0), 2)
    return {"pipeline": list(rows.values()), "sessions": sessions, "live": live,
            "resume": {"continues_at": continues_at, "follows": follows, "restarts": restarts, "reuses": reuses},
            "cost": {"total_usd": total, "by_stage": by_stage}, "controller_alive": controller_alive,
            "recorded": bool(steps)}


def render(view: dict[str, Any]) -> list[str]:
    """`factory steps`: the projection as text."""
    if not view["recorded"]:
        return ["This run has no steps (a controller before ADR-0013 wrote it)."]
    lines = []
    for row in view["pipeline"]:
        cost = f"${row['cost_usd']:.2f}" if row["cost_usd"] else ""
        sessions = f"{len(row['sessions'])} session{'s' if len(row['sessions']) != 1 else ''}" if row["sessions"] else ""
        attempt = f"attempt {row['attempt']}" if row["attempt"] > 1 else ""
        lines.append(f"{'  ' * row['depth']}{row['key']:<{max(8, 44 - 2 * row['depth'])}} {row['status']:<8} "
                     + "  ".join(x for x in (sessions, cost, attempt) if x))
    resume = view["resume"]
    lines.append("")
    if view["live"]:
        who = "followed by the controller" if view["controller_alive"] else "working on their own; a resume follows them"
        lines.append(f"Live sessions ({who}):")
        lines += [f"  {s['session_id']}  {s['agent_key'] or ''}  {s['title']}" for s in view["live"]]
    if resume["continues_at"]:
        lines.append(f"A resume continues at: {', '.join(resume['continues_at'])}")
    if resume["restarts"]:
        lines.append(f"It starts again (a stop interrupted them): {', '.join(resume['restarts'])}")
    lines.append(f"Finished sessions a replay reuses: {resume['reuses']}. Sessions cost ${view['cost']['total_usd']:.2f} "
                 f"({', '.join(f'{k} ${v:.2f}' for k, v in sorted(view['cost']['by_stage'].items()))}).")
    return lines


__all__ = ["project", "render"]
