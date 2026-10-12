"""Hosted verification repairs found by the first live Evergreen run.

1. The app's canonical origin: browser checks use http://localhost:<port> (what the app and its recipe call it), never
   a different origin for the same socket; readiness probes stay on 127.0.0.1.
2. Next.js builds trust the sandbox's existing system CA roots (Turbopack's documented switch), with TLS still verified.
3. An approved spec that names an existing test file exactly may extend that file; every other existing test stays protected.
4. A Bun-only plan does not run pytest collection and says so, without changing how Bun evidence is judged.
5. A completed hosted verification session is charged to the run exactly once, whatever its result.

Offline: the hosted sandbox is the real harness run on scratch files (health stubbed, nothing listens), git is local."""
import json
import time

import pytest
import test_bun_verification as tbv
import test_hosted_verification as hv

from factory import checks
from factory.hosted_verifier import _runtime, pytest_collection
from factory.sandbox_tools import environment_runner as er
from factory.stages import common
from factory.stages.implement import LocalVerifier, _diff_lines, run_implement_one

sandbox_double = hv.sandbox_double
no_host_execution = hv.no_host_execution
fake_bun = tbv.fake_bun
REQUEST = {"head_sha": "a" * 40, "profile_path": "ops/recipe.json"}


# ---------------------------------------------------------------------------------------------- 1. the app's origin

def test_the_app_is_reached_at_localhost_and_probed_at_127(tmp_path, monkeypatch):
    assert er.app_base_url({"id": "app", "port": 3000}) == "http://localhost:3000"
    env, runtime = er.sandbox_env({}, hv.RECIPE, REQUEST, tmp_path, tmp_path / "evidence")
    assert env["BASE_URL"] == "http://localhost:8080" and runtime["base_url"] == env["BASE_URL"] and runtime["health_host"] == "127.0.0.1"
    monkeypatch.undo()                                             # the real health(), not the autouse stand-in
    seen = []

    class Answer:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(er.urllib.request, "urlopen", lambda url, timeout=None: seen.append(url) or Answer())
    assert er.health([{"id": "app", "port": 3000, "health_path": "/api/health"}], time.monotonic() + 5)[0]["status"] == "passed"
    assert seen == ["http://127.0.0.1:3000/api/health"]


def test_a_recipe_without_an_app_service_has_no_base_url(tmp_path):
    worker_only = {**hv.RECIPE, "services": [{"id": "worker", "port": 9000, "health_path": "/"}]}
    env, runtime = er.sandbox_env({}, worker_only, REQUEST, tmp_path, tmp_path / "evidence")
    assert env["BASE_URL"] == "" and runtime["base_url"] == ""
    assert er.app_base_url(None) == ""


def test_the_controller_issues_browser_requests_for_the_origin_the_harness_uses(tmp_path, root):
    run, driver, _ = hv.run_verifier(tmp_path, root, {"app/main.py": "X = 1\n"})
    request = json.loads(driver.calls[0]["uploads"]["environment-request.json"])
    visual_request = request["change"]["e2e"][0]["visual_request"]
    assert visual_request["base_url"] == "http://localhost:8080" == er.app_base_url(hv.RECIPE["services"][0])
    assert run.record["app_base_url"] == "http://localhost:8080"
    assert run.e2e[0]["visual"]["base_url"] == "http://localhost:8080", "the evidence names the origin the browser used"


# ---------------------------------------------------------------------------------------------- 2. Next.js system roots

def package(tmp_path, data):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "package.json").write_text(json.dumps(data) if not isinstance(data, str) else data)
    return tmp_path


@pytest.mark.parametrize("deps", [{"dependencies": {"next": "16.1.1"}}, {"devDependencies": {"next": "^16"}}])
def test_a_next_app_trusts_the_existing_system_roots_with_verification_kept(tmp_path, deps):
    repo = package(tmp_path / "repo", deps)
    before = (repo / "package.json").read_bytes()
    env = {"PATH": "/usr/bin"}
    compat = er.runtime_compat(repo, env)
    assert env[er.NEXT_SYSTEM_TLS] == "1" and compat[er.NEXT_SYSTEM_TLS]["applied"] is True
    assert "NODE_TLS_REJECT_UNAUTHORIZED" not in env and "NODE_EXTRA_CA_CERTS" not in env
    assert (repo / "package.json").read_bytes() == before and sorted(p.name for p in repo.iterdir()) == ["package.json"]


