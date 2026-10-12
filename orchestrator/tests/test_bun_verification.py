"""Bun test evidence in managed verification.

Bun reports come from Bun 1.3.14 itself (tests/fixtures, captured on a synthetic two-file suite). The harness is run
here only as a test double of the hosted sandbox: `bun` on PATH is a fake process that copies a fixture report to the
path the harness asked for, so nothing but tiny scripts executes; the controller is the real code, under the guard that
nothing of the repository runs on the orchestrator host. An optional test runs the real Bun on a tiny fixture when it
is installed."""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import support.hosted as hosted

from factory import checks
from factory.hosted_verifier import _test_evidence
from factory.sandbox_tools import environment_runner as er
from factory.stages.implement import LocalVerifier, passed_test_ids, run_implement_one
from factory.verification import evidence_from_reports
from support.bun import FAILING_BUN, FILTERED, MODELS, PASSING_BUN, REPORT, BunDriver, bun_spec, env_of
from support.bun import fake_bun  # noqa: F401  (fixture)


sandbox_double = hosted.sandbox_double            # the hosted-sandbox stand-ins from the managed tests
no_host_execution = hosted.no_host_execution


# ---------------------------------------------------------------------------------------------- the report

def test_ids_come_from_the_real_bun_report_structure():
    out = er.parse_bun_junit(REPORT.read_bytes())
    assert out["total"] == 8
    assert out["passed"] == ['other.test.js::name with "quotes" & <angle> chars', f"{MODELS}::top level passes", PASSING_BUN,
                             f"{MODELS}::model catalog > adds one to 1", f"{MODELS}::model catalog > adds one to 2"]
    assert out["failed"] == [FAILING_BUN]
    assert out["skipped"] == [f"{MODELS}::model catalog > frontier entries > skipped case",
                              f"{MODELS}::model catalog > frontier entries > todo case"]


def test_tests_a_name_filter_left_out_are_skipped_never_passed():
    out = er.parse_bun_junit(FILTERED.read_bytes())
    assert out["passed"] == [f"{MODELS}::model catalog > adds one to 1", f"{MODELS}::model catalog > adds one to 2"]
    assert FAILING_BUN in out["skipped"] and PASSING_BUN in out["skipped"] and out["failed"] == []


@pytest.mark.parametrize("mangle", [
    lambda x: x.replace('tests="8"', 'tests="9"', 1),                          # declares more than it lists: truncated
    lambda x: x[: x.index("</testsuites>")],                                    # cut off
    lambda x: x.replace("<testsuites", "<testsuite", 1).replace("</testsuites>", "</testsuite>"),   # not Bun's shape
    lambda x: x.replace(' file="other.test.js" line="2"', ' line="2"').replace('<testsuite name="other.test.js" file="other.test.js"', '<testsuite name="other.test.js"'),
])
def test_a_malformed_or_inconsistent_report_is_no_evidence(mangle):
    assert er.parse_bun_junit(mangle(REPORT.read_text()).encode()) is None


def test_absolute_sandbox_paths_become_repository_relative():
    text = REPORT.read_text().replace('file="src/', 'file="/tmp/factory-test/repo/src/')
    assert PASSING_BUN in er.parse_bun_junit(text.encode(), "/tmp/factory-test/repo")["passed"]


# ---------------------------------------------------------------------------------------------- which commands give evidence

def test_a_direct_bun_test_gets_the_harness_reporter_and_keeps_its_own_arguments():
    executed, reason = er.bun_command("bun test src/catalog -t 'adds one' --timeout 5000", Path("/w/r.xml"))
    assert reason is None
    assert executed == "bun test --reporter=junit --reporter-outfile=/w/r.xml src/catalog -t 'adds one' --timeout 5000"


@pytest.mark.parametrize("text", ["bun run test", "cd web && bun test", "bun test | tee out", "FOO=1 bun test",
                                  "bun test $(git ls-files '*.test.ts')", "npx bun test", "bun test --reporter=dots", "bun test --dots"])
def test_other_bun_shapes_say_why_they_give_no_evidence(text):
    executed, reason = er.bun_command(text, Path("/w/r.xml"))
    assert executed is None and reason


@pytest.mark.parametrize("text", ['"bun" test', "b\\un test", "bun 'test'", 'bun te"st"', "'bun' test src/catalog"])
def test_quoted_or_escaped_bun_test_fails_closed_instead_of_crashing(text):
    executed, reason = er.bun_command(text, Path("/w/r.xml"))
    assert executed is None and reason


def test_an_escaped_space_names_another_executable_and_yields_no_evidence():
    assert er.bun_command("bun\\ test", Path("/w/r.xml")) == (None, None)     # one shell word: a program called "bun test"


def test_a_quoted_bun_command_is_an_unsupported_row_and_the_harness_keeps_going(tmp_path, fake_bun):
    rows = run_commands(tmp_path, ['"bun" test src/catalog', "python -m pytest -q tests/test_a.py"], env_of(fake_bun))
    assert rows[0]["command"] == '"bun" test src/catalog' and rows[0]["tests"]["status"] == "unsupported"
    assert "instrumented" not in rows[0] and "--reporter" not in fake_bun["argv"].read_text()
    assert rows[1]["tests"]["status"] == "reported", "a later command still runs and reports"


