import pytest

from factory.gates import ArtifactRef, Decision, Gate, GateDecision, GateKind, StaleDecision, validate_decision
from factory.state import IllegalTransition, RunStage, WorkItemStage
from factory.store import RunStore


def test_happy_path_run_state(tmp_path, examples):
    store = RunStore(tmp_path)
    run_id = "run_test000001"
    dec = examples["decomposition"]
    store.append(run_id, "run.created", {"type": "orchestrator"}, {"outcome_id": "out_ledger0001"})
    store.append(run_id, "stage.entered", {"type": "orchestrator"}, {"stage": "decomposing"})
    store.append(run_id, "artifact.produced", {"type": "agent"}, {"artifact_type": "decomposition", "revision": 1,
                                                                 "work_items": dec["work_items"]})
    store.append(run_id, "session.finished", {"type": "platform"}, {"session_id": "s1", "list_cost_usd": 6.4})
    store.append(run_id, "stage.entered", {"type": "orchestrator"}, {"stage": "gate_1_decomposition"})
    store.append(run_id, "gate.opened", {"type": "orchestrator"}, {"gate_id": "g1"})
    st = store.state(run_id)
    assert st.stage == RunStage.GATE_1_DECOMPOSITION
    assert set(st.work_items) == {w["work_item_id"] for w in dec["work_items"]}
    assert "g1" in st.open_gates and st.list_cost_usd == 6.4

    store.append(run_id, "gate.decided", {"type": "human"}, {"gate_id": "g1", "decision": "approve"})
    store.append(run_id, "stage.entered", {"type": "orchestrator"}, {"stage": "specifying"})
    for wid in st.work_items:
        store.append(run_id, "work_item.stage", {"type": "orchestrator"}, {"stage": "spec_drafting", "spec_round": 1}, work_item_id=wid)
        store.append(run_id, "work_item.stage", {"type": "orchestrator"}, {"stage": "spec_judging", "spec_id": f"spec_{wid}"}, work_item_id=wid)
    store.append(run_id, "stage.entered", {"type": "orchestrator"}, {"stage": "gate_2_spec_readiness"})
    store.append(run_id, "work_item.stage", {"type": "human"}, {"stage": "spec_approved"}, work_item_id="wi-smart-retries")
    st = store.state(run_id)
    assert st.stage == RunStage.GATE_2_SPEC_READINESS
    # Only the root item is ready: the others are not approved yet.
    assert [w.work_item_id for w in st.ready_work_items()] == ["wi-smart-retries"]


def test_illegal_run_transition(tmp_path):
    store = RunStore(tmp_path)
    store.append("run_x", "run.created", {"type": "orchestrator"}, {"outcome_id": "o"})
    store.append("run_x", "stage.entered", {"type": "orchestrator"}, {"stage": "implementing"})  # intake -> implementing
    with pytest.raises(IllegalTransition):
        store.state("run_x")


def test_illegal_work_item_transition(tmp_path, examples):
    store = RunStore(tmp_path)
    store.append("run_y", "run.created", {"type": "orchestrator"}, {"outcome_id": "o"})
    store.append("run_y", "artifact.produced", {"type": "agent"}, {"artifact_type": "decomposition", "revision": 1,
                                                                 "work_items": examples["decomposition"]["work_items"]})
    store.append("run_y", "work_item.stage", {"type": "orchestrator"}, {"stage": "merged"}, work_item_id="wi-smart-retries")
    with pytest.raises(IllegalTransition):
        store.state("run_y")


