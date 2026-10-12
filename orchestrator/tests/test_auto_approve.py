"""Opt-in pre-approval of Gates 1 and 2 (FACTORY_AUTO_APPROVE): a listed gate still opens, and is approved by "policy"
only when nothing in it needs a person. No network."""
import copy
import json

import pytest
from conftest import make_config

from factory.gates import Decision, InMemoryQueue, LogNotifier
from factory.stages import RunContext
from factory.stages.common import AUTO_GATES, auto_approval, parse_auto_approve
from factory.stages.decompose import gate_1
from factory.stages.specify import gate_2
from factory.state import Policy, RunStage
from factory.store import RunStore
from factory.surfaces.filequeue import FileQueue


def _ctx(tmp_path, root, auto=(), queue=None):
    ctx = RunContext(cfg=make_config(root, tmp_path), store=RunStore(tmp_path), driver=None, queue=queue or InMemoryQueue(),
                     notifier=LogNotifier(), forge=None, policy=Policy(auto_approve=tuple(auto)), run_id="run_auto000001")
    ctx.event("run.created", {"policy": {"preview_mode": "local"}, "budget_usd": 50, "outcome_id": "o", "title": "t"})
    ctx.event("stage.entered", {"stage": RunStage.DECOMPOSING.value})
    return ctx


def test_parse_auto_approve():
    assert parse_auto_approve(None) == () and parse_auto_approve("") == ()
    assert parse_auto_approve("all") == AUTO_GATES
    assert parse_auto_approve(" gate_2 , gate_1,gate_2") == ("gate_2", "gate_1")
    with pytest.raises(ValueError, match="gate_3"):
        parse_auto_approve("gate_1,gate_3")


def test_auto_approval_needs_the_gate_listed_and_nothing_blocking(tmp_path, root):
    assert auto_approval(_ctx(tmp_path / "a", root), "gate_1", []) is None                   # the default: every gate waits
    ctx = _ctx(tmp_path / "b", root, auto=("gate_1",))
    assert "pre-approved gate_1" in auto_approval(ctx, "gate_1", [])
    assert auto_approval(ctx, "gate_1", ["Which currency?"]) is None
    assert auto_approval(ctx, "gate_2", []) is None


def test_gate_1_approves_itself_as_policy_and_keeps_the_gate_for_audit(tmp_path, root, examples):
    ctx = _ctx(tmp_path, root, auto=("gate_1",))
    decomposition = copy.deepcopy(examples["decomposition"])
    for q in decomposition.get("open_questions", []):
        q["blocking"] = False
    d = gate_1(ctx, examples["outcome"], decomposition, {"decomposition.json": "a" * 64})
    assert d.decision == Decision.APPROVE and d.decided_by == "policy" and "FACTORY_AUTO_APPROVE" in d.comments
    events = ctx.store.events(ctx.run_id)
    opened = next(e for e in events if e.kind == "gate.opened")
    decided = next(e for e in events if e.kind == "gate.decided")
    assert decided.payload["gate_id"] == opened.payload["gate_id"] and decided.payload["artifact_shas"] == ["a" * 64]
    assert decided.actor == {"type": "orchestrator", "id": "policy"}


def test_gate_2_approves_a_passing_set_through_the_file_queue(tmp_path, root, examples):
    queue = FileQueue(tmp_path / "_queue")
    ctx = _ctx(tmp_path, root, auto=("gate_2",), queue=queue)
    ctx.event("stage.entered", {"stage": RunStage.GATE_1_DECOMPOSITION.value})
    ctx.event("stage.entered", {"stage": RunStage.SPECIFYING.value})
    spec = copy.deepcopy(examples["spec_smart_retries"])
    spec["open_questions"] = [{"question": "q", "blocking": False, "default_if_unanswered": "d"}]
    verdict = copy.deepcopy(examples["verdict_round2"])
    verdict["overall"].update(result="pass", blocking_issue_count=0)
    d = gate_2(ctx, examples["outcome"], {spec["spec_id"]: spec}, verdict, {f"{spec['spec_id']}.json": "b" * 64, "verdict.json": "c" * 64})
    assert d.decision == Decision.APPROVE and d.decided_by == "policy"
    recorded = json.loads((tmp_path / "_queue" / "decisions.json").read_text())[d.gate_id]
    assert recorded["decided_by"] == "policy" and sorted(recorded["artifact_shas"]) == ["b" * 64, "c" * 64]


def test_gate_2_with_a_blocking_question_or_a_failed_verdict_is_not_pre_approved(tmp_path, root, examples, monkeypatch):
    import factory.stages.specify as specify_mod
    from factory.gates import GateKind
    ctx = _ctx(tmp_path, root, auto=("gate_2",))
    seen = []
    monkeypatch.setattr(specify_mod, "open_gate_and_wait",
                        lambda ctx, kind, *a, auto_approve=None, **kw: seen.append((kind, auto_approve)))
    spec = copy.deepcopy(examples["spec_smart_retries"])
    passing = copy.deepcopy(examples["verdict_round2"])
    passing["overall"].update(result="pass", blocking_issue_count=0)
    ctx.event("stage.entered", {"stage": RunStage.GATE_1_DECOMPOSITION.value})
    ctx.event("stage.entered", {"stage": RunStage.SPECIFYING.value})
    spec["open_questions"] = [{"question": "Which clock?", "blocking": True, "default_if_unanswered": "UTC"}]
    gate_2(ctx, examples["outcome"], {spec["spec_id"]: spec}, passing, {})
    ctx.event("stage.entered", {"stage": RunStage.SPECIFYING.value})
    failing = copy.deepcopy(passing)
    failing["overall"].update(result="revise", blocking_issue_count=2)
    spec["open_questions"] = []
    gate_2(ctx, examples["outcome"], {spec["spec_id"]: spec}, failing, {})
    assert seen == [(GateKind.SPEC_READINESS, None), (GateKind.ESCALATION, None)]
