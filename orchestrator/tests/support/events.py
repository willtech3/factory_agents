"""Assertions about a run's event log that any test driving real stage code can reuse."""
from pathlib import Path

from factory import checks


def assert_log_valid(store, run_id: str, schemas_dir: Path) -> None:
    """Every event the run appended validates against run_event.schema.json, the contract replay and the Console read."""
    for ev in store.events(run_id):
        result = checks.schema_valid(ev.to_dict(), schemas_dir / "run_event.schema.json", schemas_dir)
        assert result.passed, (ev.kind, result.detail)
