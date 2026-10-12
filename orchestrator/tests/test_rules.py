"""ADR-0015: the rule registry. Every check the orchestrator runs has a rule and every check rule names a check that
runs; the prompts and the rubric hold the blocks the registry renders; the judges' decision rules are post-conditions in
code; agent outputs are validated on receipt. No network."""
import copy
import dataclasses
import json
from pathlib import Path

import pytest
from support import artifacts
from support.sessions import AutoQueue, run_after_gate_1, stage_result
from test_implement_bookkeeping import TEST_ID, Driver, _ctx, _spec

from factory import checks, rules, verification
from factory.checks import CheckResult
from factory.config import REPO_ROOT
from factory.gates import Decision
from factory.hosted_verifier import ENVIRONMENT_CHECK
from factory.local_verifier import SETUP_CHECK
from factory.stages.implement import deterministic_findings, report_schema_check, run_implement_one
from factory.stages.specify import run_specify

URL = "https://github.com/w/app"


def _checks_that_run(examples, root, schemas_dir) -> set[str]:
    """The name of every check the pipeline produces, from the functions that produce them."""
    spec = verification.normalized(examples["spec_smart_retries"])
    bundle = verification.EvidenceBundle("hosted", "h" * 40, CheckResult(SETUP_CHECK, True), [], [], {}, {},
                                         environment=CheckResult(ENVIRONMENT_CHECK, True))
    diff = {"changed": [], "deleted": [], "existing_tests": [], "added_lines": {}, "removed_lines": {}}
    merge = verification.judge(bundle, spec, diff=diff, report={"criteria": []}, schema_dir=schemas_dir, reported_head="h" * 40,
                               manifest_path=root / "examples" / "ledgerline" / "service_manifest.json",
                               size_budget_lines={"XS": 50, "S": 150, "M": 400})
    ctx = type("Ctx", (), {"cfg": type("Cfg", (), {"schemas_dir": schemas_dir})()})()
    decomposition, outcome = examples["decomposition"], examples["outcome"]
    design = [checks.dag_acyclic(decomposition["work_items"]), checks.metric_coverage(outcome, decomposition),
              checks.decomposition_size(decomposition, {"XS": 50, "S": 150, "M": 400}),
              checks.schema_valid(spec, schemas_dir / "spec.schema.json", schemas_dir), checks.spec_traceability(spec, decomposition),
              checks.spec_size_estimate(spec, {"XS": 50, "S": 150, "M": 400}), checks.criteria_describe_product(spec),
              checks.e2e_coverage(spec), report_schema_check(ctx, {}, None)]
    return {c.check.split(":")[0] for c in merge + design}


def test_every_check_has_a_rule_and_every_check_rule_a_check(examples, root, schemas_dir):
    ran = _checks_that_run(examples, root, schemas_dir)
    assert ran - set(rules.BY_CHECK) == set(), "a check no rule states: add it to factory/rules.py"
    assert set(rules.BY_CHECK) - ran == set(), "a check rule whose check never runs"
    assert all(r.checks for r in rules.RULES if r.enforced_by == rules.CHECK)
    assert not any(r.checks for r in rules.RULES if r.enforced_by != rules.CHECK)
    assert len({r.id for r in rules.RULES}) == len(rules.RULES) and all(r.roles for r in rules.RULES)


def test_the_prompts_and_the_rubric_hold_what_the_registry_renders():
    """`factory rules --check` in CI terms: edit factory/rules.py, run `factory rules`, commit the result."""
    assert rules.drift(REPO_ROOT) == []
    implementer = (REPO_ROOT / "agents" / "implementer.md").read_text()
    for r in rules.for_role("implementer"):
        assert f"**`{r.id}`**" in implementer
    assert "**`spec-pass`**" not in implementer and "**`spec-pass`**" in (REPO_ROOT / "agents" / "spec_judge.md").read_text()
    rubric = (REPO_ROOT / "rubrics" / "implementation.md").read_text()
    marker = rubric.index("<!-- grader copy ends here")
    assert rubric.index(rules.BEGIN) > marker, "the deterministic items stay below the grader marker"


def test_a_block_is_spliced_in_place_and_appended_once(tmp_path):
    text = "# Agent\n\nbody\n"
    once = rules.splice(text, "first")
    assert once.endswith(f"{rules.BEGIN}\nfirst\n{rules.END}\n") and rules.splice(once, "first") == once
    assert rules.splice(once, "second").count(rules.BEGIN) == 1 and "second" in rules.splice(once, "second")


