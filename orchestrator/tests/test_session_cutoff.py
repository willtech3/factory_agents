"""Post-mortem issue 17: a session that stops on its dollar cap or its grader's iteration cap is not a result. The
orchestrator records it, opens a budget gate, and only re-runs (cap raised) or accepts on the owner's say-so. No network."""

import pytest
from support.sessions import AutoQueue, SpecStageDriver, run_after_gate_1

from factory.gates import Decision, GateKind, InMemoryQueue
from factory.stages import specify
from factory.stages.common import BUDGET_RAISE, SessionTruncated
from factory.state import Policy, RunStage


class SessionGateQueue(AutoQueue):
    """The owner approves a session's cap gate at once; a run-cap gate (it presents paused-N.json) waits for a human."""

    def open(self, gate):
        if gate.artifacts:
            return InMemoryQueue.open(self, gate)
        super().open(gate)


def test_a_cap_raise_the_run_budget_cannot_cover_pauses_the_run_before_the_re_run(tmp_path, root, examples):
    """The owner raised the judge's cap, but spent plus the raised cap passes `constraints.budget_usd`: the run pauses
    at its budget (budget.py) before the re-run starts, with the raised cap as the step that did not fit. Writers 4 x
    $0.50 plus the cut-off judge's $6.00 are spent; the re-run would commit $9.00 against a $15.00 run budget."""
    from factory.budget import BudgetPaused

    ctx = run_after_gate_1(tmp_path, root, examples, SpecStageDriver(examples), SessionGateQueue(Decision.APPROVE))
    ctx.policy = Policy(spec_judge_max_rounds=2, run_budget_usd=15.0)
    with pytest.raises(BudgetPaused) as info:
        specify.run_specify(ctx, examples["outcome"], examples["decomposition"])
    e = info.value
    assert (e.stage, e.sessions, e.cap_usd, e.spent_usd, e.budget_usd) == ("judge", 1, 9.0, 8.0, 15.0)
    assert [c["agent_key"] for c in ctx.driver.calls].count("spec_judge") == 1          # the re-run never started
    events = ctx.store.events(ctx.run_id)
    kinds = [ev.kind for ev in events]
    pauses = [ev.payload for ev in events if ev.kind == "budget.reached"]
    assert len(pauses) == 2 and pauses[0]["session_id"] == "sesn_j_cut1" and pauses[1]["next"]["cap_usd"] == 9.0
    i = kinds.index("budget.reached")
    assert kinds[i + 1:i + 4] == ["gate.opened", "gate.decided", "stage.entered"]        # the session's gate, approved, back to specifying
    assert kinds[-2:] == ["budget.reached", "gate.opened"]                               # then the run cap: paused, its gate open
    assert ctx.store.state(ctx.run_id).stage == RunStage.PAUSED_BUDGET


def test_budget_reached_pauses_the_run_and_the_owner_raises_the_cap(tmp_path, root, examples):
    ctx = run_after_gate_1(tmp_path, root, examples, SpecStageDriver(examples), AutoQueue(Decision.APPROVE))
    specs, verdict = specify.run_specify(ctx, examples["outcome"], examples["decomposition"])
    judges = [c for c in ctx.driver.calls if c["agent_key"] == "spec_judge"]
    assert len(judges) == 2 and judges[1]["budget_usd"] == pytest.approx(judges[0]["budget_usd"] * BUDGET_RAISE)
    assert verdict["overall"]["result"] == "pass"                                                            # the cut-off verdict (escalate) was not used
    events = ctx.store.events(ctx.run_id)
    kinds = [e.kind for e in events]
    i = kinds.index("budget.reached")
    assert kinds[i + 1:i + 4] == ["gate.opened", "gate.decided", "stage.entered"]                             # paused, asked, resumed
    assert events[i].payload["session_id"] == "sesn_j_cut1" and events[i].payload["budget_usd"] == judges[0]["budget_usd"]
    gate = [e for e in events if e.kind == "gate.opened"][-1].payload
    assert gate["kind"] == GateKind.BUDGET.value and gate["assignees"] == [examples["outcome"]["owner"]["handle"]]
    assert "raised to" in gate["summary"] and f"judge/r1-truncated1-" in gate["summary"]
    assert events[i + 3].payload["stage"] == RunStage.SPECIFYING.value
    st = ctx.store.state(ctx.run_id)
    assert st.stage == RunStage.SPECIFYING and st.list_cost_usd == pytest.approx(0.5 * 4 + judges[0]["budget_usd"] + 2.0)  # the cut-off session's spend counts
    assert kinds.count("judge.verdict") == 1                                                                 # one verdict, not two
    kept = tmp_path / ctx.run_id / "artifacts" / "judge"
    assert (kept / "r1-truncated1-verdict.json").is_file() and (kept / "r1-verdict.json").is_file()


