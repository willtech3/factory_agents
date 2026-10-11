"""RunStore: artefacts are never silently overwritten; the event log numbers events contiguously under concurrency."""
import json

import pytest

from factory.store import ArtifactConflict, RunStore


def test_put_artifact_refuses_different_content_under_the_same_name(tmp_path):
    store = RunStore(tmp_path)
    sha = store.put_artifact("run_store000001", "specify", "wi-a-r1-spec.json", b'{"a": 1}')
    assert store.put_artifact("run_store000001", "specify", "wi-a-r1-spec.json", b'{"a": 1}') == sha   # same bytes: no-op
    with pytest.raises(ArtifactConflict):
        store.put_artifact("run_store000001", "specify", "wi-a-r1-spec.json", b'{"a": 2}')
    assert store.get_artifact("run_store000001", "specify", "wi-a-r1-spec.json") == b'{"a": 1}'       # the original survives
    assert store.sha256("run_store000001", "specify", "wi-a-r1-spec.json") == sha
    # the orchestrator's own pointer files may be replaced explicitly
    sha2 = store.put_artifact("run_store000001", "specify", "wi-a-r1-spec.json", b'{"a": 2}', replace=True)
    assert sha2 != sha and json.loads(store.get_artifact("run_store000001", "specify", "wi-a-r1-spec.json")) == {"a": 2}
    assert store.sha256("run_store000001", "specify", "wi-a-r1-spec.json") == sha2


def test_event_seqs_are_contiguous_when_threads_append_at_once(tmp_path):
    """Post-mortem issue 2: five threads appending to one run produced seqs [1, 1, 1, 1, 2] and replay refused the run."""
    from concurrent.futures import ThreadPoolExecutor

    store = RunStore(tmp_path)
    run_id = "run_store000002"
    store.append(run_id, "run.created", {"type": "orchestrator"}, {"outcome_id": "o"})

    def burst(i: int) -> list[int]:
        return [store.append(run_id, "artifact.produced", {"type": "agent"}, {"artifact_type": "probe", "thread": i}).seq
                for _ in range(20)]

    with ThreadPoolExecutor(5) as pool:
        seqs = [s for out in pool.map(burst, range(5)) for s in out]
    assert sorted(seqs) == list(range(1, 101))
    assert [e.seq for e in store.events(run_id)] == list(range(101))
    assert store.state(run_id).last_seq == 100                      # replay accepts the log


def test_reverification_keeps_old_and_new_evidence_without_unlocking_raw_outputs(tmp_path):
    store = RunStore(tmp_path)
    run, stage, name = "run_reverify", "implement", "spec_a-r2-test_results.json"
    old = store.put_artifact(run, stage, name, b"old proof")
    new = store.put_derived_artifact(run, stage, name, b"new proof")
    assert store.get_artifact(run, stage, name) == b"new proof"
    assert store.get_artifact(run, "history", f"{stage}/{name}/{old}") == b"old proof"
    assert store.get_artifact(run, "history", f"{stage}/{name}/{new}") == b"new proof"
    assert store.put_derived_artifact(run, stage, name, b"new proof") == new
    with pytest.raises(ValueError):
        store.put_derived_artifact(run, stage, "spec_a-r2-implementation_report.json", b"replacement")


def test_event_seqs_stay_contiguous_across_store_instances(tmp_path):
    """The Console and `factory resume` append from other processes: a second RunStore over the same directory (its own
    cache, its own lock) must still get the next seq, and a line longer than the tail chunk must not confuse it."""
    a, b = RunStore(tmp_path), RunStore(tmp_path)
    run_id = "run_store000003"
    assert a.append(run_id, "run.created", {"type": "orchestrator"}, {"outcome_id": "o"}).seq == 0
    assert b.append(run_id, "stage.entered", {"type": "orchestrator"}, {"stage": "decomposing"}).seq == 1
    assert a.append(run_id, "gate.opened", {"type": "orchestrator"}, {"gate_id": "g", "summary": "x" * 200_000}).seq == 2
    assert b.append(run_id, "gate.decided", {"type": "human"}, {"gate_id": "g", "decision": "approve"}).seq == 3
    assert a.append(run_id, "stage.entered", {"type": "orchestrator"}, {"stage": "gate_1_decomposition"}).seq == 4
    assert [e.seq for e in RunStore(tmp_path).events(run_id)] == [0, 1, 2, 3, 4]
