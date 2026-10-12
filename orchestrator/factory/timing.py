"""Where a run's wall-clock time went, derived from its event log (`factory timing <run_id>`).

Every instant between the run's first and last event is put in exactly one bucket, so the buckets add up to the wall
clock:

- `waiting on a person`: a gate or a blocking question is open. A run waiting on a person makes no progress whatever
  else is going on, so this wins over everything below.
- `agents`: at least one session is running. Overlapping sessions share the slice, so `by_stage` adds up to this bucket
  and says which stage the run was actually waiting on (parallel reviewer passes cost their wall time once, not twice).
- `stopped`: the controller was down, from `run.failed` to the next `run.resumed`.
- `controller`: everything else: uploads and session creation, local verification, git, polling.

Built from the first live runs' post-mortem, which had to reconstruct this by hand (docs/postmortems/2026-10-10).
"""
from __future__ import annotations

from collections import defaultdict
from itertools import pairwise
from dataclasses import dataclass
from datetime import datetime
from typing import Any

WAITING, AGENTS, STOPPED, CONTROLLER = "waiting on a person", "agents", "stopped", "controller"
BUCKETS = (AGENTS, WAITING, CONTROLLER, STOPPED)


@dataclass(frozen=True)
class Span:
    start: float
    end: float
    label: str
    detail: dict[str, Any]

    @property
    def seconds(self) -> float:
        return max(0.0, self.end - self.start)


def _t(at: str) -> float:
    return datetime.fromisoformat(at).timestamp()


def _kind(ev: Any) -> str:
    return ev.kind if hasattr(ev, "kind") else ev["kind"]


def _get(ev: Any, name: str) -> Any:
    return getattr(ev, name) if hasattr(ev, name) else ev.get(name)


def spans(events: list[Any], now: float | None = None) -> dict[str, list[Span]]:
    """Sessions, person waits and controller stops as time spans. Unclosed spans end at the controller's stop (sessions:
    the stop interrupted them) or at `now` / the last event."""
    if not events:
        return {"sessions": [], "waits": [], "stops": []}
    end = now if now is not None else _t(_get(events[-1], "at"))
    sessions: dict[str, dict[str, Any]] = {}
    waits: dict[str, dict[str, Any]] = {}
    stops: list[Span] = []
    stopped_at: float | None = None
    out_sessions: list[Span] = []
    out_waits: list[Span] = []
    for ev in events:
        k, p, at = _kind(ev), _get(ev, "payload") or {}, _t(_get(ev, "at"))
        if k == "session.started" and p.get("session_id"):
            sessions[p["session_id"]] = {"start": at, "stage": _get(ev, "stage") or p.get("stage") or "?",
                                         "agent_key": p.get("agent_key"), "title": p.get("title"),
                                         "work_item_id": _get(ev, "work_item_id")}
        elif k == "session.finished" and p.get("session_id") in sessions:
            s = sessions.pop(p["session_id"])
            out_sessions.append(Span(s["start"], at, s["stage"], {**s, "session_id": p["session_id"],
                                                                  "list_cost_usd": p.get("list_cost_usd"),
                                                                  "stop_reason": p.get("stop_reason")}))
        elif k == "gate.opened" and p.get("gate_id"):
            waits[p["gate_id"]] = {"start": at, "kind": p.get("kind") or _get(ev, "stage") or "gate"}
        elif k == "gate.decided" and p.get("gate_id") in waits:
            w = waits.pop(p["gate_id"])
            out_waits.append(Span(w["start"], at, w["kind"], {"gate_id": p["gate_id"], "decision": p.get("decision"),
                                                              "decided_by": p.get("decided_by")}))
        elif k == "question.asked" and p.get("blocking") and p.get("question_id"):
            waits[p["question_id"]] = {"start": at, "kind": "question"}
        elif k == "question.answered" and p.get("question_id") in waits:
            w = waits.pop(p["question_id"])
            out_waits.append(Span(w["start"], at, w["kind"], {"question_id": p["question_id"],
                                                              "answered_by": p.get("answered_by")}))
        elif k == "run.failed":
            stopped_at = at if stopped_at is None else stopped_at
        elif k == "run.resumed" and stopped_at is not None:
            stops.append(Span(stopped_at, at, "stopped", {}))
            stopped_at = None
    if stopped_at is not None and end > stopped_at:
        stops.append(Span(stopped_at, end, "stopped", {}))
    # A session whose finish was recorded keeps its real end, even one recorded while the controller was down. One
    # that never finished ends at the first stop after it started (the stop interrupted it), else it is still running.
    for sid, s in sessions.items():
        stop = next((st.start for st in stops if st.start >= s["start"]), None)
        out_sessions.append(Span(s["start"], stop if stop is not None else end, s["stage"],
                                 {**s, "session_id": sid, "stop_reason": "controller stopped" if stop is not None else "running"}))
    # A gate left open when the controller stopped is still waiting on its owner (a resume picks the same gate up).
    out_waits += [Span(w["start"], end, w["kind"], {"open": True}) for w in waits.values()]
    return {"sessions": sorted(out_sessions, key=lambda s: s.start), "waits": sorted(out_waits, key=lambda s: s.start),
            "stops": stops}


