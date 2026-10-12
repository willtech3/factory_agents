"""`factory timing`: the wall clock of a run split into buckets that add up, checked on the first live run's real log."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory import timing
from factory.state import RunEvent

REFERENCE = Path(__file__).resolve().parents[2] / "evals/datasets/factory-bench/sample-app/reference-run/events.jsonl"


def _events(path: Path) -> list[RunEvent]:
    return [RunEvent(**json.loads(line)) for line in path.read_text().splitlines() if line.strip()]


def _ev(seq: int, at_s: int, kind: str, payload: dict | None = None, stage: str | None = None) -> RunEvent:
    return RunEvent(run_id="run_t", seq=seq, kind=kind, actor={"type": "orchestrator", "id": "orchestrator"},
                    payload=payload or {}, stage=stage, at=f"2026-10-10T10:{at_s // 60:02d}:{at_s % 60:02d}+00:00")


def test_reference_run_buckets_add_up_and_match_the_post_mortem():
    rep = timing.report(_events(REFERENCE))
    wall = rep["wall_seconds"]
    assert wall == pytest.approx(231.2 * 60, abs=30)
    assert sum(rep["buckets"].values()) == pytest.approx(wall, abs=1e-6)
    assert sum(rep["by_stage"].values()) == pytest.approx(rep["buckets"][timing.AGENTS], abs=1e-6)
    # The three gate decisions took 57 minutes; the spec judge was the stage the run waited on longest.
    assert rep["buckets"][timing.WAITING] / 60 == pytest.approx(56.9, abs=0.5)
    assert max(rep["by_stage"], key=rep["by_stage"].get) == "judge"
    assert rep["by_stage"]["judge"] / 60 == pytest.approx(51.6, abs=0.5)
    assert rep["rounds"]["review"] == 4 and rep["rounds"]["judge"] == 4 and rep["rounds"]["implement"] == 2
    lines = timing.render(rep)
    assert lines[0].startswith("wall clock") and any("judge" in line for line in lines)


def test_overlapping_sessions_share_their_slice_and_a_stop_is_not_agent_time():
    events = [
        _ev(0, 0, "run.created"),
        _ev(1, 0, "session.started", {"session_id": "s1", "agent_key": "reviewer"}, stage="review"),
        _ev(2, 0, "session.started", {"session_id": "s2", "agent_key": "security_reviewer"}, stage="security"),
        _ev(3, 60, "session.finished", {"session_id": "s1"}, stage="review"),
        _ev(4, 120, "run.failed", {"reason": "crash"}),           # s2 is cut off by the stop
        _ev(5, 300, "run.resumed", {"stage": "implementing"}),
        _ev(6, 300, "gate.opened", {"gate_id": "g1", "kind": "merge_readiness"}),
        _ev(7, 420, "gate.decided", {"gate_id": "g1", "decision": "approve"}),
        _ev(8, 480, "run.completed"),
    ]
    rep = timing.report(events)
    assert rep["wall_seconds"] == 480
    assert rep["buckets"] == {timing.AGENTS: 120, timing.WAITING: 120, timing.CONTROLLER: 60, timing.STOPPED: 180}
    assert rep["by_stage"] == {"review": 30, "security": 90}
    assert [s["stop_reason"] for s in rep["sessions"]] == [None, "controller stopped"]


def test_verify_model_setting():
    from factory.config import DEFAULT_VERIFY_MODEL, parse_verify_model
    assert parse_verify_model(None) == DEFAULT_VERIFY_MODEL == {"id": "claude-haiku-5-5", "effort": "low"}
    assert parse_verify_model("agent") is None
    assert parse_verify_model("claude-sonnet-5-5:medium") == {"id": "claude-sonnet-5-5", "effort": "medium"}
    assert parse_verify_model(" claude-sonnet-5-5 ") == {"id": "claude-sonnet-5-5"}


def test_open_gate_counts_to_now_and_empty_log_is_zero():
    events = [_ev(0, 0, "run.created"), _ev(1, 30, "gate.opened", {"gate_id": "g", "kind": "decomposition_review"})]
    rep = timing.report(events, now=timing._t(events[1].at) + 90)
    assert rep["buckets"][timing.WAITING] == 90 and rep["waits"][0]["open"] is True
    assert timing.report([])["wall_seconds"] == 0.0