def test_dependency_gating(tmp_path, examples):
    store = RunStore(tmp_path)
    store.append("run_z", "run.created", {"type": "orchestrator"}, {"outcome_id": "o"})
    store.append("run_z", "artifact.produced", {"type": "agent"}, {"artifact_type": "decomposition", "revision": 1,
                                                                 "work_items": examples["decomposition"]["work_items"]})
    for wid in ("wi-smart-retries", "wi-dunning-emails"):
        for stage in ("spec_drafting", "spec_judging", "spec_approved"):
            store.append("run_z", "work_item.stage", {"type": "orchestrator"}, {"stage": stage}, work_item_id=wid)
    st = store.state("run_z")
    assert [w.work_item_id for w in st.ready_work_items()] == ["wi-smart-retries"]  # emails depends on retries
    for stage in ("implementing", "reviewing", "merge_judging", "integrating", "cr_open", "merged"):
        store.append("run_z", "work_item.stage", {"type": "orchestrator"}, {"stage": stage}, work_item_id="wi-smart-retries")
    st = store.state("run_z")
    assert [w.work_item_id for w in st.ready_work_items()] == ["wi-dunning-emails"]
    assert st.work_items["wi-smart-retries"].stage == WorkItemStage.MERGED


def test_gate_decision_binds_artifact_hashes():
    gate = Gate(run_id="r", kind=GateKind.DECOMPOSITION_REVIEW,
                artifacts=(ArtifactRef("decomposition.json", "a" * 64), ArtifactRef("decomposition.md", "b" * 64)),
                assignees=("priya.n",), summary="s", options=(Decision.APPROVE, Decision.REQUEST_CHANGES, Decision.REJECT))
    ok = GateDecision(gate_id=gate.gate_id, decision=Decision.APPROVE, decided_by="priya.n", artifact_shas=("b" * 64, "a" * 64))
    validate_decision(gate, ok)
    stale = GateDecision(gate_id=gate.gate_id, decision=Decision.APPROVE, decided_by="priya.n", artifact_shas=("a" * 64, "c" * 64))
    with pytest.raises(StaleDecision):
        validate_decision(gate, stale)
    no_comment = GateDecision(gate_id=gate.gate_id, decision=Decision.REQUEST_CHANGES, decided_by="priya.n",
                              artifact_shas=("a" * 64, "b" * 64))
    with pytest.raises(ValueError):
        validate_decision(gate, no_comment)
    wrong_option = GateDecision(gate_id=gate.gate_id, decision=Decision.APPROVE_SUBSET, decided_by="priya.n",
                                artifact_shas=("a" * 64, "b" * 64), approved_subset=("spec_x",))
    with pytest.raises(ValueError):
        validate_decision(gate, wrong_option)


def _merge_item(store, run_id, wid):
    for stage in ("spec_drafting", "spec_judging", "spec_approved", "implementing", "reviewing", "merge_judging",
                  "integrating", "cr_open", "merged"):
        store.append(run_id, "work_item.stage", {"type": "orchestrator"}, {"stage": stage}, work_item_id=wid)


def test_deploy_stages_follow_merge(tmp_path, examples):
    store = RunStore(tmp_path)
    store.append("run_d", "run.created", {"type": "orchestrator"}, {"outcome_id": "o"})
    store.append("run_d", "artifact.produced", {"type": "agent"}, {"artifact_type": "decomposition", "revision": 1,
                                                                 "work_items": examples["decomposition"]["work_items"]})
    _merge_item(store, "run_d", "wi-smart-retries")
    for stage in ("spec_drafting", "spec_judging", "spec_approved"):
        store.append("run_d", "work_item.stage", {"type": "orchestrator"}, {"stage": stage}, work_item_id="wi-dunning-emails")
    store.append("run_d", "work_item.stage", {"type": "orchestrator"}, {"stage": "deploying_dev"}, work_item_id="wi-smart-retries")
    store.append("run_d", "deploy.started", {"type": "platform"}, {"pipeline_id": 77, "sha": "abc"}, work_item_id="wi-smart-retries")
    st = store.state("run_d")
    assert st.work_items["wi-smart-retries"].stage == WorkItemStage.DEPLOYING_DEV
    assert st.work_items["wi-smart-retries"].dev_pipeline_id == 77
    # A dependant may start once the upstream item is on the default branch, deploy still in flight.
    assert [w.work_item_id for w in st.ready_work_items()] == ["wi-dunning-emails"]
    store.append("run_d", "deploy.succeeded", {"type": "platform"}, {"pipeline_id": 77, "environment_url": "https://dev.example.internal"},
                 work_item_id="wi-smart-retries")
    store.append("run_d", "e2e.dev_passed", {"type": "orchestrator"}, {"environment_url": "https://dev.example.internal", "results": []},
                 work_item_id="wi-smart-retries")
    store.append("run_d", "work_item.stage", {"type": "orchestrator"}, {"stage": "deployed_dev"}, work_item_id="wi-smart-retries")
    st = store.state("run_d")
    assert st.work_items["wi-smart-retries"].stage == WorkItemStage.DEPLOYED_DEV
    assert st.work_items["wi-smart-retries"].dev_environment_url == "https://dev.example.internal"