def report(events: list[Any], now: float | None = None) -> dict[str, Any]:
    """The run's wall clock split into buckets, the agents bucket split by stage, plus the sessions and waits behind it."""
    if not events:
        return {"wall_seconds": 0.0, "buckets": dict.fromkeys(BUCKETS, 0.0), "by_stage": {}, "sessions": [], "waits": [],
                "rounds": {}}
    t0 = _t(_get(events[0], "at"))
    t1 = now if now is not None else _t(_get(events[-1], "at"))
    sp = spans(events, now)
    cuts = {t0, t1}
    for group in sp.values():
        for s in group:
            cuts.update((min(max(s.start, t0), t1), min(max(s.end, t0), t1)))
    points = sorted(cuts)
    buckets: dict[str, float] = dict.fromkeys(BUCKETS, 0.0)
    by_stage: dict[str, float] = defaultdict(float)
    waiting_on: dict[str, float] = defaultdict(float)
    for a, b in pairwise(points):
        if b <= a:
            continue
        mid, width = (a + b) / 2, b - a
        waits = [w for w in sp["waits"] if w.start <= mid < w.end]
        live = [s for s in sp["sessions"] if s.start <= mid < s.end]
        if waits:
            buckets[WAITING] += width
            for w in waits:
                waiting_on[w.label] += width / len(waits)
        elif live:
            buckets[AGENTS] += width
            for s in live:
                by_stage[s.label] += width / len(live)
        elif any(s.start <= mid < s.end for s in sp["stops"]):
            buckets[STOPPED] += width
        else:
            buckets[CONTROLLER] += width
    rounds: dict[str, int] = defaultdict(int)
    for s in sp["sessions"]:
        rounds[s.label] += 1
    return {
        "wall_seconds": t1 - t0, "buckets": buckets, "by_stage": dict(by_stage), "waiting_on": dict(waiting_on),
        "sessions": [{"stage": s.label, "agent_key": s.detail.get("agent_key"), "title": s.detail.get("title"),
                      "session_id": s.detail.get("session_id"), "start_offset_seconds": s.start - t0,
                      "seconds": s.seconds, "list_cost_usd": s.detail.get("list_cost_usd"),
                      "stop_reason": s.detail.get("stop_reason")} for s in sp["sessions"]],
        "waits": [{"kind": w.label, "start_offset_seconds": w.start - t0, "seconds": w.seconds, **w.detail} for w in sp["waits"]],
        "rounds": dict(rounds),
    }


def _m(seconds: float) -> str:
    return f"{seconds / 60:6.1f}m"


def render(rep: dict[str, Any]) -> list[str]:
    """`factory timing` as lines: the buckets, the stages the run waited on, then every session and wait in order."""
    wall = rep["wall_seconds"] or 1.0
    lines = [f"wall clock {_m(rep['wall_seconds']).strip()}"]
    for name in BUCKETS:
        secs = rep["buckets"].get(name, 0.0)
        lines.append(f"  {name:22s} {_m(secs)}  {100 * secs / wall:5.1f}%")
        if name == AGENTS:
            for stage, s in sorted(rep["by_stage"].items(), key=lambda kv: -kv[1]):
                lines.append(f"    {stage:20s} {_m(s)}  {100 * s / wall:5.1f}%  ({rep['rounds'].get(stage, 0)} sessions)")
        if name == WAITING:
            for kind, s in sorted(rep.get("waiting_on", {}).items(), key=lambda kv: -kv[1]):
                lines.append(f"    {kind:20s} {_m(s)}")
    lines.append("timeline")
    rows = [(s["start_offset_seconds"], (f"  +{_m(s['start_offset_seconds']).strip():>7s} session {s['stage']:13s} {_m(s['seconds'])}"
             f"  ${s['list_cost_usd'] if s['list_cost_usd'] is not None else '?'}  {s['stop_reason'] or ''}")) for s in rep["sessions"]]
    rows += [(w["start_offset_seconds"], (f"  +{_m(w['start_offset_seconds']).strip():>7s} wait    {w['kind']:13s} {_m(w['seconds'])}"
              f"  {w.get('decision') or w.get('answered_by') or ('open' if w.get('open') else '')}")) for w in rep["waits"]]
    lines += [text for _, text in sorted(rows, key=lambda r: r[0])]
    return lines