def test_commands_without_bun_are_left_alone():
    assert er.bun_command("python -m pytest -q", Path("/w/r.xml")) == (None, None)


# ---------------------------------------------------------------------------------------------- the harness, with a fake bun

def run_commands(tmp_path, commands, env):
    root = tmp_path / "sb"
    repo, logs, evidence, outputs = root / "repo", root / "logs", root / "evidence", root / "outputs"
    for d in (repo, logs, evidence, outputs):
        d.mkdir(parents=True)
    hosted.write(repo, hosted.SEED)
    report = {"status": "blocked", "phases": [], "services": []}
    er.run_change(hosted.RECIPE, {"commands": commands, "e2e": [], "collect_tests": False}, repo, {**os.environ, **env},
                  time.monotonic() + 120, logs, evidence, root, outputs, report)
    return report["change"]["results"]["commands"]


def test_mixed_pytest_and_bun_commands_each_report_their_own_framework(tmp_path, fake_bun):
    rows = run_commands(tmp_path, ["python -m pytest -q tests/test_a.py", "bun test src/catalog"], env_of(fake_bun, FAKE_BUN_EXIT="1"))
    pytest_row, bun_row = rows
    assert pytest_row["tests"]["framework"] == "pytest" and pytest_row["tests"]["passed"] == ["tests/test_a.py::test_a"]
    assert bun_row["command"] == "bun test src/catalog" and bun_row["instrumented"] == "bun-junit"
    assert bun_row["exit_code"] == 1 and bun_row["tests"]["status"] == "reported" and bun_row["tests"]["failed"] == [FAILING_BUN]
    assert fake_bun["argv"].read_text().startswith("test --reporter=junit --reporter-outfile=")


def test_bun_evidence_that_is_missing_invalid_or_unsupported_is_explicit(tmp_path, fake_bun):
    broken = tmp_path / "broken.xml"
    broken.write_text(REPORT.read_text().replace('tests="8"', 'tests="3"', 1))
    missing = run_commands(tmp_path / "a", ["bun test"], env_of(fake_bun, FAKE_BUN_REPORT=""))[0]
    invalid = run_commands(tmp_path / "b", ["bun test"], env_of(fake_bun, FAKE_BUN_REPORT=str(broken)))[0]
    script = run_commands(tmp_path / "c", ["bun run test"], env_of(fake_bun))[0]
    assert missing["exit_code"] == 0 and missing["tests"] == {"framework": "bun", "report": "junit", "status": "missing", "reason": "Bun wrote no JUnit report"}
    assert invalid["tests"]["status"] == "invalid" and "passed" not in invalid["tests"]
    assert script["exit_code"] == 0 and script["tests"]["status"] == "unsupported" and "direct `bun test" in script["tests"]["reason"]


