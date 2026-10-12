"""A run parked at Gate 2 with its controller stopped: the real event log, store artefacts and file-queue gate Gate 2 leaves, then the run.failed the stop handler records. Recovery tests drive the real runner against it with a driver that refuses any agent."""
import json

from conftest import make_config

from factory.gates import Decision, GateKind, LogNotifier
from factory.stages import RunContext
from factory.stages.common import gate_decision, open_gate, public
from factory.state import Policy, RunStage, WorkItemStage
from factory.store import RunStore
from factory.surfaces.filequeue import FileQueue


RUN = "run_gate2recov"

SHA = "a" * 64

GATE2_OPTIONS = (Decision.APPROVE, Decision.APPROVE_SUBSET, Decision.REQUEST_CHANGES, Decision.REJECT)

OUTCOME = {"outcome_id": "o", "title": "Model update", "statement": "Add the new model", "owner": {"handle": "will"},
           "target_repositories": [{"url": "https://github.com/x/evergreen", "default_branch": "main"}]}

DECOMPOSITION = {"revision": 1, "work_items": [{"work_item_id": "wi-a", "title": "catalog", "depends_on": []},
                                               {"work_item_id": "wi-b", "title": "picker", "depends_on": []}]}

def spec(sid, wid):
    return {"spec_id": sid, "work_item_id": wid, "title": sid, "acceptance_criteria": [], "allowed_paths": ["src/**"],
            "test_plan": {"commands": ["bun test src"], "e2e": []}, "dependencies": {"specs": []}, "_markdown": f"# {sid}"}

SPECS = {"spec_a": spec("spec_a", "wi-a"), "spec_b": spec("spec_b", "wi-b")}

def verdict(result="pass"):
    return {"round": 2, "overall": {"result": result, "summary_for_human": "Specs are ready", "blocking_issue_count": 0 if result == "pass" else 2}}

class RefuseAgents:
    """No agent may run before the owner decides; recovery never needs one to wait."""
    def __init__(self):
        self.calls = []

    def run(self, **kw):
        self.calls.append(kw)
        raise AssertionError(f"an agent was started: {kw.get('agent_key')}")

    def interrupt_active(self):
        return []

def context(tmp_path, root, design_only=True, **cfg):
    runs = tmp_path / "runs"
    return RunContext(cfg=make_config(root, runs, **cfg), store=RunStore(runs), driver=RefuseAgents(), queue=FileQueue(runs / "_queue"),
                      notifier=LogNotifier(), forge=None, policy=Policy(design_only=design_only), run_id=RUN)

def park(tmp_path, root, result="pass", design_only=True, stop=True):
    """A run waiting at Gate 2 on the specs of round 2, its controller stopped by a signal. Returns (gate, presented)."""
    ctx = context(tmp_path, root, design_only)
    ctx.event("run.created", {"outcome_id": "o", "title": "t", "policy": {"preview_mode": "local"}, "design_only": design_only,
                              "budget_usd": 50}, stage="intake")
    ctx.store.put_artifact(RUN, "intake", "outcome.json", json.dumps(OUTCOME).encode())
    ctx.event("stage.entered", {"stage": "decomposing"}, stage="decompose")
    ctx.store.put_artifact(RUN, "decompose", "r1-decomposition.json", json.dumps(DECOMPOSITION).encode())
    ctx.event("artifact.produced", {"artifact_type": "decomposition", "revision": 1, "work_items": DECOMPOSITION["work_items"]}, stage="decompose")
    ctx.event("stage.entered", {"stage": "gate_1_decomposition"}, stage="gate_1")
    g1 = open_gate(ctx, GateKind.DECOMPOSITION_REVIEW, {"decomposition.json": SHA}, "plan", ["will"],
                   (Decision.APPROVE, Decision.REQUEST_CHANGES, Decision.REJECT))
    ctx.queue.record_decision(g1.gate_id, "approve", "will")
    assert gate_decision(ctx, g1.gate_id, GateKind.DECOMPOSITION_REVIEW).decision == Decision.APPROVE
    ctx.event("stage.entered", {"stage": "specifying"}, stage="specify")
    for wi in ("wi-a", "wi-b"):
        ctx.event("work_item.stage", {"stage": WorkItemStage.SPEC_DRAFTING.value}, work_item_id=wi)
        ctx.event("work_item.stage", {"stage": WorkItemStage.SPEC_JUDGING.value}, work_item_id=wi)
        for n in (1, 2):
            ctx.store.put_artifact(RUN, "specify", f"{wi}-r{n}-spec.json", json.dumps({"round": n}).encode())
    ctx.store.put_artifact(RUN, "judge", "r2-verdict.json", json.dumps(verdict(result)).encode())
    shas = {f"{sid}.json": ctx.store.put_artifact(RUN, "specify", f"final-{sid}.json", json.dumps(public(s), indent=2).encode(), replace=True)
            for sid, s in SPECS.items()}
    for sid, s in SPECS.items():
        ctx.store.put_artifact(RUN, "specify", f"final-{sid}.md", s["_markdown"].encode(), replace=True)
    shas["verdict.json"] = ctx.store.put_artifact(RUN, "judge", "final-verdict.json", json.dumps(verdict(result), indent=2).encode(), replace=True)
    ctx.event("stage.entered", {"stage": RunStage.GATE_2_SPEC_READINESS.value}, stage="gate_2")
    kind = GateKind.SPEC_READINESS if result == "pass" else GateKind.ESCALATION
    gate = open_gate(ctx, kind, shas, "Specs are ready", ["will"], GATE2_OPTIONS)
    if stop:
        ctx.event("run.failed", {"reason": "stopped by signal 15", "interrupted_sessions": []})
    return gate, shas
