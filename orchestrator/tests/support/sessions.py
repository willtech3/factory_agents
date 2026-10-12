"""Fakes at the Managed Agents boundary that the stage tests share: what a finished session returns, a gate surface whose
owner answers at once, a run parked just after Gate 1, a spec-stage driver, and the event-stream pieces SessionDriver
reads. Everything behind them (stages, store, state machine, checks) is the real code."""
import json
from types import SimpleNamespace

from conftest import make_config

from factory.gates import GateDecision, InMemoryQueue, LogNotifier
from factory.sessions import StageResult
from factory.stages import RunContext
from factory.state import Policy
from factory.store import RunStore


def stage_result(outputs: dict[str, bytes], *, sid: str = "s", agent_id: str = "a", cost: float | None = 0.5,
                 stop_reason: str | None = "end_turn", outcome_result: str | None = "satisfied", iterations: int = 1,
                 last_message: str | None = None) -> StageResult:
    """What `SessionDriver.run` returns for a session that finished with `outputs` in /mnt/session/outputs/."""
    return StageResult(session_id=sid, agent_id=agent_id, agent_version=1, outputs=outputs, outcome_result=outcome_result,
                       outcome_iterations=iterations, list_cost_usd=cost, stop_reason=stop_reason, events_seen=1,
                       last_message=last_message)


class AutoQueue(InMemoryQueue):
    """The owner answers every gate with `decision` the moment it opens."""

    def __init__(self, decision):
        super().__init__()
        self.auto = decision

    def open(self, gate):
        super().open(gate)
        self.decide(GateDecision(gate_id=gate.gate_id, decision=self.auto, decided_by="priya.n",
                                 artifact_shas=tuple(a.sha256 for a in gate.artifacts)))


class SpecStageDriver:
    """Spec writers finish normally; the judge's first `judge_truncations` sessions hit their cap (the cut-off verdict
    says `escalate`), the next one passes. `writer_iterations_exhausted` makes every writer stop on its grader's cap."""

    def __init__(self, examples, judge_truncations=1, writer_iterations_exhausted=False):
        self.examples, self.calls = examples, []
        self.judge_truncations, self.writer_iterations_exhausted = judge_truncations, writer_iterations_exhausted

    def run(self, **kw):
        self.calls.append(kw)
        if kw["agent_key"] == "spec_writer":
            wid = kw["work_item_id"]
            spec = dict(self.examples["spec_smart_retries"], spec_id=f"spec_{wid.removeprefix('wi-')}", work_item_id=wid)
            out = {"spec.json": json.dumps(spec).encode(), "spec.md": f"# {wid}".encode()}
            if self.writer_iterations_exhausted:
                return stage_result(out, outcome_result="max_iterations_reached", iterations=4, cost=1.2, sid=f"sesn_w_{wid}")
            return stage_result(out, sid=f"sesn_w_{wid}")
        n = sum(1 for c in self.calls if c["agent_key"] == "spec_judge")
        if n <= self.judge_truncations:
            cut = {**self.examples["verdict_round2"], "overall": {**self.examples["verdict_round2"]["overall"], "result": "escalate"}}
            return stage_result({"verdict.json": json.dumps(cut).encode()}, stop_reason="budget_reached", outcome_result=None,
                                cost=kw["budget_usd"], sid=f"sesn_j_cut{n}")
        return stage_result({"verdict.json": json.dumps(self.examples["verdict_round2"]).encode()}, outcome_result=None, cost=2.0,
                            sid="sesn_j_ok")


def run_after_gate_1(tmp_path, root, examples, driver, queue, run_id="run_budget000001") -> RunContext:
    """A run whose Ledgerline decomposition the owner approved at Gate 1, ready to specify."""
    ctx = RunContext(cfg=make_config(root, tmp_path), store=RunStore(tmp_path), driver=driver, queue=queue,
                     notifier=LogNotifier(), forge=None, policy=Policy(spec_judge_max_rounds=2), run_id=run_id)
    ctx.store.put_artifact(ctx.run_id, "intake", "outcome.json", json.dumps(examples["outcome"]).encode())
    ctx.event("run.created", {"policy": {"preview_mode": "local"}, "budget_usd": 50, "outcome_id": "o", "title": "t"})
    ctx.event("stage.entered", {"stage": "decomposing"})
    ctx.event("artifact.produced", {"artifact_type": "decomposition", "revision": 1, "work_items": examples["decomposition"]["work_items"]})
    ctx.event("stage.entered", {"stage": "gate_1_decomposition"})
    return ctx


# ---- the event stream SessionDriver drains

class Page:
    """One page of a paginated listing (events or files), the last one."""

    def __init__(self, data):
        self.data = data
        self.has_more = False


class FakeStream:
    """Yields the events given, then raises if told to (the peer closing the connection mid-stream)."""

    def __init__(self, events, then_raise=None):
        self.events, self.then_raise = events, then_raise

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        yield from self.events
        if self.then_raise:
            raise self.then_raise


def ev(id_, type_, **kw):
    """A platform event with an id and a type."""
    return SimpleNamespace(id=id_, type=type_, **kw)
