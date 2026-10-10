"""Every example artifact validates against its schema with both validators:
the full jsonschema library (orchestrator side) and the dependency-free sandbox tool.
"""
import json
import sys
from pathlib import Path

import pytest

from factory import checks

EXAMPLE_TO_SCHEMA = {
    "outcome": "outcome.schema.json",
    "decomposition": "decomposition.schema.json",
    "spec_smart_retries": "spec.schema.json",
    "verdict_round2": "judge_verdict.schema.json",
    "findings_round1": "review_findings.schema.json",
    "review_response_round1": "review_response.schema.json",
    "merge_readiness": "merge_readiness.schema.json",
    "service_manifest": "service_manifest.schema.json",
}


@pytest.mark.parametrize("name,schema", EXAMPLE_TO_SCHEMA.items())
def test_examples_validate_with_jsonschema(examples, schemas_dir, name, schema):
    result = checks.schema_valid(examples[name], schemas_dir / schema, schemas_dir)
    assert result.passed, result.detail


@pytest.mark.parametrize("name,schema", EXAMPLE_TO_SCHEMA.items())
def test_examples_validate_with_sandbox_tool(root, schemas_dir, name, schema):
    sys.path.insert(0, str(root / "orchestrator" / "factory" / "sandbox_tools"))
    import validate  # noqa: E402

    errors = validate.validate_file(root / "examples" / "ledgerline" / f"{name}.json", schemas_dir, schema)
    assert errors == [], errors


def test_sandbox_tool_catches_missing_required(tmp_path, schemas_dir, root, examples):
    sys.path.insert(0, str(root / "orchestrator" / "factory" / "sandbox_tools"))
    import validate  # noqa: E402

    broken = dict(examples["spec_smart_retries"])
    broken.pop("acceptance_criteria")
    broken["allowed_paths"] = []
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(broken))
    errors = validate.validate_file(p, schemas_dir)
    assert any("acceptance_criteria" in e for e in errors)
    assert any("allowed_paths" in e and "fewer" in e for e in errors)


def test_all_schemas_are_valid_json_schema(schemas_dir):
    import jsonschema

    for p in schemas_dir.glob("*.schema.json"):
        schema = json.loads(p.read_text())
        jsonschema.Draft202012Validator.check_schema(schema)


def test_spec_requires_user_visible_and_e2e(schemas_dir, examples):
    import copy

    spec = copy.deepcopy(examples["spec_smart_retries"])
    del spec["acceptance_criteria"][0]["user_visible"]
    r = checks.schema_valid(spec, schemas_dir / "spec.schema.json", schemas_dir)
    assert not r.passed and "user_visible" in r.detail
    spec = copy.deepcopy(examples["spec_smart_retries"])
    del spec["test_plan"]["e2e"]
    r = checks.schema_valid(spec, schemas_dir / "spec.schema.json", schemas_dir)
    assert not r.passed and "e2e" in r.detail
    spec = copy.deepcopy(examples["spec_smart_retries"])
    spec["test_plan"]["e2e"][0]["target"] = "production"
    assert not checks.schema_valid(spec, schemas_dir / "spec.schema.json", schemas_dir).passed


def test_run_event_accepts_deploy_kinds(schemas_dir):
    for kind in ("deploy.started", "deploy.succeeded", "deploy.failed", "e2e.dev_passed", "e2e.dev_failed"):
        ev = {"event_id": "evt_1", "run_id": "run_abcdefgh", "seq": 0, "at": "2026-10-09T10:00:00Z", "kind": kind,
              "actor": {"type": "platform"}, "payload": {}}
        assert checks.schema_valid(ev, schemas_dir / "run_event.schema.json", schemas_dir).passed


def test_run_event_uses_change_request_kinds(schemas_dir):
    """Event kinds are forge-neutral: cr.* (a change request is a GitHub PR or a GitLab MR), never mr.*."""
    for kind, ok in (("cr.opened", True), ("cr.merged", True), ("cr.closed", True),
                     ("mr.opened", False), ("mr.merged", False), ("pr.opened", False)):
        ev = {"event_id": "evt_1", "run_id": "run_abcdefgh", "seq": 0, "at": "2026-10-09T10:00:00Z", "kind": kind,
              "actor": {"type": "platform"}, "payload": {}}
        assert checks.schema_valid(ev, schemas_dir / "run_event.schema.json", schemas_dir).passed is ok, kind


def test_outcome_repository_may_be_created(schemas_dir, examples):
    import copy

    outcome = copy.deepcopy(examples["outcome"])
    outcome["target_repositories"][0].update({"create": True, "visibility": "private"})
    assert checks.schema_valid(outcome, schemas_dir / "outcome.schema.json", schemas_dir).passed
    outcome["target_repositories"][0]["visibility"] = "secret"
    assert not checks.schema_valid(outcome, schemas_dir / "outcome.schema.json", schemas_dir).passed


def test_outcome_accepts_measured_metrics(schemas_dir, examples):
    import copy

    outcome = copy.deepcopy(examples["outcome"])
    outcome["success_metrics"][0].update({"measured_at": "2026-12-20T09:00:00Z", "measured_value": "3.1%"})
    assert checks.schema_valid(outcome, schemas_dir / "outcome.schema.json", schemas_dir).passed
