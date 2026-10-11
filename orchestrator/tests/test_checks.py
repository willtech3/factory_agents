from factory import checks


def test_glob_matching():
    allowed = ["app/services/billing/**", "db/migrate/*_create_payment_attempts.rb", "spec/models/payment_attempt_spec.rb"]
    assert checks.path_allowed("app/services/billing/stripe_webhook_handler.rb", allowed)
    assert checks.path_allowed("app/services/billing/deep/nested/file.rb", allowed)
    assert checks.path_allowed("db/migrate/20261009_create_payment_attempts.rb", allowed)
    assert not checks.path_allowed("db/migrate/20261009_alter_payments.rb", allowed)
    assert not checks.path_allowed("app/controllers/webhooks/stripe_controller.rb", allowed)
    assert not checks.path_allowed("spec/models/account_spec.rb", allowed)


def test_diff_scope_reports_outside_files():
    r = checks.diff_scope(["app/models/payment_attempt.rb", "config/routes.rb"], ["app/models/**"])
    assert not r.passed
    assert r.data["outside"] == ["config/routes.rb"]


def test_protected_tests_blocks_existing_test_edits():
    r = checks.protected_tests(
        modified_files=["spec/models/account_spec.rb", "app/models/account.rb"],
        deleted_files=[],
        existing_test_files=["spec/models/account_spec.rb"],
        allowed_paths=["app/models/**", "spec/models/**"],
    )
    assert not r.passed and "account_spec" in r.detail


def test_test_weakening_detects_skip_markers():
    r = checks.test_weakening({"spec/x_spec.rb": ["  it 'works' do", "  xit 'later' do"], "tests/test_y.py": ["@pytest.mark.skip(reason='flaky')"]})
    assert not r.passed and len(r.data["hits"]) == 2


def test_dag_acyclic(examples):
    assert checks.dag_acyclic(examples["decomposition"]["work_items"]).passed
    cyclic = [{"work_item_id": "a", "depends_on": ["b"]}, {"work_item_id": "b", "depends_on": ["a"]}]
    r = checks.dag_acyclic(cyclic)
    assert not r.passed and "cycle" in r.detail


def test_metric_coverage(examples):
    assert checks.metric_coverage(examples["outcome"], examples["decomposition"]).passed
    outcome = dict(examples["outcome"])
    outcome["success_metrics"] = outcome["success_metrics"] + [{"metric": "NPS", "target": "+10"}]
    r = checks.metric_coverage(outcome, examples["decomposition"])
    assert not r.passed and "NPS" in r.detail


def test_spec_traceability(examples):
    assert checks.spec_traceability(examples["spec_smart_retries"], examples["decomposition"]).passed
    bad = dict(examples["spec_smart_retries"], work_item_id="wi-nope")
    assert not checks.spec_traceability(bad, examples["decomposition"]).passed


def test_ac_test_mapping(examples):
    spec = examples["spec_smart_retries"]
    report = {"criteria": [{"ac_id": ac["ac_id"], "tests": [f"t_{ac['ac_id']}"]} for ac in spec["acceptance_criteria"]
                                    if ac["verification"] == "automated_test"]}
    passing = [f"t_{ac['ac_id']}" for ac in spec["acceptance_criteria"]]
    assert checks.ac_test_mapping(spec, report, passing).passed
    r = checks.ac_test_mapping(spec, report, passing[:-1])  # one test missing from the passing set
    assert not r.passed


def test_commands_passed():
    assert checks.commands_passed([{"command": "x", "exit_code": 0}]).passed
    assert not checks.commands_passed([{"command": "x", "exit_code": 0}, {"command": "y", "exit_code": 1}]).passed


# ---- Stage 5 / platform-contract checks

def test_manifest_valid_accepts_example_and_rejects_missing(root, schemas_dir, tmp_path):
    assert checks.manifest_valid(root / "examples" / "ledgerline" / "service_manifest.json", schemas_dir).passed
    missing = checks.manifest_valid(tmp_path / "service_manifest.json", schemas_dir)
    assert not missing.passed and "missing" in missing.detail
    bad = tmp_path / "service_manifest.json"
    bad.write_text('{"service": "x", "runtime": {"kind": "container"}, "env": []}')  # no health
    r = checks.manifest_valid(bad, schemas_dir)
    assert not r.passed and "health" in r.detail
    bad.write_text("{not json")
    assert not checks.manifest_valid(bad, schemas_dir).passed


def test_e2e_coverage(examples):
    spec = examples["spec_smart_retries"]
    assert checks.e2e_coverage(spec).passed
    import copy
    uncovered = copy.deepcopy(spec)
    uncovered["acceptance_criteria"][1]["user_visible"] = True  # AC-2 has no e2e entry
    r = checks.e2e_coverage(uncovered)
    assert not r.passed and r.data["missing"] == ["AC-2"]
    dangling = copy.deepcopy(spec)
    dangling["test_plan"]["e2e"][0]["covers_ac"].append("AC-99")
    r = checks.e2e_coverage(dangling)
    assert not r.passed and r.data["dangling"] == ["AC-99"]