def test_deploy_failure_routes_back_to_implementing(tmp_path, examples):
    store = RunStore(tmp_path)
    store.append("run_e", "run.created", {"type": "orchestrator"}, {"outcome_id": "o"})
    store.append("run_e", "artifact.produced", {"type": "agent"}, {"artifact_type": "decomposition", "revision": 1,
                                                                 "work_items": examples["decomposition"]["work_items"]})
    _merge_item(store, "run_e", "wi-smart-retries")
    for stage in ("deploying_dev", "escalated", "implementing"):
        store.append("run_e", "work_item.stage", {"type": "orchestrator"}, {"stage": stage}, work_item_id="wi-smart-retries")
    assert store.state("run_e").work_items["wi-smart-retries"].stage == WorkItemStage.IMPLEMENTING


def test_illegal_deploy_transitions(tmp_path, examples):
    store = RunStore(tmp_path)
    store.append("run_f", "run.created", {"type": "orchestrator"}, {"outcome_id": "o"})
    store.append("run_f", "artifact.produced", {"type": "agent"}, {"artifact_type": "decomposition", "revision": 1,
                                                                 "work_items": examples["decomposition"]["work_items"]})
    _merge_item(store, "run_f", "wi-smart-retries")
    store.append("run_f", "work_item.stage", {"type": "orchestrator"}, {"stage": "deployed_dev"}, work_item_id="wi-smart-retries")
    with pytest.raises(IllegalTransition):   # merged -> deployed_dev skips the observation
        store.state("run_f")
    store2 = RunStore(tmp_path / "g")
    store2.append("run_g", "run.created", {"type": "orchestrator"}, {"outcome_id": "o"})
    store2.append("run_g", "artifact.produced", {"type": "agent"}, {"artifact_type": "decomposition", "revision": 1,
                                                                  "work_items": examples["decomposition"]["work_items"]})
    _merge_item(store2, "run_g", "wi-smart-retries")
    store2.append("run_g", "work_item.stage", {"type": "orchestrator"}, {"stage": "deploying_dev"}, work_item_id="wi-smart-retries")
    store2.append("run_g", "work_item.stage", {"type": "orchestrator"}, {"stage": "implementing"}, work_item_id="wi-smart-retries")
    with pytest.raises(IllegalTransition):   # deploying_dev -> implementing only via the escalation gate
        store2.state("run_g")


def test_design_only_runs_complete_at_gate_2(tmp_path):
    store = RunStore(tmp_path)
    for kind, payload in (("run.created", {"outcome_id": "o"}), ("stage.entered", {"stage": "decomposing"}),
                          ("stage.entered", {"stage": "gate_1_decomposition"}), ("stage.entered", {"stage": "specifying"}),
                          ("stage.entered", {"stage": "gate_2_spec_readiness"}), ("run.completed", {"design_only": True})):
        store.append("run_design", kind, {"type": "orchestrator"}, payload)
    assert store.state("run_design").stage == RunStage.COMPLETED


def test_notes_by_item_routes_gate_1_approval_notes():
    from factory.runner import notes_by_item
    notes = notes_by_item("keep it small\nwi-a: drop the cache\nwi-b:\nunknown-x: ignore me", ["wi-a", "wi-b"])
    assert "- keep it small" in notes["wi-a"] and "- drop the cache" in notes["wi-a"]
    assert notes["wi-b"].count("- ") == 2 and "unknown-x: ignore me" in notes["wi-b"] and "drop the cache" not in notes["wi-b"]
    assert notes_by_item("", ["wi-a"]) == {}
