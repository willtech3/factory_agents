"""Spec writers run all at once by default; dependency-order waves only when the policy asks. No network."""
import json
import threading
from types import SimpleNamespace

from conftest import make_config

from factory.gates import InMemoryQueue, LogNotifier
from factory.stages import RunContext
from factory.stages import specify
from factory.state import Policy
from factory.store import RunStore


class Driver:
    """Records how many writer sessions overlap in time; the judge passes the set immediately."""

    def __init__(self, examples):
        self.examples = examples
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()
        self.gate = threading.Barrier(4, timeout=5)   # four writers must be in flight together to pass

    def run(self, **kw):
        if kw["agent_key"] == "spec_writer":
            with self.lock:
                self.active += 1; self.peak = max(self.peak, self.active)
            try:
                self.gate.wait()
            except threading.BrokenBarrierError:
                pass
            with self.lock:
                self.active -= 1
            wid = kw["work_item_id"]
            spec = dict(self.examples["spec_smart_retries"], spec_id=f"spec_{wid.removeprefix('wi-')}", work_item_id=wid)
            return SimpleNamespace(session_id="s", agent_id="w", agent_version=1, outcome_result="satisfied", outcome_iterations=1,
                                   list_cost_usd=0.1, stop_reason="end_turn", events_seen=1, custom_tool_calls=[],
                                   outputs={"spec.json": json.dumps(spec).encode(), "spec.md": b"# s"})
        verdict = dict(self.examples["verdict_round2"])
        return SimpleNamespace(session_id="j", agent_id="j", agent_version=1, outcome_result=None, outcome_iterations=0,
                               list_cost_usd=0.1, stop_reason="end_turn", events_seen=1, custom_tool_calls=[],
                               outputs={"verdict.json": json.dumps(verdict).encode()})


def _ctx(tmp_path, root, examples, policy):
    ctx = RunContext(cfg=make_config(root, tmp_path), store=RunStore(tmp_path), driver=Driver(examples), queue=InMemoryQueue(),
                     notifier=LogNotifier(), forge=None, policy=policy, run_id="run_parallel0001")
    ctx.event("run.created", {"outcome_id": "o", "title": "t"})
    ctx.event("stage.entered", {"stage": "decomposing"})
    ctx.event("artifact.produced", {"artifact_type": "decomposition", "revision": 1, "work_items": examples["decomposition"]["work_items"]})
    ctx.event("stage.entered", {"stage": "gate_1_decomposition"})
    return ctx


def test_all_specs_are_written_at_once_by_default(tmp_path, root, examples):
    ctx = _ctx(tmp_path, root, examples, Policy())
    specs, verdict = specify.run_specify(ctx, examples["outcome"], examples["decomposition"])
    assert len(specs) == 4 and ctx.driver.peak == 4            # the chain of dependencies did not serialise the writers
    assert verdict["overall"]["result"] == "pass"


def test_waves_policy_serialises_dependent_items(tmp_path, root, examples):
    ctx = _ctx(tmp_path, root, examples, Policy(spec_waves=True))
    ctx.driver.gate = threading.Barrier(1)                       # no overlap required
    specs, _ = specify.run_specify(ctx, examples["outcome"], examples["decomposition"])
    assert len(specs) == 4 and ctx.driver.peak < 4
