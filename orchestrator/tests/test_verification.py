"""ADR-0014: one verification pipeline. The controller renders the test plan (structured commands and journeys), every
target returns the same evidence bundle, `judge` turns it into the merge checks, and one definition of a passing row
holds for commands, e2e entries on either target, and Stage 5's rows against dev. Real subprocesses, no network."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from support.bun import PASSING_BUN
from support.bun import fake_bun  # noqa: F401  (fixture)
from test_deploy_observe import RUN, SHA, ScriptedSource, _ctx, _seed_merged

from factory import checks, verification
from factory.checks import CheckResult
from factory.gates import InMemoryQueue
from factory.local_verifier import SETUP_CHECK, LocalVerifier
from factory.plans import plan_of, render_command
from factory.stages.deploy_observe import DeployObservation, observe_once
from factory.stages.common import git

JOURNEY = {"journey": "add-model", "target": "preview", "covers_ac": ["AC-1"], "description": "add a model",
           "visual": {"persona": "admin", "viewport": {"width": 1280, "height": 800}, "steps": ["open the catalog"],
                      "environment": {"name": "test", "data": "synthetic", "authentication": "none-required"}}}


# ---------------------------------------------------------------------------------------------- the plan

def test_the_controller_renders_structured_commands_and_journeys():
    assert render_command({"runner": "pytest", "select": ["tests/test_a.py", "tests/test_b.py::test_x"]}) == \
        "python3 -m pytest -q tests/test_a.py tests/test_b.py::test_x"
    assert render_command({"runner": "bun", "select": ["src/catalog"]}) == "bun test src/catalog"
    assert render_command({"runner": "pytest"}) == "python3 -m pytest -q"
    assert render_command({"runner": "pytest", "select": ["a; rm -rf /"]}) == "python3 -m pytest -q 'a; rm -rf /'"
    assert render_command("ruff check .") == "ruff check ."                    # the shell form older specs use
    plan = plan_of({"test_plan": {"commands": [{"runner": "bun", "select": ["src"]}, "ruff check ."],
                                  "e2e": [JOURNEY, {"command": "curl -f $BASE_URL/health", "target": "local", "covers_ac": ["AC-2"]}]}})
    assert plan.commands == ["bun test src", "ruff check ."]
    assert plan.e2e[0]["command"] == "factory:journey add-model" and plan.e2e[0]["journey"] == "add-model"
    assert plan.e2e[1]["command"] == "curl -f $BASE_URL/health"


def test_the_spec_schema_takes_structured_entries_and_refuses_flags_or_ambiguous_entries(examples, schemas_dir):
    spec = dict(examples["spec_smart_retries"])
    schema = schemas_dir / "spec.schema.json"

    def valid(test_plan):
        return checks.schema_valid({**spec, "test_plan": {**spec["test_plan"], **test_plan}}, schema, schemas_dir).passed
    assert valid({"commands": [{"runner": "pytest", "select": ["spec/x_spec.rb"]}, {"runner": "bun"}, "bundle exec rubocop"]})
    assert valid({"e2e": [*spec["test_plan"]["e2e"], {**JOURNEY, "covers_ac": ["AC-1"]}]})
    assert not valid({"commands": [{"runner": "pytest", "select": ["-k", "x"]}]})            # a selection is never a flag
    assert not valid({"commands": [{"runner": "pytest", "args": "--lf"}]})
    assert not valid({"commands": [{"runner": "make"}]})
    assert not valid({"e2e": [{**JOURNEY, "command": "factory:journey add-model"}]})          # a command or a journey, not both
    assert not valid({"e2e": [{k: v for k, v in JOURNEY.items() if k != "journey"}]})


# ---------------------------------------------------------------------------------------------- one evidence shape

def _verifier(tmp_path: Path) -> LocalVerifier:
    v = LocalVerifier("unused", tmp_path / "v")
    v.setup = CheckResult(SETUP_CHECK, True, "the host's tools stand in for the per-run environment")
    return v


def test_a_bun_test_yields_evidence_on_the_controllers_host_too(tmp_path, fake_bun, monkeypatch):
    """Before ADR-0014 only the hosted harness attached Bun's reporter; the controller's runs now use the same code."""
    for key in ("PATH", "FAKE_BUN_ARGV", "FAKE_BUN_REPORT"):
        monkeypatch.setenv(key, fake_bun[key])
    repo = tmp_path / "repo"
    repo.mkdir()
    rows = _verifier(tmp_path).run_commands(repo, plan_of({"test_plan": {"commands": [{"runner": "bun", "select": ["src/catalog"]}]}}).commands)
    assert rows[0]["command"] == "bun test src/catalog" and rows[0]["instrumented"] == "bun-junit"
    assert rows[0]["tests"]["status"] == "reported" and PASSING_BUN in rows[0]["tests"]["passed"]
    assert fake_bun["argv"].read_text().startswith("test --reporter=junit --reporter-outfile=")
    passing, known = verification.evidence_from_reports(rows, None)
    assert PASSING_BUN in passing and set(passing) <= set(known)


class Clone(LocalVerifier):
    """The controller's clone with nothing to install and no pytest to collect: commands and e2e entries run for real."""

    def prepare_environment(self, repo, manifest=None):
        self.setup = CheckResult(SETUP_CHECK, True, "nothing to install", {})
        return self.setup

    def collect_test_ids(self, repo, timeout=300):
        return None


