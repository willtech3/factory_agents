"""RunStore: artefacts are never silently overwritten; the event log numbers events contiguously under concurrency."""
import json
from pathlib import Path

import pytest

from factory.state import IllegalTransition, RunStage
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
    store.append(run_id, "run.created", {"type": "orchestrator"}, {"policy": {"preview_mode": "local"}, "budget_usd": 50, "outcome_id": "o"})

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
    cached state) must still get the next seq and fold the other's events in before validating its own."""
    a, b = RunStore(tmp_path), RunStore(tmp_path)
    run_id = "run_store000003"
    assert a.append(run_id, "run.created", {"type": "orchestrator"}, {"outcome_id": "o", "budget_usd": 50}).seq == 0
    assert b.append(run_id, "stage.entered", {"type": "orchestrator"}, {"stage": "decomposing"}).seq == 1
    assert a.append(run_id, "gate.opened", {"type": "orchestrator"}, {"gate_id": "g", "summary": "x" * 200_000}).seq == 2
    assert b.append(run_id, "gate.decided", {"type": "human"}, {"gate_id": "g", "decision": "approve"}).seq == 3
    assert a.append(run_id, "stage.entered", {"type": "orchestrator"}, {"stage": "gate_1_decomposition"}).seq == 4
    assert [e.seq for e in RunStore(tmp_path).events(run_id)] == [0, 1, 2, 3, 4]
    # b's cached state is behind a's last append: it must catch up, not validate against its stale stage
    with pytest.raises(IllegalTransition):
        b.append(run_id, "stage.entered", {"type": "orchestrator"}, {"stage": "implementing"})   # gate_1 -> implementing
    assert b.state(run_id).stage == RunStage.GATE_1_DECOMPOSITION and b.last_seq(run_id) == 4


def _append_from_process(root: str, run_id: str, n: int) -> list[int]:
    store = RunStore(Path(root))
    return [store.append(run_id, "artifact.produced", {"type": "agent"}, {"artifact_type": "probe", "i": i}).seq for i in range(n)]


def test_event_seqs_are_contiguous_when_processes_append_at_once(tmp_path):
    """SQLite serialises writers across processes, which the old flock and cached-tail logic did by hand."""
    from concurrent.futures import ProcessPoolExecutor
    run_id = "run_store000004"
    RunStore(tmp_path).append(run_id, "run.created", {"type": "orchestrator"}, {"outcome_id": "o", "budget_usd": 50})
    with ProcessPoolExecutor(4) as pool:
        seqs = [s for out in pool.map(_append_from_process, [str(tmp_path)] * 4, [run_id] * 4, [25] * 4) for s in out]
    assert sorted(seqs) == list(range(1, 101))
    assert [e.seq for e in RunStore(tmp_path).events(run_id)] == list(range(101))


def test_a_refused_event_leaves_no_trace_and_the_run_readable(tmp_path):
    store = RunStore(tmp_path)
    run_id = "run_store000005"
    store.append(run_id, "run.created", {"type": "orchestrator"}, {"outcome_id": "o", "budget_usd": 50})
    with pytest.raises(KeyError):
        store.append(run_id, "work_item.stage", {"type": "orchestrator"}, {"stage": "implementing"}, work_item_id="wi-none")
    with pytest.raises(IllegalTransition):
        store.append(run_id, "run.completed", {"type": "orchestrator"})                      # intake -> completed
    assert [e.seq for e in store.events(run_id)] == [0] and RunStore(tmp_path).state(run_id).last_seq == 0
    assert store.append(run_id, "stage.entered", {"type": "orchestrator"}, {"stage": "decomposing"}).seq == 1


def test_a_legacy_jsonl_log_is_imported_once_and_kept(tmp_path):
    """A run an older controller wrote: its events.jsonl is imported as it is on first open, then appends continue."""
    run_id = "run_store000006"
    legacy = RunStore(tmp_path / "scratch")
    events = [legacy.append(run_id, "run.created", {"type": "orchestrator"}, {"outcome_id": "o", "budget_usd": 50}),
              legacy.append(run_id, "stage.entered", {"type": "orchestrator"}, {"stage": "decomposing"})]
    (tmp_path / run_id).mkdir()
    (tmp_path / run_id / "events.jsonl").write_text(legacy.export_events(run_id))
    store = RunStore(tmp_path)
    assert store.exists(run_id) and list(store.list_runs()) == [run_id]
    assert [e.to_dict() for e in store.events(run_id)] == [e.to_dict() for e in events]
    assert not (tmp_path / run_id / "events.jsonl").exists() and (tmp_path / run_id / "events.imported.jsonl").is_file()
    assert store.append(run_id, "stage.entered", {"type": "orchestrator"}, {"stage": "gate_1_decomposition"}).seq == 2
    assert RunStore(tmp_path).state(run_id).stage == RunStage.GATE_1_DECOMPOSITION
    assert [json.loads(line)["seq"] for line in store.export_events(run_id).splitlines()] == [0, 1, 2]


def test_reading_a_run_that_does_not_exist_creates_nothing(tmp_path):
    store = RunStore(tmp_path)
    assert store.events("run_missing01") == [] and store.last_seq("run_missing01") == -1
    assert store.state("run_missing01").last_seq == -1 and store.step("run_missing01", "k") is None
    assert not (tmp_path / "run_missing01").exists() and list(store.list_runs()) == []


def test_steps_record_attempts_sessions_and_results(tmp_path):
    store = RunStore(tmp_path)
    run_id = "run_store000007"
    first = store.begin_step(run_id, "session/review/r1/p1", "session", "a" * 64)
    assert (first.attempt, first.status) == (1, "running")
    store.set_step_session(run_id, "session/review/r1/p1", "sesn_1")
    assert store.step(run_id, "session/review/r1/p1").session_id == "sesn_1"
    store.fail_step(run_id, "session/review/r1/p1", "budget_reached", {"session_id": "sesn_1"})
    again = store.begin_step(run_id, "session/review/r1/p1", "session", "a" * 64)
    assert again.attempt == 2 and store.step(run_id, "session/review/r1/p1").session_id is None
    store.finish_step(run_id, "session/review/r1/p1", {"outputs": {"findings.json": "b" * 64}})
    done = store.step(run_id, "session/review/r1/p1")
    assert done.status == "done" and done.result == {"outputs": {"findings.json": "b" * 64}} and done.error is None
    assert [s.key for s in store.steps(run_id)] == ["session/review/r1/p1"]
    with pytest.raises(KeyError):
        store.finish_step(run_id, "no/such/step", {})


def test_blobs_are_content_addressed_and_checked(tmp_path):
    store = RunStore(tmp_path)
    digest = store.put_blob("run_store000008", b"findings")
    assert store.put_blob("run_store000008", b"findings") == digest and store.get_blob("run_store000008", digest) == b"findings"
    (tmp_path / "run_store000008" / "blobs" / digest).write_bytes(b"tampered")
    with pytest.raises(ValueError):
        store.get_blob("run_store000008", digest)