def test_owner_may_close_the_run_at_the_budget_gate(tmp_path, root, examples):
    ctx = run_after_gate_1(tmp_path, root, examples, SpecStageDriver(examples), AutoQueue(Decision.REJECT))
    with pytest.raises(SessionTruncated):
        specify.run_specify(ctx, examples["outcome"], examples["decomposition"])
    assert ctx.store.state(ctx.run_id).stage == RunStage.CLOSED
    assert len([c for c in ctx.driver.calls if c["agent_key"] == "spec_judge"]) == 1


def test_grader_iteration_cap_opens_the_gate_and_approve_accepts_the_last_output(tmp_path, root, examples):
    ctx = run_after_gate_1(tmp_path, root, examples, SpecStageDriver(examples, judge_truncations=0, writer_iterations_exhausted=True), AutoQueue(Decision.APPROVE))
    specs, verdict = specify.run_specify(ctx, examples["outcome"], examples["decomposition"])
    assert len(specs) == 4 and len([c for c in ctx.driver.calls if c["agent_key"] == "spec_writer"]) == 4     # accepted, not re-run
    events = ctx.store.events(ctx.run_id)
    cut = [e for e in events if e.kind == "session.truncated"]
    assert len(cut) == 4 and all(e.payload["reason"] == "max_iterations_reached" and e.payload["outcome_iterations"] == 4 for e in cut)
    gates = [e.payload for e in events if e.kind == "gate.opened"]
    assert len(gates) == 4 and all(g["kind"] == GateKind.BUDGET.value and "grader iterations" in g["summary"] for g in gates)
    assert ctx.store.state(ctx.run_id).stage == RunStage.SPECIFYING                                          # informational: no pause


def test_grader_iteration_cap_reject_escalates_once_even_with_parallel_writers(tmp_path, root, examples):
    ctx = run_after_gate_1(tmp_path, root, examples, SpecStageDriver(examples, judge_truncations=0, writer_iterations_exhausted=True), AutoQueue(Decision.REJECT))
    with pytest.raises(SessionTruncated):
        specify.run_specify(ctx, examples["outcome"], examples["decomposition"])
    assert ctx.store.state(ctx.run_id).stage == RunStage.ESCALATED                      # the log replays: one escalation
    events = ctx.store.events(ctx.run_id)
    assert sum(e.kind == "run.escalated" for e in events) == 1 and sum(e.kind == "session.truncated" for e in events) == 4
    assert sum(e.kind == "gate.opened" for e in events) <= 4                           # sessions cut short after the close ask nobody


def test_budget_scale_never_drops_a_cap_below_the_observed_median(monkeypatch):
    from factory.cli import _policy

    base = Policy()
    monkeypatch.setenv("FACTORY_BUDGET_SCALE", "0.1")
    scaled = _policy({}, False).stage_budget_usd
    assert set(scaled) == set(base.stage_budget_usd)
    for stage, cap in base.stage_budget_usd.items():                  # scaled, but never below the stage's observed median
        assert scaled[stage] == max(round(cap * 0.1, 2), base.stage_budget_floor_usd[stage]), stage
    assert any(scaled[s] == base.stage_budget_floor_usd[s] > round(c * 0.1, 2) for s, c in base.stage_budget_usd.items())
    monkeypatch.setenv("FACTORY_BUDGET_SCALE", "2")
    assert _policy({}, False).stage_budget_usd == {s: c * 2 for s, c in base.stage_budget_usd.items()}


def test_start_screen_budget_and_resumed_cap_keep_their_precedence(monkeypatch):
    from factory.cli import _policy

    outcome = {"constraints": {"budget_usd": 60}}
    monkeypatch.setenv("FACTORY_RUN_BUDGET_USD", "45")
    assert _policy(outcome, False).run_budget_usd == 45
    assert _policy(outcome, False, budget_usd=90).run_budget_usd == 90
    monkeypatch.delenv("FACTORY_RUN_BUDGET_USD")
    assert _policy(outcome, False).run_budget_usd == 60