def test_a_failed_check_cites_its_rule_and_the_registrys_fix():
    det = [CheckResult("diff_scope", False, "outside: x.py"), CheckResult(SETUP_CHECK, True)]
    [f] = deterministic_findings(det)
    assert f["violates"][0].startswith("rule allowed-paths:") and f["suggested_fix"] == rules.BY_ID["allowed-paths"].fix
    [hosted] = deterministic_findings([CheckResult(SETUP_CHECK, False, "recipe")], managed=True)
    assert hosted["suggested_fix"] == rules.BY_ID["environment-builds"].hosted_fix and hosted["category"] == "test_quality"


# ---------------------------------------------------------------------- judges' decision rules

def _verdict(result="pass", score=2, severity="minor"):
    return {"overall": {"result": result, "blocking_issue_count": 0, "summary_for_human": "ok"},
            "per_spec": [{"spec_id": "s1", "criteria": [{"criterion": "S1", "score": score, "evidence": "e"}],
                          "issues": [{"issue_id": "I-1", "severity": severity}]}], "coherence": {"gaps": []}}


def test_spec_pass_needs_no_zero_and_no_blocking_issue():
    assert rules.spec_pass_violations(_verdict()) == []
    assert rules.spec_pass_violations(_verdict(score=0)) == ["s1: rubric criterion 'S1' scored 0"]
    assert rules.spec_pass_violations(_verdict(severity="blocking")) == ["s1: blocking issue I-1"]
    assert rules.spec_pass_violations(_verdict("revise", score=0)) == []


def test_merge_ready_needs_passing_checks_and_every_criterion_met():
    spec = {"acceptance_criteria": [{"ac_id": "AC-1"}, {"ac_id": "AC-2"}]}
    met = artifacts.merge_readiness({"spec_id": "s", **spec})
    assert rules.merge_ready_violations(met, spec, [CheckResult("diff_scope", True)]) == []
    assert rules.merge_ready_violations(met, spec, [CheckResult("diff_scope", False)]) == ["deterministic check diff_scope failed"]
    partial = {**met, "criteria": [{"ac_id": "AC-1", "status": "met", "evidence": "e"}, {"ac_id": "AC-2", "status": "not_assessed", "evidence": "e"}]}
    assert rules.merge_ready_violations(partial, spec, []) == ["AC-2 is not_assessed, not met"]
    assert rules.merge_ready_violations({**met, "criteria": []}, spec, [])[0] == "AC-1 is missing from the criteria table, not met"
    assert rules.merge_ready_violations({**met, "result": "another_round", "criteria": []}, spec, []) == []


def test_a_spec_judge_pass_with_a_zero_is_revised_by_the_stage(tmp_path, root, examples):
    decomposition = examples["decomposition"]
    item = decomposition["work_items"][0]
    spec = dict(examples["spec_smart_retries"], spec_id=f"spec_{item['work_item_id'].removeprefix('wi-')}", work_item_id=item["work_item_id"])

    def run(**kw):
        if kw["agent_key"] == "spec_writer":
            return stage_result({"spec.json": json.dumps(spec).encode(), "spec.md": b"# spec"})
        verdict = {"verdict_id": "ver_fake0001", "decomposition_id": decomposition["decomposition_id"], "round": 1,
                   "overall": {"result": "pass", "blocking_issue_count": 0, "summary_for_human": "ok"},
                   "per_spec": [{"spec_id": spec["spec_id"], "criteria": [{"criterion": "S2", "score": 0, "evidence": "AC-3"}], "issues": []}],
                   "coherence": {"interface_mismatches": [], "overlaps": [], "gaps": [], "ordering_problems": [], "contradictions": []},
                   "goal_coverage": []}
        return stage_result({"verdict.json": json.dumps(verdict).encode()})
    ctx = run_after_gate_1(tmp_path, root, examples, type("D", (), {"run": staticmethod(run)})(), AutoQueue(Decision.APPROVE))
    ctx = dataclasses.replace(ctx, policy=dataclasses.replace(ctx.policy, spec_judge_max_rounds=1))
    _, verdict = run_specify(ctx, examples["outcome"], dict(decomposition, work_items=[item]))
    assert verdict["overall"]["result"] == "revise" and verdict["overall"]["summary_for_human"].startswith("Overridden (rule spec-pass)")


