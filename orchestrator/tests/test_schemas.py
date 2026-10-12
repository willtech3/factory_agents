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
    "implementation_report": "implementation_report.schema.json",
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


def test_sandbox_tool_infers_the_implementation_report_schema(root, schemas_dir, tmp_path):
    """The implementer validates implementation_report.json in-sandbox like every other artefact (post-mortem issue 5)."""
    sys.path.insert(0, str(root / "orchestrator" / "factory" / "sandbox_tools"))
    import validate  # noqa: E402

    assert validate._guess_schema(Path("/mnt/session/outputs/implementation_report.json")) == "implementation_report.schema.json"
    assert validate.validate_file(root / "examples" / "ledgerline" / "implementation_report.json", schemas_dir) == []
    broken = json.loads((root / "examples" / "ledgerline" / "implementation_report.json").read_text())
    broken["criteria"][0]["tests"] = "tests/test_x.py::test_a"            # a string, not a list of node ids
    broken["acceptance_criteria"] = {}                                      # the old, unparsed shape
    p = tmp_path / "implementation_report.json"
    p.write_text(json.dumps(broken))
    errors = validate.validate_file(p, schemas_dir)
    assert any("tests" in e and "array" in e for e in errors) and any("acceptance_criteria" in e for e in errors)


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


def test_merge_readiness_requires_the_judges_criteria_table(schemas_dir, examples):
    import copy

    verdict = copy.deepcopy(examples["merge_readiness"])
    del verdict["criteria"]
    r = checks.schema_valid(verdict, schemas_dir / "merge_readiness.schema.json", schemas_dir)
    assert not r.passed and "criteria" in r.detail
    verdict = copy.deepcopy(examples["merge_readiness"])
    verdict["criteria"][0]["status"] = "untestable_as_written"       # the reviewer's vocabulary, not the judge's
    assert not checks.schema_valid(verdict, schemas_dir / "merge_readiness.schema.json", schemas_dir).passed
    verdict["criteria"][0]["status"] = "not_assessed"
    assert checks.schema_valid(verdict, schemas_dir / "merge_readiness.schema.json", schemas_dir).passed


def test_service_manifest_accepts_a_runtime_setup_command(schemas_dir, examples):
    import copy

    manifest = copy.deepcopy(examples["service_manifest"])
    manifest["runtime"]["setup"] = "bundle install --without production"
    assert checks.schema_valid(manifest, schemas_dir / "service_manifest.schema.json", schemas_dir).passed
    manifest["runtime"]["setup"] = ["not", "a", "string"]
    assert not checks.schema_valid(manifest, schemas_dir / "service_manifest.schema.json", schemas_dir).passed


def test_review_finding_may_carry_the_environmental_flag(schemas_dir, examples):
    import copy

    doc = copy.deepcopy(examples["findings_round1"])
    doc["findings"][0]["environmental"] = True
    assert checks.schema_valid(doc, schemas_dir / "review_findings.schema.json", schemas_dir).passed
    doc["findings"][0]["environmental"] = "yes"
    assert not checks.schema_valid(doc, schemas_dir / "review_findings.schema.json", schemas_dir).passed


def test_every_agent_definition_names_the_model_its_provenance_records():
    """Stages stamp provenance with the model read from agents/<key>.md; a definition it cannot read would fail mid-run."""
    from factory.config import AGENT_FILES, agent_model
    assert all(agent_model(key).startswith("claude-") for key in AGENT_FILES)
