"""The orchestrator's own pytest run is the source of the passing-test set (post-mortem issue 5): node ids come from
the junit report it asks pytest for, mapped ids are checked against what pytest collects, and a repository pytest
cannot collect leaves the mapping unverified rather than verified. Runs real pytest in a subprocess, no network."""
import sys
from pathlib import Path

from factory import checks
from factory.checks import CheckResult
from factory.stages.implement import SETUP_CHECK, LocalVerifier, parse_junit, passed_test_ids

TESTS = '''import pytest

def test_one():
    assert 1

class TestK:
    def test_two(self):
        assert 1

@pytest.mark.parametrize("x", [1, 2])
def test_p(x):
    assert x

def test_three():
    assert 0
'''


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (repo / "tests" / "test_a.py").write_text(TESTS)
    return repo


def _verifier(tmp_path: Path) -> LocalVerifier:
    """A verifier whose per-run environment is the orchestrator's own venv (it has pytest; nothing is installed).

    The venv is linked in at the production layout (workdir/venv), not used in place: the shell shim lands next to
    the per-run environment, and pointing env_dir at the venv itself put it in the orchestrator checkout."""
    v = LocalVerifier("unused", tmp_path / "v")
    v.env_dir.parent.mkdir(parents=True)
    v.env_dir.symlink_to(Path(sys.executable).parent.parent, target_is_directory=True)
    v.setup = CheckResult(SETUP_CHECK, True, "the test's own interpreter stands in for the per-run environment")
    return v


def test_run_commands_reports_pytest_node_ids_from_its_own_junit(tmp_path):
    repo = _repo(tmp_path)
    v = _verifier(tmp_path)
    out = v.run_commands(repo, ["python -m pytest -q tests", "echo not-a-test-command"], timeout=120)
    assert out[0]["exit_code"] == 1 and "pytest" not in out[1]
    assert out[0]["tests"]["passed"] == ["tests/test_a.py::test_one", "tests/test_a.py::TestK::test_two",
                                          "tests/test_a.py::test_p[1]", "tests/test_a.py::test_p[2]"]
    assert out[0]["tests"]["failed"] == ["tests/test_a.py::test_three"]
    passed, failed = passed_test_ids(out)
    assert "tests/test_a.py::TestK::test_two" in passed and failed == ["tests/test_a.py::test_three"]
    assert passed_test_ids([out[1]]) is None                               # no pytest ran: nothing to verify with
    # a compound command still gets the report (PYTEST_ADDOPTS reaches the pytest inside it)
    out2 = v.run_commands(repo, ["true && python -m pytest -q tests/test_a.py::test_one"], timeout=120)
    assert out2[0]["exit_code"] == 0 and out2[0]["tests"]["passed"] == ["tests/test_a.py::test_one"]


def test_collect_test_ids_lists_what_pytest_knows_and_none_elsewhere(tmp_path):
    repo = _repo(tmp_path)
    v = _verifier(tmp_path)
    assert v.collect_test_ids(repo) == ["tests/test_a.py::test_one", "tests/test_a.py::TestK::test_two",
                                        "tests/test_a.py::test_p[1]", "tests/test_a.py::test_p[2]", "tests/test_a.py::test_three"]
    empty = tmp_path / "empty"
    empty.mkdir()
    assert v.collect_test_ids(empty) == []                                 # pytest runs, collects nothing: a (verified) empty set
    broken = tmp_path / "broken"
    (broken / "tests").mkdir(parents=True)
    (broken / "tests" / "test_b.py").write_text("import module_that_does_not_exist\n")
    assert v.collect_test_ids(broken) is None                              # collection error: unverifiable
    unprepared = LocalVerifier("unused", tmp_path / "v2")
    unprepared.setup = CheckResult(SETUP_CHECK, False, "install failed")
    assert unprepared.collect_test_ids(repo) is None                        # no environment: nothing is verifiable


def test_collect_test_ids_survives_a_verbose_addopts(tmp_path):
    """`addopts = -v` cancels `--collect-only -q` and pytest prints a tree with no node ids; the ids still come back
    (the hosted harness hit this on emoji-smith; the local verifier read an empty set as "every mapped test is missing")."""
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "-v"\n')
    assert _verifier(tmp_path).collect_test_ids(repo) == [
        "tests/test_a.py::test_one", "tests/test_a.py::TestK::test_two",
        "tests/test_a.py::test_p[1]", "tests/test_a.py::test_p[2]", "tests/test_a.py::test_three"]


def test_parse_junit_rebuilds_node_ids_from_file_classname_and_name():
    xml = b'''<testsuites><testsuite><testcase classname="tests.test_a" name="test_one" file="tests/test_a.py"/>
    <testcase classname="tests.test_a.TestK" name="test_two" file="tests/test_a.py"/>
    <testcase classname="test_a.TestK" name="test_three" file="tests/test_a.py"><failure message="x"/></testcase>
    <testcase classname="tests.test_a" name="test_four" file="tests/test_a.py"><skipped/></testcase>
    <testcase classname="pkg.tests.test_c" name="test_five[a-b]"/></testsuite></testsuites>'''
    r = parse_junit(xml)
    assert r["passed"] == ["tests/test_a.py::test_one", "tests/test_a.py::TestK::test_two", "pkg/tests/test_c.py::test_five[a-b]"]
    assert r["failed"] == ["tests/test_a.py::TestK::test_three"] and r["skipped"] == ["tests/test_a.py::test_four"]
    assert parse_junit(b"not xml") == {"passed": [], "failed": [], "skipped": []}


def _spec():
    return {"acceptance_criteria": [{"ac_id": "AC-1", "verification": "automated_test"},
                                    {"ac_id": "AC-2", "verification": "automated_test"},
                                    {"ac_id": "AC-3", "verification": "manual"}]}


def test_ac_test_mapping_only_counts_tests_the_orchestrator_collected_and_saw_pass():
    collected = ["tests/test_a.py::test_one", "tests/test_a.py::TestK::test_two", "tests/test_a.py::test_three"]
    passed = ["tests/test_a.py::test_one", "tests/test_a.py::TestK::test_two"]
    ok = {"criteria": [{"ac_id": "AC-1", "tests": ["tests/test_a.py::test_one"]}, {"ac_id": "AC-2", "tests": ["tests/test_a.py::TestK"]}]}
    r = checks.ac_test_mapping(_spec(), ok, passed, collected)
    assert r.passed and r.data["verified"] and "saw pass" in r.detail      # a class id counts when everything under it passed
    fabricated = {"criteria": [{"ac_id": "AC-1", "tests": ["tests/does_not_exist.py::nope"]}, {"ac_id": "AC-2", "tests": ["tests/test_a.py::test_one"]}]}
    r = checks.ac_test_mapping(_spec(), fabricated, passed, collected)
    assert not r.passed and "not found" in r.detail and "does_not_exist" in r.detail
    failing = {"criteria": [{"ac_id": "AC-1", "tests": ["tests/test_a.py::test_three"]}, {"ac_id": "AC-2", "tests": ["tests/test_a.py"]}]}
    r = checks.ac_test_mapping(_spec(), failing, passed, collected)
    assert not r.passed and "did not pass" in r.detail and "AC-2" in r.detail   # the file id fails: test_three under it failed
    # the old hole: the report's own list as the passing set is gone; an unverifiable repository says so
    r = checks.ac_test_mapping(_spec(), fabricated, None)
    assert r.passed and not r.data["verified"] and "UNVERIFIED" in r.detail
    assert not checks.ac_test_mapping(_spec(), {"criteria": [{"ac_id": "AC-1", "tests": []}]}, None).passed   # presence still required