def test_other_apps_and_an_explicit_setting_are_left_alone(tmp_path):
    env = {}
    assert er.runtime_compat(package(tmp_path / "a", {"dependencies": {"react": "19"}}), env) == {} and env == {}
    assert er.runtime_compat(package(tmp_path / "b", "{not json"), env) == {} and env == {}
    assert er.runtime_compat(tmp_path / "c", env) == {} and env == {}
    env = {er.NEXT_SYSTEM_TLS: "0"}
    compat = er.runtime_compat(package(tmp_path / "d", {"dependencies": {"next": "16"}}), env)
    assert env[er.NEXT_SYSTEM_TLS] == "0" and compat[er.NEXT_SYSTEM_TLS] == {"value": "0", "applied": False,
                                                                             "reason": "already set by the sandbox environment"}


def test_the_controller_keeps_only_the_known_non_secret_runtime_facts():
    kept = _runtime({"base_url": "http://localhost:3000", "health_host": "127.0.0.1", "token": "x",
                     "compat": {er.NEXT_SYSTEM_TLS: {"value": "1", "applied": True, "reason": "next"}, "API_KEY": {"value": "secret"}}})
    assert kept == {"base_url": "http://localhost:3000", "health_host": "127.0.0.1",
                    "compat": {er.NEXT_SYSTEM_TLS: {"value": "1", "applied": True, "reason": "next"}}}
    assert _runtime("nope") is None


# ---------------------------------------------------------------------------------------------- 3. exact test paths

def test_only_exact_test_paths_are_named():
    assert checks.named_test_paths(["tests/models.test.ts", "tests/**", "src/**", "tests/", "app/models.ts", "tests/a_test.py",
                                    "tests/[ab].test.ts", "/tests/b.test.ts"]) == ["tests/a_test.py", "tests/b.test.ts", "tests/models.test.ts"]


PROVEN = object()


def protected(modified=(), deleted=(), named=(), removed=PROVEN):
    """protected_tests with, by default, removal evidence that proves nothing was removed from any modified file."""
    existing = ["tests/models.test.ts", "tests/other.test.ts"]
    removed = {f: [] for f in modified} if removed is PROVEN else removed
    return checks.protected_tests(modified, deleted, existing, named, removed)


def test_exactly_approved_test_updates_are_allowed_but_deletions_and_other_edits_are_not():
    ok = protected(modified=["tests/models.test.ts"], named=["tests/models.test.ts"])
    assert ok.passed and ok.data["approved_updates"] == ["tests/models.test.ts"] and "updated" in ok.detail
    other = protected(modified=["tests/models.test.ts", "tests/other.test.ts"], named=["tests/models.test.ts"])
    assert not other.passed and other.data["touched"] == ["tests/other.test.ts"], "a glob never unlocks a test"
    shrunk = protected(modified=["tests/models.test.ts"], named=["tests/models.test.ts"], removed={"tests/models.test.ts": ["expect(x)"]})
    assert shrunk.passed and shrunk.data["approved_updates"] == ["tests/models.test.ts"]
    gone = protected(deleted=["tests/models.test.ts"], named=["tests/models.test.ts"])
    assert not gone.passed and "deleted or renamed" in gone.detail
    assert not protected(modified=["tests/models.test.ts"]).passed, "no exact name, no change"


@pytest.mark.parametrize("removed", [None, {}, {"tests/models.test.ts": None}])
def test_a_named_test_stays_protected_when_the_diff_cannot_be_read(removed):
    result = protected(modified=["tests/models.test.ts"], named=["tests/models.test.ts"], removed=removed)
    assert not result.passed and "does not provide readable test changes" in result.detail


def test_a_binary_or_unparsed_diff_proves_nothing():
    _, removed = _diff_lines("diff --git a/tests/models.test.ts b/tests/models.test.ts\nBinary files a/tests/models.test.ts and b/tests/models.test.ts differ")
    assert removed == {}
    _, quoted = _diff_lines('diff --git "a/tests/sp ace.test.ts" "b/tests/sp ace.test.ts"\n--- "a/tests/sp ace.test.ts"\n+++ "b/tests/sp ace.test.ts"\n@@ -1 +1,2 @@\n+new')
    assert quoted == {}
    assert not protected(modified=["tests/models.test.ts"], named=["tests/models.test.ts"], removed=removed).passed


