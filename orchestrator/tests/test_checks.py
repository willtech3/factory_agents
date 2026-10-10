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
        spec_touches_tests=False,
    )
    assert not r.passed and "account_spec" in r.detail


def test_protected_tests_allows_when_spec_is_about_tests():
    r = checks.protected_tests(["spec/models/account_spec.rb"], [], ["spec/models/account_spec.rb"],
                               ["spec/models/account_spec.rb"], spec_touches_tests=True)
    assert r.passed


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
    report = {"criteria_to_tests": [{"ac_id": ac["ac_id"], "tests": [f"t_{ac['ac_id']}"]} for ac in spec["acceptance_criteria"]
                                    if ac["verification"] == "automated_test"]}
    passing = [f"t_{ac['ac_id']}" for ac in spec["acceptance_criteria"]]
    assert checks.ac_test_mapping(spec, report, passing).passed
    r = checks.ac_test_mapping(spec, report, passing[:-1])  # one test missing from the passing set
    assert not r.passed


def test_commands_passed():
    assert checks.commands_passed([{"command": "x", "exit_code": 0}]).passed
    assert not checks.commands_passed([{"command": "x", "exit_code": 0}, {"command": "y", "exit_code": 1}]).passed
