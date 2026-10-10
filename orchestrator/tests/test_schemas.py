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