def test_a_failing_local_entry_fails_its_check_in_platform_mode(tmp_path):
    """A `target: local` entry used to pass in every mode unless a criterion mapped to it. Every entry now runs in the
    plan's order, and `judge` checks each target: the local entries ran on the controller's clone, the preview entries
    were not run (no preview deployment), and both fail closed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("X = 1\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")
    head = git(repo, "rev-parse", "HEAD").strip()
    spec = {"spec_id": "spec_v", "allowed_paths": ["app.py"], "acceptance_criteria": [],
            "test_plan": {"commands": ["true"], "new_tests": [], "e2e": [
                {"command": "true", "target": "local", "covers_ac": ["AC-1"], "description": "passes"},
                {"command": "exit 3", "target": "local", "covers_ac": ["AC-1"], "description": "fails"},
                {"command": "curl -f $BASE_URL", "target": "preview", "covers_ac": ["AC-1"], "description": "no preview"}]}}
    ctx = SimpleNamespace(cfg=SimpleNamespace(preview_mode="platform", service_manifest_path="service_manifest.json",
                                              preview_environment_prefix="review/"),
                          policy=SimpleNamespace(preview_wait_seconds=0), forge=None)
    target = verification.target_for(ctx, repo, head, head, "main")
    bundle = verification.verify(ctx, target, plan_of(spec), spec=spec, verifier=Clone("unused", tmp_path / "v"))
    assert target.kind == "platform" and [r["command"] for r in bundle.e2e] == ["true", "exit 3", "curl -f $BASE_URL"]
    assert [r.get("exit_code") for r in bundle.e2e] == [0, 3, None]
    assert bundle.test_results()["environment"]["host"] == "platform"
    diff = {"changed": [], "deleted": [], "existing_tests": [], "added_lines": {}, "removed_lines": {}}
    det = {d.check: d for d in verification.judge(bundle, spec, diff=diff, report={"criteria": []})}
    assert det["test_plan_commands"].passed
    assert not det["e2e_local"].passed and "exit 3" in det["e2e_local"].detail and "'true'" not in det["e2e_local"].detail
    assert not det["e2e_preview"].passed and "not run" in det["e2e_preview"].detail


# ---------------------------------------------------------------------------------------------- one definition of passed

@pytest.mark.parametrize("row,passed", [
    ({"exit_code": 0}, True), ({"exit_code": 1}, False), ({"exit_code": None}, False), ({}, False),
    ({"exit_code": 0, "visual": {"status": "passed"}}, True), ({"exit_code": 0, "visual": {"status": "blocked"}}, False),
])
def test_one_definition_of_a_passing_row(row, passed):
    assert checks.row_passed(row) is passed
    assert DeployObservation("succeeded", e2e_results=[{"command": "x", **row}]).passed is passed


def test_stage_5_reads_its_rows_with_the_same_definition(tmp_path, root, examples):
    """A dev e2e row that exited 0 without the browser evidence it owes is a failure against dev, as it is before merge;
    a journey entry reaches Stage 5 as the controller renders it."""
    ctx = _ctx(tmp_path, root, InMemoryQueue())
    _seed_merged(ctx, examples)
    spec = {**examples["spec_smart_retries"], "test_plan": {**examples["spec_smart_retries"]["test_plan"], "e2e": [JOURNEY]}}
    seen = []

    def run_e2e(url, entries):
        seen.extend(entries)
        return [{"command": e["command"], "target": "preview", "covers_ac": e["covers_ac"], "exit_code": 0,
                 "visual": {"status": "blocked"}} for e in entries]
    obs = observe_once(ctx, spec, SHA, "main", ScriptedSource(["success"]), run_e2e, poll_seconds=0, sleep=lambda s: None)
    assert [e["command"] for e in seen] == ["factory:journey add-model"]
    assert not obs.passed and [e.kind for e in ctx.store.events(RUN)][-1] == "e2e.dev_failed"
    assert obs.environment_url == "https://dev.ledgerline.internal"