def test_e2e_passed_treats_not_run_as_failure():
    assert checks.e2e_passed([{"command": "a", "target": "preview", "exit_code": 0}]).passed
    r = checks.e2e_passed([{"command": "a", "target": "preview", "exit_code": None}])
    assert not r.passed and "not run" in r.detail
    r = checks.e2e_passed([{"command": "a", "target": "preview", "exit_code": 1},
                           {"command": "b", "target": "local", "exit_code": 1}])  # local ignored for preview
    assert not r.passed and r.data["failed"] == ["a"]


def test_run_all_for_merge_includes_e2e_and_manifest(examples, root, schemas_dir):
    spec = examples["spec_smart_retries"]
    report = {"criteria": [{"ac_id": ac["ac_id"], "tests": [f"t_{ac['ac_id']}"]} for ac in spec["acceptance_criteria"]]}
    passing = [f"t_{ac['ac_id']}" for ac in spec["acceptance_criteria"]]
    common = dict(spec=spec, changed_files=["app/models/payment_attempt.rb"], deleted_files=[], existing_test_files=[],
                  added_lines_by_file={}, implementation_report=report, passing_tests=passing,
                  command_results=[{"command": "x", "exit_code": 0}])
    # No e2e results supplied: the preview check is present and failing (not run != passed).
    names = {c.check: c for c in checks.run_all_for_merge(**common)}
    assert names["e2e_coverage"].passed and not names["e2e_preview"].passed and "manifest_valid" not in names
    # Results supplied and manifest present: everything green.
    e2e = [{"command": e["command"], "target": "preview", "exit_code": 0} for e in spec["test_plan"]["e2e"] if e["target"] == "preview"]
    names = {c.check: c for c in checks.run_all_for_merge(
        **common, e2e_results=e2e, manifest_path=root / "examples" / "ledgerline" / "service_manifest.json", schema_dir=schemas_dir)}
    assert all(c.passed for c in names.values()), [c.detail for c in names.values() if not c.passed]
    assert "manifest_valid" in names and "e2e_preview" in names


def test_ac_test_mapping_says_when_the_report_is_not_in_the_schema_shape():
    from factory.checks import ac_test_mapping

    spec = {"acceptance_criteria": [{"ac_id": "AC-1", "verification": "automated_test"}]}
    r = ac_test_mapping(spec, {"acceptance_criteria": {"AC-1": {"pytest_node_ids": ["tests/test_x.py::test_a"]}}},
                        ["tests/test_x.py::test_a"])
    assert not r.passed and "no `criteria: [{ac_id, tests}]` list" in r.detail and "AC-1: no test mapped" in r.detail


def test_ac_test_mapping_reads_the_report_shape_the_implementer_writes():
    from factory.checks import ac_test_mapping, criteria_mapping

    spec = {"acceptance_criteria": [{"ac_id": "AC-1", "verification": "automated_test"}, {"ac_id": "AC-2", "verification": "manual_check"},
                                    {"ac_id": "AC-3", "verification": "automated_test"}]}
    report = {"criteria": [{"ac_id": "AC-1", "tests": ["tests/test_x.py::test_a"], "status": "met"}, {"ac_id": "AC-3", "tests": ["tests/test_x.py::test_c"]}]}
    assert criteria_mapping(report) == {"AC-1": ["tests/test_x.py::test_a"], "AC-3": ["tests/test_x.py::test_c"]}
    ok = ac_test_mapping(spec, report, ["tests/test_x.py::test_a", "tests/test_x.py::test_c"])
    assert ok.passed, ok.detail
    bad = ac_test_mapping(spec, {"criteria": [{"ac_id": "AC-1", "tests": []}]}, [])
    assert not bad.passed and "AC-1: no test mapped" in bad.detail and "AC-3: no test mapped" in bad.detail


def test_failed_deterministic_checks_become_blocking_findings(schemas_dir):
    from factory.checks import CheckResult, schema_valid
    from factory.stages.implement import deterministic_findings

    det = [CheckResult("diff_scope", True, "ok"), CheckResult("test_plan_commands", False, "failed: ['make e2e']"),
           CheckResult("ac_test_mapping", False, "AC-1: no test mapped")]
    cmds = [{"command": "make e2e", "exit_code": 2, "stderr_tail": "port in use"}]
    fs = deterministic_findings(det, cmds)
    assert [f["finding_id"] for f in fs] == ["DET-test_plan_commands", "DET-ac_test_mapping"]
    assert all(f["severity"] == "blocking" and f["status"] == "open" for f in fs)
    assert "port in use" in fs[0]["evidence"]["reproduction"]
    doc = {"review_id": "rev_x", "spec_id": "spec_x", "round": 1, "diff_ref": {"base_sha": "a", "head_sha": "b", "branch": "c"},
           "checks_performed": [], "findings": fs, "spec_conformance": [], "produced_by": {}}
    r = schema_valid(doc, schemas_dir / "review_findings.schema.json", schemas_dir)
    assert r.passed or "produced_by" in r.detail, r.detail       # findings themselves must be schema-shaped