@pytest.mark.skipif(shutil.which("bun") is None, reason="Bun is not installed on this machine")
def test_the_real_bun_runtime_produces_what_the_parser_expects(tmp_path):
    """The pinned runtime on a tiny synthetic file, through the harness's own instrumentation (no app, no network)."""
    version = subprocess.run(["bun", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    root = tmp_path / "sb"
    repo, logs, evidence, outputs = root / "repo", root / "logs", root / "evidence", root / "outputs"
    for d in (repo, logs, evidence, outputs):
        d.mkdir(parents=True)
    hosted.write(repo, {"src/a.test.ts": 'import { describe, expect, test } from "bun:test";\n'
                                     'describe("outer", () => { describe("inner", () => { test("adds up", () => expect(1 + 1).toBe(2)); '
                                     'test.todo("later"); }); });\ntest("fails", () => expect(1).toBe(2));\n'})
    report = {"status": "blocked", "phases": [], "services": []}
    er.run_change(hosted.RECIPE, {"commands": ["bun test src"], "e2e": [], "collect_tests": False}, repo, dict(os.environ),
                  time.monotonic() + 120, logs, evidence, root, outputs, report)
    row = report["change"]["results"]["commands"][0]
    assert row["tests"]["status"] == "reported", (version, row)
    assert row["tests"]["passed"] == ["src/a.test.ts::outer > inner > adds up"]
    assert row["tests"]["failed"] == ["src/a.test.ts::fails"] and row["tests"]["skipped"] == ["src/a.test.ts::outer > inner > later"]


# ---------------------------------------------------------------------------------------------- the controller

def test_the_controller_only_accepts_ids_of_the_reported_framework():
    good = {"framework": "bun", "report": "junit", "status": "reported", "passed": [PASSING_BUN], "failed": [], "skipped": []}
    assert _test_evidence(good)["passed"] == [PASSING_BUN]
    wrong = {**good, "passed": ["tests/test_a.py::test_a"]}
    assert _test_evidence(wrong)["status"] == "invalid"
    assert _test_evidence({"framework": "bun", "status": "unsupported", "reason": "package script"}) == \
        {"framework": "bun", "status": "unsupported", "reason": "package script"}
    assert _test_evidence({"framework": "jest", "status": "reported", "passed": ["x"], "failed": [], "skipped": []}) is None
    assert _test_evidence(None) is None


def test_exit_codes_and_unsupported_rows_are_never_test_evidence():
    rows = [{"command": "bun run test", "exit_code": 0, "tests": {"framework": "bun", "status": "unsupported", "reason": "script"}},
            {"command": "bun test", "exit_code": 0, "tests": {"framework": "bun", "status": "missing", "reason": "no report"}},
            {"command": "echo ok", "exit_code": 0}]
    assert passed_test_ids(rows) is None and evidence_from_reports(rows, None) == (None, None)


def mapping(tests, passing, known, require=True):
    spec = {"acceptance_criteria": [{"ac_id": "AC-1", "verification": "automated_test"}]}
    return checks.ac_test_mapping(spec, {"criteria": [{"ac_id": "AC-1", "tests": tests}]}, passing, known, require_verified=require)


def test_managed_mapping_fails_closed_without_reports_and_local_mode_is_unchanged():
    closed = mapping([PASSING_BUN], None, None)
    assert not closed.passed and "no machine-readable test report" in closed.detail
    assert mapping([PASSING_BUN], None, None, require=False).passed, "local and platform modes keep the UNVERIFIED pass"


def test_bun_describe_and_file_ids_need_every_reported_test_under_them_to_pass():
    out = er.parse_bun_junit(REPORT.read_bytes())
    known = out["passed"] + out["failed"] + out["skipped"]
    assert mapping([PASSING_BUN], out["passed"], known).passed
    assert mapping([f"{MODELS}::model catalog > adds one to 1"], out["passed"], known).passed
    assert not mapping([FAILING_BUN], out["passed"], known).passed
    assert not mapping([f"{MODELS}::model catalog > frontier entries > todo case"], out["passed"], known).passed
    assert not mapping([f"{MODELS}::model catalog > frontier entries"], out["passed"], known).passed, "a failed and a skipped test inside"
    assert mapping(["other.test.js"], out["passed"], known).passed
    assert not mapping([f"{MODELS}::model catalog > no such test"], out["passed"], known).passed
    assert "not found" in mapping([f"{MODELS}::model catalog > no such test"], out["passed"], known).detail


# ---------------------------------------------------------------------------------------------- a managed round, end to end


def managed_round(tmp_path, root, fake, bun_tests, commands=None, **env):
    origin = hosted.remote(tmp_path)
    driver = BunDriver(origin, tmp_path, bun_tests=bun_tests, extra_env=env_of(fake, **{"FAKE_BUN_EXIT": "1", **env}))
    ctx = hosted.context(tmp_path, root, driver)
    verdict = run_implement_one(ctx, bun_spec(commands), str(origin), "main", LocalVerifier(str(origin), tmp_path / "runs" / hosted.RUN_ID / "verify"), {})
    det = {d["check"]: d for d in hosted.artifact(ctx, "implement", "spec_hosted-r1-deterministic_checks.json")}
    return verdict, det, hosted.artifact(ctx, "implement", "spec_hosted-r1-test_results.json")


def test_a_passing_bun_test_proves_its_criterion_in_a_managed_round(tmp_path, root, fake_bun, no_host_execution):
    _, det, results = managed_round(tmp_path, root, fake_bun, [PASSING_BUN])
    assert det["ac_test_mapping"]["passed"], det["ac_test_mapping"]["detail"]
    assert det["ac_test_mapping"]["detail"] == "all automated criteria map to tests the orchestrator collected and saw pass"
    bun_row = results["commands"][1]
    assert bun_row["command"] == "bun test src/catalog" and bun_row["tests"]["framework"] == "bun"
    assert bun_row["location"] == "hosted-sandbox" and "Failed tests:" in bun_row["stderr_tail"]
    assert not det["test_plan_commands"]["passed"], "the fixture suite's own failing test still fails the command check"


def test_a_failing_skipped_or_unlisted_bun_test_fails_its_criterion(tmp_path, root, fake_bun, no_host_execution):
    _, det, _ = managed_round(tmp_path, root, fake_bun, [FAILING_BUN, f"{MODELS}::model catalog > frontier entries > todo case",
                                                          f"{MODELS}::invented by the implementer"])
    detail = det["ac_test_mapping"]["detail"]
    assert not det["ac_test_mapping"]["passed"]
    assert "did not pass" in detail and FAILING_BUN in detail and "invented by the implementer" in detail


def test_an_exit_zero_package_script_is_not_evidence_for_a_bun_criterion(tmp_path, root, fake_bun, no_host_execution):
    _, det, results = managed_round(tmp_path, root, fake_bun, [PASSING_BUN], commands=["python -m pytest -q tests/test_a.py", "bun run test"],
                                    FAKE_BUN_EXIT="0")
    assert results["commands"][1]["exit_code"] == 0 and results["commands"][1]["tests"]["status"] == "unsupported"
    assert det["test_plan_commands"]["passed"] and not det["ac_test_mapping"]["passed"]
    assert "No bun test evidence" in results["commands"][1]["stderr_tail"]
