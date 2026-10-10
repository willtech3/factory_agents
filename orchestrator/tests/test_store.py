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