def test_diff_lines_reads_headers_only_before_the_first_hunk():
    diff = "\n".join(["diff --git a/t.ts b/t.ts", "--- a/t.ts", "+++ b/t.ts", "@@ -1 +1,2 @@", "--- not a header", "+++ not a header",
                      "+added", "diff --git a/new.ts b/new.ts", "--- /dev/null", "+++ b/new.ts", "@@ -0,0 +1 @@", "+first",
                      "diff --git a/gone.ts b/gone.ts", "--- a/gone.ts", "+++ /dev/null", "@@ -1 +0,0 @@", "-last"])
    added, removed = _diff_lines(diff)
    assert added == {"t.ts": ["++ not a header", "added"], "new.ts": ["first"]}
    assert removed == {"t.ts": ["-- not a header"], "gone.ts": ["last"]}


def test_analyse_reports_removed_lines_and_counts_a_rename_as_removing_the_old_path(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    hv.git(repo, "init", "-q", "-b", "main")
    hv.write(repo, {"tests/models.test.ts": "a\nb\n", "tests/old.test.ts": "x\n"})
    hv.git(repo, "add", "-A"); hv.git(repo, "commit", "-q", "-m", "base")
    base = hv.git(repo, "rev-parse", "HEAD").strip()
    hv.write(repo, {"tests/models.test.ts": "a\nb\nc\n"})
    hv.git(repo, "mv", "tests/old.test.ts", "tests/renamed.test.ts")
    hv.git(repo, "add", "-A"); hv.git(repo, "commit", "-q", "-m", "change")
    diff = LocalVerifier("unused", tmp_path / "v").analyse(repo, base, hv.git(repo, "rev-parse", "HEAD").strip())
    assert diff["added_lines"]["tests/models.test.ts"] == ["c"] and diff["removed_lines"]["tests/models.test.ts"] == []
    assert "tests/old.test.ts" in diff["deleted"] and "tests/renamed.test.ts" in diff["changed"]


@pytest.mark.parametrize("allowed, passes", [(["app/**", "tests/test_a.py"], True), (["app/**", "tests/**"], False)])
@pytest.mark.parametrize("test_body", ["def test_a():\n    assert 1 == 1\n", "import pytest\n@pytest.mark.skip(reason='skip')\ndef test_a():\n    assert True\n"])
def test_a_managed_round_permits_approved_test_updates_but_still_detects_skips(tmp_path, root, no_host_execution, allowed, passes, test_body):
    origin = hv.remote(tmp_path)
    grown = test_body
    driver = hv.Driver(origin, tmp_path, change_files={"app/main.py": "X = 1\n", "tests/test_a.py": grown})
    ctx = hv.context(tmp_path, root, driver)
    spec = {**hv.spec(), "allowed_paths": allowed}
    run_implement_one(ctx, spec, str(origin), "main", LocalVerifier(str(origin), tmp_path / "runs" / hv.RUN_ID / "verify"), {})
    det = {d["check"]: d for d in hv.artifact(ctx, "implement", "spec_hosted-r1-deterministic_checks.json")}
    assert det["protected_tests"]["passed"] is passes, det["protected_tests"]["detail"]
    assert det["test_weakening"]["passed"] is ("pytest.mark.skip" not in test_body)


# ---------------------------------------------------------------------------------------------- 4. Bun-only collection

def test_pytest_is_collected_unless_every_command_is_bun():
    assert pytest_collection(["bun test src"])["collect_tests"] is False
    assert "Bun's own JUnit reports" in pytest_collection(["bun test src", "bun run test"])["collect_skip_reason"]
    assert pytest_collection(["bun test src", "python -m pytest -q"]) == {"collect_tests": True}
    assert pytest_collection(["make test"]) == {"collect_tests": True}
    assert pytest_collection([]) == {"collect_tests": True}


def test_a_bun_only_round_skips_pytest_collection_and_still_proves_its_criterion(tmp_path, root, fake_bun, no_host_execution):
    origin = hv.remote(tmp_path)
    driver = tbv.BunDriver(origin, tmp_path, bun_tests=[tbv.PASSING_BUN], extra_env=tbv.env_of(fake_bun))
    driver.report = {"criteria": [{"ac_id": "AC-1", "tests": [tbv.PASSING_BUN]}], "head_commit": "x"}
    ctx = hv.context(tmp_path, root, driver)
    spec = tbv.bun_spec(["bun test src/catalog"])
    spec["acceptance_criteria"] = spec["acceptance_criteria"][:1]
    run_implement_one(ctx, spec, str(origin), "main", LocalVerifier(str(origin), tmp_path / "runs" / hv.RUN_ID / "verify"), {})
    results = hv.artifact(ctx, "implement", "spec_hosted-r1-test_results.json")
    assert results["collected_tests"] is None
    assert results["environment"]["collection"] == {"framework": "pytest", "status": "skipped",
                                                    "reason": pytest_collection(["bun test"])["collect_skip_reason"]}
    det = {d["check"]: d for d in hv.artifact(ctx, "implement", "spec_hosted-r1-deterministic_checks.json")}
    assert det["ac_test_mapping"]["passed"] and "UNVERIFIED" not in det["ac_test_mapping"]["detail"]
    assert not list((tmp_path).glob("sandbox-*/logs/collect.log")), "no pytest collection ran"


def test_a_pytest_round_still_records_its_collection(tmp_path, root):
    run, _, _ = hv.run_verifier(tmp_path, root, {"app/main.py": "X = 1\n"})
    assert run.record["collection"]["status"] == "passed" and run.record["collection"]["collected"] >= 1


# ---------------------------------------------------------------------------------------------- 5. what verification cost

def finished(ctx):
    return [e for e in ctx.store.events(hv.RUN_ID) if e.kind == "session.finished"]


def test_a_completed_verification_is_charged_once(tmp_path, root):
    run, _, ctx = hv.run_verifier(tmp_path, root, {"app/main.py": "X = 1\n"})
    events = finished(ctx)
    assert len(events) == 1 and events[0].payload["session_id"] == run.record["session_id"] and events[0].stage == "verify"
    assert events[0].spec_id == "spec_hosted" and ctx.store.state(hv.RUN_ID).list_cost_usd == pytest.approx(run.record["cost_usd"])


def test_a_failed_or_unbound_verification_is_charged_too(tmp_path, root):
    driver = hv.Driver(tmp_path / "unused", tmp_path, tamper=lambda report: report.update(head_sha="f" * 40))
    run, _, ctx = hv.run_verifier(tmp_path, root, {"app/main.py": "X = 1\n"}, driver=driver)
    assert not run.setup.passed and len(finished(ctx)) == 1 and ctx.store.state(hv.RUN_ID).list_cost_usd == pytest.approx(0.4)


def test_the_run_budget_pauses_the_run_before_a_hosted_verification_starts(tmp_path, root):
    """A hosted verification commits its cap like any session: when the run budget cannot cover it, the run pauses at its
    budget before any sandbox session exists (budget.py), rather than spending past constraints.budget_usd."""
    import dataclasses

    from factory.budget import BudgetPaused
    from factory.hosted_verifier import HostedVerifier
    from factory.state import RunStage

    repo, base, head = hv.candidate(tmp_path, {"app/main.py": "X = 1\n"})
    driver = hv.Driver(tmp_path / "unused", tmp_path)
    ctx = hv.context(tmp_path, root, driver)
    for stage in ("decomposing", "gate_1_decomposition", "specifying", "gate_2_spec_readiness", "implementing"):
        ctx.event("stage.entered", {"stage": stage})
    ctx.policy = dataclasses.replace(ctx.policy, run_budget_usd=3.0)          # below the $4 hosted verification cap
    with pytest.raises(BudgetPaused):
        HostedVerifier(ctx, tmp_path / "verify").verify(repo, base, head, hv.spec(), branch="b", round_no=1, work_item_id="wi-hosted")
    assert not driver.calls
    assert ctx.store.state(hv.RUN_ID).stage == RunStage.PAUSED_BUDGET
    paused = next(e.payload for e in ctx.store.events(hv.RUN_ID) if e.kind == "budget.reached")
    assert paused["next"]["stage"] == "verify" and paused["next"]["cap_usd"] == 4.0


def test_generator_of_deleted_tests_is_not_consumed_before_protection():
    result = checks.protected_tests([], iter(['tests/models.test.ts']), ['tests/models.test.ts'],
                                    ['tests/models.test.ts'], {})
    assert not result.passed and result.data['touched'] == ['tests/models.test.ts']