class Judging(Driver):
    """The bookkeeping harness, with a merge judge that says ready while one criterion is not met."""

    def run(self, **kw):
        res = super().run(**kw)
        if kw["agent_key"] == "merge_judge":
            v = json.loads(res.outputs["merge_readiness.json"])
            v["criteria"][0]["status"] = "not_met"
            res.outputs["merge_readiness.json"] = json.dumps(v).encode()
        return res


def test_a_ready_verdict_with_an_unmet_criterion_is_another_round(tmp_path, root, examples):
    ctx, verifier = _ctx(tmp_path, root, examples, mapping=[TEST_ID])
    ctx.driver = Judging(ctx, examples, [TEST_ID])
    verdict = run_implement_one(ctx, _spec(examples), URL, "main", verifier, {})
    assert verdict["result"] == "another_round" and "Overridden (rule merge-ready): AC-1 is not_met" in verdict["summary_for_human"]


# ---------------------------------------------------------------------- validated on receipt

class Invalid(Driver):
    """The bookkeeping harness, with one agent's output broken."""

    def __init__(self, ctx, examples, mapping, broken):
        super().__init__(ctx, examples, mapping)
        self.broken = broken

    def run(self, **kw):
        res = super().run(**kw)
        if kw["agent_key"] == self.broken:
            name = next(iter(res.outputs))
            res.outputs[name] = json.dumps({**json.loads(res.outputs[name]), "unexpected": True}).encode()
        return res


def test_an_invalid_review_is_refused_and_its_session_is_not_reused(tmp_path, root, examples):
    ctx, verifier = _ctx(tmp_path, root, examples, mapping=[TEST_ID])
    ctx.driver = Invalid(ctx, examples, [TEST_ID], "reviewer")
    with pytest.raises(RuntimeError, match="rule artifact-schema"):
        run_implement_one(ctx, _spec(examples), URL, "main", verifier, {})
    [review] = [s for s in ctx.store.steps(ctx.run_id) if s.key.startswith("session/review/")]
    assert review.status == "failed" and "unexpected" in review.error


def test_an_invalid_implementation_report_is_a_failed_check_the_next_round_answers(tmp_path, root, examples):
    ctx, verifier = _ctx(tmp_path, root, examples, mapping=[TEST_ID])
    ctx.driver = Invalid(ctx, examples, [TEST_ID], "implementer")
    run_implement_one(ctx, _spec(examples), URL, "main", verifier, {})
    det = {d["check"]: d for d in ctx.store.get_json(ctx.run_id, "implement", f"{_spec(examples)['spec_id']}-r1-deterministic_checks.json")}
    assert not det["report_schema"]["passed"] and "unexpected" in det["report_schema"]["detail"]
    second = [c for c in ctx.driver.calls if c["agent_key"] == "implementer"][1]
    ids = [f["finding_id"] for f in json.loads(second["uploads"]["findings.json"])["findings"]]
    assert "DET-report_schema" in ids


def test_the_examples_are_what_the_schemas_say(examples, schemas_dir):
    """The fixtures every fake starts from validate: a fake that passes a test with an invalid artefact proves nothing."""
    for name, schema in (("implementation_report", "implementation_report"), ("findings_round1", "review_findings"),
                         ("merge_readiness", "merge_readiness"), ("review_response_round1", "review_response"),
                         ("verdict_round2", "judge_verdict")):
        assert checks.schema_errors(copy.deepcopy(examples[name]), schemas_dir / f"{schema}.schema.json", schemas_dir) == [], name


def test_the_rules_command_reports_drift(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from factory import cli
    from factory import config as cfgmod
    for rel in ["agents", "rubrics"]:
        (tmp_path / rel).mkdir()
    for path in rules.generated_files(REPO_ROOT):
        (tmp_path / path.relative_to(REPO_ROOT)).write_text(path.read_text())
    monkeypatch.setattr(cfgmod, "REPO_ROOT", tmp_path)
    assert CliRunner().invoke(cli.app, ["rules", "--check"]).exit_code == 0
    implementer = tmp_path / "agents" / "implementer.md"
    implementer.write_text(implementer.read_text().replace("Never skip a test", "Rarely skip a test"))
    out = CliRunner().invoke(cli.app, ["rules", "--check"])
    assert out.exit_code == 1 and "agents/implementer.md" in out.output
    assert "rendered agents/implementer.md" in CliRunner().invoke(cli.app, ["rules"]).output
    assert CliRunner().invoke(cli.app, ["rules", "--check"]).exit_code == 0
    assert Path(implementer).read_text().count(rules.BEGIN) == 1
