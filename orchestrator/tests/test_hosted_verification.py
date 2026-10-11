"""Preview mode "managed": every candidate head is verified in a hosted sandbox built from the app's approved recipe.

No network, no app, no browser: the fake session driver plays the hosted sandbox by running the real harness
(`environment_runner.run_change`) on the uploaded archive with trivial commands, `health` stubbed (nothing listens),
and a test double standing in for the in-sandbox browser command. The controller side is the real code: the recipe
pinning, the binding of results to head/recipe/archive, visual collection, the deterministic checks, and the guarantee
that nothing of the repository executes on the orchestrator host."""
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_config

from factory import cli, security
from factory import test_environments as te
from factory.config import AgentRef, managed_gaps
from factory.gates import InMemoryQueue, LogNotifier
from factory.hosted_verifier import ENVIRONMENT_CHECK, SETUP_CHECK, HostedVerifier
from factory.sandbox_tools import environment_runner as er
from factory.stages import RunContext, implement
from factory.stages.implement import LocalVerifier, run_implement_one
from factory.state import Policy, WorkItemStage
from factory.store import RunStore

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/k9sAAAAASUVORK5CYII="
VERIFY_WRITES = ("python -c \"import json,os,pathlib; pathlib.Path(os.environ['FACTORY_EVIDENCE_DIR'],'verification-result.json')"
                 ".write_text(json.dumps({'status':'passed','checks':[{'id':'smoke','status':'passed','observation':'ok'}]}))\"")
RECIPE = {"schema_version": 1, "name": "hosted-test", "runtime": "linux",
          "commands": {"prepare": "true", "start": "true", "seed": "true", "verify": VERIFY_WRITES, "stop": "true"},
          "services": [{"id": "app", "port": 8080, "health_path": "/health"}], "secrets": [],
          "data": {"kind": "synthetic", "authentication": "none-required"},
          "network": {"allowed_hosts": [], "allow_package_managers": False}, "timeout_seconds": 300,
          "verification": {"kind": "api", "checks": [{"id": "smoke", "description": "the app answers"}]}}
MANIFEST = {"service": "hosted-app", "runtime": {"kind": "python", "entrypoint": "python -m app", "port": 8080},
            "health": {"path": "/health"}, "env": [], "test_environment": {"profile": "ops/recipe.json"}}
# Stands in for a browser command inside the sandbox: answers the controller's request with per-step screenshots.
SHOT = f"""import base64, json, os, pathlib
req = json.loads(pathlib.Path(os.environ["FACTORY_VISUAL_REQUEST"]).read_text())
out = pathlib.Path(os.environ["FACTORY_EVIDENCE_DIR"])
for i, _ in enumerate(req["steps"], 1):
    (out / f"step{{i}}.png").write_bytes(base64.b64decode("{PNG}"))
seen = json.loads(os.environ.get("SHOT_VIEWPORT") or "null") or req["viewport"]
(out / "result.json").write_text(json.dumps({{"request_id": req["id"], "head_sha": req["head_sha"], "status": "passed",
    "runner": "sandbox browser (test double)", "summary": "Greeting visible", "viewport_observed": seen,
    "steps": [{{"status": "passed", "observation": s}} for s in req["steps"]],
    "media": [{{"file": f"step{{i}}.png", "role": "screenshot", "caption": s}} for i, s in enumerate(req["steps"], 1)]}}))
"""
VISUAL = {"persona": "Visitor", "viewport": {"width": 1024, "height": 700}, "steps": ["Open the page", "Click Show greeting"],
          "environment": {"name": "Hosted recipe", "data": "synthetic", "authentication": "none-required"}}
REPORT = {"criteria": [{"ac_id": "AC-1", "tests": ["tests/test_a.py::test_a"]}], "head_commit": "x"}
RUN_ID = "run_hosted00000001"


def spec(e2e_command="python e2e_shot.py") -> dict:
    return {"spec_id": "spec_hosted", "work_item_id": "wi-hosted", "title": "hosted", "allowed_paths": ["app/**", "tests/**"],
            "acceptance_criteria": [
                {"ac_id": "AC-1", "user_visible": False, "verification": "automated_test", "given": "g", "when": "w", "then": "t"},
                {"ac_id": "AC-2", "user_visible": True, "surface": "ui", "verification": "manual", "given": "g", "when": "w", "then": "t"}],
            "_markdown": "# spec",
            "test_plan": {"commands": ["python -m pytest -q tests/test_a.py"],
                          "e2e": [{"command": e2e_command, "target": "preview", "covers_ac": ["AC-2"], "description": "Shows the greeting",
                                   "visual": json.loads(json.dumps(VISUAL))}]}}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout


def write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content)


SEED = {"README.md": "# app\n", "tests/test_a.py": "def test_a():\n    assert True\n", "e2e_shot.py": SHOT,
        "service_manifest.json": json.dumps(MANIFEST), "ops/recipe.json": json.dumps(RECIPE)}


@pytest.fixture(autouse=True)
def sandbox_double(monkeypatch):
    """The harness runs here only as a test double of the sandbox: no login shell, and nothing to health-check."""
    monkeypatch.setattr(er, "SHELL", ["/bin/bash", "-c"])
    monkeypatch.setattr(er, "health", lambda services, deadline: [{"service": s["id"], "status": "passed"} for s in services])


@pytest.fixture
def no_host_execution(monkeypatch):
    """Managed mode must never run the repository on the controller."""
    def refuse(*a, **k):
        raise AssertionError("controller-side execution in managed mode")
    for name in ("prepare_environment", "run_commands", "run_e2e", "collect_test_ids", "_run_in_group"):
        monkeypatch.setattr(LocalVerifier, name, refuse)
    monkeypatch.setattr(implement, "LocalPreview", refuse)
    monkeypatch.setattr(security, "dependency_audit", refuse)
    monkeypatch.setattr(security, "sast", refuse)


class FakeClient:
    def __init__(self):
        self.archived, self.created, self.metadata = [], [], []
        self.beta = SimpleNamespace(environments=SimpleNamespace(create=self._create, retrieve=self._retrieve, list=self._list),
                                    sessions=SimpleNamespace(archive=lambda sid, timeout=None: self.archived.append(sid)))

    def _create(self, name, config, metadata, timeout=None):
        self.created.append(config)
        self.metadata.append(metadata)
        return SimpleNamespace(id="env_hosted")

    def _env(self, env_id):
        config = self.created[-1]
        return SimpleNamespace(id=env_id, archived_at=None, created_at="2026-10-10T00:00:00Z", metadata=self.metadata[-1],
                               config=SimpleNamespace(model_dump=lambda exclude_none=True: config))

    def _retrieve(self, env_id, timeout=None):
        return self._env(env_id)

    def _list(self, limit=None, include_archived=False, timeout=None):
        return SimpleNamespace(data=[self._env("env_hosted")] if self.created else [], has_next_page=lambda: False)


def result(outputs: dict[str, bytes], cost=0.4):
    return SimpleNamespace(session_id=f"sesn_{len(outputs)}", agent_id="agent_x", agent_version=1, outcome_result="satisfied", outcome_iterations=1,
                           list_cost_usd=cost, stop_reason="end_turn", events_seen=1, custom_tool_calls=[], outputs=outputs)


def hosted_session(kw: dict, scratch: Path, extra_env=None, tamper=None):
    """The sandbox, played by the real harness on the uploaded archive."""
    request = json.loads(kw["uploads"]["environment-request.json"])
    root = scratch / f"sandbox-{len(list(scratch.glob('sandbox-*')))}"
    repo, logs, evidence, outputs = root / "repo", root / "logs", root / "evidence", root / "outputs"
    for d in (repo, logs, evidence, outputs):
        d.mkdir(parents=True)
    (root / "source.tar").write_bytes(kw["uploads"]["test-source.tar"])
    with tarfile.open(root / "source.tar") as archive:
        archive.extractall(repo, filter="data")
    profile = json.loads((repo / request["profile_path"]).read_text())
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}", "BASE_URL": "http://127.0.0.1:8080",
           "FACTORY_EVIDENCE_DIR": str(evidence), "FACTORY_TEST_HEAD": request["head_sha"], **(extra_env or {})}
    report = {**request, "status": "blocked", "phases": [], "services": []}
    er.run_change(profile, request["change"], repo, env, time.monotonic() + 300, logs, evidence, root, outputs, report)
    report["phases"].append({"phase": "stop", **er.command(profile["commands"]["stop"], repo, env, 30, logs / "stop.log")})
    report.pop("_uploaded_bytes", None)
    if tamper:
        tamper(report)
    files = {p.name: p.read_bytes() for p in outputs.iterdir()}
    return result({**files, "environment-result.json": json.dumps(report).encode()})


class Driver:
    def __init__(self, origin: Path, scratch: Path, change_files=None, extra_env=None, tamper=None):
        self.origin, self.scratch, self.calls, self.client = origin, scratch, [], FakeClient()
        self.change_files = change_files or {"app/main.py": "X = 1\n"}
        self.extra_env, self.tamper = extra_env, tamper

    def run(self, **kw):
        self.calls.append(kw)
        if kw.get("stage") == "verify":
            return hosted_session(kw, self.scratch, self.extra_env, self.tamper)
        key = kw["agent_key"]
        if key == "implementer":
            push(self.origin, self.scratch, f"factory/{RUN_ID}/spec_hosted", self.change_files, len(self.calls))
            outputs = {"implementation_report.json": json.dumps(REPORT).encode()}
            if "findings.json" in kw["uploads"]:
                answered = json.loads(kw["uploads"]["findings.json"])["findings"]
                outputs["review_response.json"] = json.dumps({"responses": [{"finding_id": f["finding_id"], "disposition": "fixed",
                                                                            "explanation": "done"} for f in answered]}).encode()
            return result(outputs)
        if key in ("reviewer", "security_reviewer"):
            return result({"findings.json": json.dumps({"findings": [], "checks_performed": [], "spec_conformance": []}).encode()})
        if key == "merge_judge":
            return result({"merge_readiness.json": json.dumps({"result": "ready", "summary_for_human": "fine", "criteria": []}).encode()})
        raise AssertionError(f"unexpected agent {key}")


def remote(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    write(seed, SEED)
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "init")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "origin", "main")
    return origin


def push(origin: Path, scratch: Path, branch: str, files: dict[str, str], n: int) -> None:
    w = scratch / f"work{n}"
    subprocess.run(["git", "clone", "-q", str(origin), str(w)], check=True)
    git(w, "checkout", "-q", "-b", branch, "origin/main")
    write(w, files)
    git(w, "add", "-A")
    git(w, "commit", "-q", "--allow-empty", "-m", f"round {n}")
    git(w, "push", "-q", "-f", "origin", branch)


def context(tmp_path, root, driver, **cfg):
    runs = tmp_path / "runs"
    config = make_config(root, runs, **{"preview_mode": "managed", "app_id": "app_test", "test_vault_ids": ("vlt_test",), **cfg})
    ctx = RunContext(cfg=config, store=RunStore(runs), driver=driver, queue=InMemoryQueue(), notifier=LogNotifier(),
                     forge=None, policy=Policy(review_max_rounds=1), run_id=RUN_ID)
    ctx.event("run.created", {"budget_usd": 50, "outcome_id": "o", "title": "t", "policy": {"preview_mode": "managed"}})
    ctx.event("artifact.produced", {"artifact_type": "decomposition", "revision": 1, "work_items": [{"work_item_id": "wi-hosted", "depends_on": []}]}, stage="decompose")
    for st in (WorkItemStage.SPEC_DRAFTING, WorkItemStage.SPEC_JUDGING, WorkItemStage.SPEC_APPROVED):
        ctx.event("work_item.stage", {"stage": st.value}, work_item_id="wi-hosted")
    return ctx


def artifact(ctx, stage, name):
    return json.loads(ctx.store.artifact_path(RUN_ID, stage, name).read_text())


def candidate(tmp_path, files):
    """A checkout with the seed as base and `files` committed on top: (repo, base_sha, head_sha)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, SEED)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD").strip()
    write(repo, files)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", "change")
    return repo, base, git(repo, "rev-parse", "HEAD").strip()


# ---------------------------------------------------------------------------------------------- the whole loop

def test_managed_round_runs_everything_in_the_sandbox_and_passes_on_bound_evidence(tmp_path, root, no_host_execution):
    origin = remote(tmp_path)
    driver = Driver(origin, tmp_path)
    ctx = context(tmp_path, root, driver, agents={"security_reviewer": AgentRef("agent_sec", 1)})
    verdict = run_implement_one(ctx, spec(), str(origin), "main", LocalVerifier(str(origin), tmp_path / "runs" / RUN_ID / "verify"), {})
    assert verdict["result"] == "ready"

    verify = [kw for kw in driver.calls if kw.get("stage") == "verify"]
    assert len(verify) == 1
    v = verify[0]
    assert v["environment_id"] == "env_hosted" and v["vault_ids"] == ["vlt_test"] and v.get("repository") is None
    assert v["system_override"] == te.SYSTEM and v["budget_usd"] == 4.0
    assert set(v["uploads"]) == {"test-source.tar", "environment-request.json", "tools/environment_runner.py"}
    assert driver.client.archived, "the hosted session is archived after use"

    results = artifact(ctx, "implement", "spec_hosted-r1-test_results.json")
    env = results["environment"]
    assert env["mode"] == "managed" and env["host"] == "anthropic-cloud" and env["app_id"] == "app_test" and env["bound"]
    assert env["profile_path"] == "ops/recipe.json" and env["profile_sha256"] == te.fingerprint(RECIPE)
    assert results["commands"][0]["exit_code"] == 0 and results["commands"][0]["location"] == "hosted-sandbox"
    assert results["commands"][0]["tests"]["passed"] == ["tests/test_a.py::test_a"]
    assert "tests/test_a.py::test_a" in results["collected_tests"]
    row = results["e2e"][0]
    assert row["exit_code"] == 0 and row["visual"]["status"] == "passed" and row["visual"]["head_sha"] == env["head_sha"]
    assert results["preview"]["status"] == "hosted_sandbox" and results["preview"]["url"] is None

    det = {d["check"]: d["passed"] for d in artifact(ctx, "implement", "spec_hosted-r1-deterministic_checks.json")}
    assert det[SETUP_CHECK] and det[ENVIRONMENT_CHECK] and det["visual_e2e"] and det["e2e_preview"] and det["ac_test_mapping"]

    kinds = [e.kind for e in ctx.store.events(RUN_ID)]
    assert "verification.hosted" in kinds and "visual.verified" in kinds
    hosted = next(e for e in ctx.store.events(RUN_ID) if e.kind == "verification.hosted").payload
    assert hosted["status"] == "passed" and hosted["profile_sha256"] == te.fingerprint(RECIPE)

    review = next(kw for kw in driver.calls if kw["agent_key"] == "reviewer")
    assert json.loads(review["uploads"]["preview.json"])["url"] is None
    assert "No preview URL is reachable from this sandbox" in review["description"]
    sec = next(kw for kw in driver.calls if kw["agent_key"] == "security_reviewer")
    pre = json.loads(sec["uploads"]["security_prechecks.json"])
    assert pre["dependency_audit"]["status"] == "skipped" and "managed" in pre["sast"]["reason"]
    judge = next(kw for kw in driver.calls if kw["agent_key"] == "merge_judge")
    assert 'preview mode "managed"' in judge["description"]
    implementer = next(kw for kw in driver.calls if kw["agent_key"] == "implementer")
    assert "Anthropic-hosted test sandbox" in implementer["description"]


def test_wrong_viewport_blocks_the_ui_criterion_even_though_the_baseline_passed(tmp_path, root, no_host_execution):
    origin = remote(tmp_path)
    driver = Driver(origin, tmp_path, extra_env={"SHOT_VIEWPORT": json.dumps({"width": 1742, "height": 1427})})
    ctx = context(tmp_path, root, driver)
    verdict = run_implement_one(ctx, spec(), str(origin), "main", LocalVerifier(str(origin), tmp_path / "runs" / RUN_ID / "verify"), {})
    assert verdict["result"] != "ready"
    det = {d["check"]: d for d in artifact(ctx, "implement", "spec_hosted-r1-deterministic_checks.json")}
    assert det[ENVIRONMENT_CHECK]["passed"], "the app ran and the baseline passed"
    assert not det["visual_e2e"]["passed"], "a baseline smoke never stands in for the spec's browser criterion"
    row = artifact(ctx, "implement", "spec_hosted-r1-test_results.json")["e2e"][0]
    assert row["visual"]["status"] == "blocked" and "viewport" in row["visual"]["summary"]


# ---------------------------------------------------------------------------------------------- fail closed, before or after the session

def run_verifier(tmp_path, root, files, driver=None, the_spec=None, **cfg):
    repo, base, head = candidate(tmp_path, files)
    driver = driver or Driver(tmp_path / "unused", tmp_path)
    ctx = context(tmp_path, root, driver, **cfg)
    run = HostedVerifier(ctx, tmp_path / "verify").verify(repo, base, head, the_spec or spec(), branch="b", round_no=1, work_item_id="wi-hosted")
    return run, driver, ctx


def test_a_change_to_the_recipe_blocks_verification_without_a_session(tmp_path, root):
    widened = {**RECIPE, "network": {"allowed_hosts": ["api.example.com"], "allow_package_managers": True}}
    run, driver, _ = run_verifier(tmp_path, root, {"ops/recipe.json": json.dumps(widened)})
    assert not driver.calls
    assert not run.setup.passed and "differs from the approved recipe" in run.setup.detail
    assert all(c["exit_code"] is None for c in run.commands) and run.e2e[0]["exit_code"] is None
    assert run.e2e[0]["visual"]["status"] == "blocked" and run.collected is None


def test_the_configured_approved_fingerprint_wins_over_the_base_recipe(tmp_path, root):
    run, driver, _ = run_verifier(tmp_path, root, {"app/main.py": "X = 1\n"}, test_profile_sha256="0" * 64)
    assert not driver.calls and not run.setup.passed and "approved recipe" in run.setup.detail


def test_required_test_credentials_need_an_explicit_test_vault(tmp_path, root):
    secret = {**RECIPE, "network": {"allowed_hosts": ["api.example.com"], "allow_package_managers": False},
              "secrets": [{"name": "TEST_KEY", "required": True, "allowed_hosts": ["api.example.com"], "injection": "header"}]}
    repo, base, head = candidate(tmp_path, {})
    write(repo, {"ops/recipe.json": json.dumps(secret)})
    git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "recipe with a secret")
    head = git(repo, "rev-parse", "HEAD").strip()
    driver = Driver(tmp_path / "unused", tmp_path)
    ctx = context(tmp_path, root, driver, test_vault_ids=(), test_profile_sha256=te.fingerprint(secret))
    run = HostedVerifier(ctx, tmp_path / "verify").verify(repo, base, head, spec(), branch="b", round_no=1)
    assert not driver.calls and "no test vault is attached" in run.setup.detail


def test_a_result_bound_to_another_head_counts_as_nothing_run(tmp_path, root):
    driver = Driver(tmp_path / "unused", tmp_path, tamper=lambda report: report.update(head_sha="f" * 40))
    run, _, ctx = run_verifier(tmp_path, root, {"app/main.py": "X = 1\n"}, driver=driver)
    assert len(driver.calls) == 1 and not run.setup.passed and not run.environment.passed
    assert all(c["exit_code"] is None for c in run.commands) and run.collected is None
    assert run.e2e[0]["exit_code"] is None and run.e2e[0]["visual"]["status"] == "blocked"
    assert json.loads(ctx.store.artifact_path(RUN_ID, "implement", "spec_hosted-r1-hosted_verification.json").read_text())["bound"] is False


def test_the_attached_local_browser_worker_is_refused_in_the_sandbox(tmp_path, root):
    run, driver, _ = run_verifier(tmp_path, root, {"app/main.py": "X = 1\n"}, the_spec=spec("factory:browser"))
    assert driver.calls, "the commands still run"
    assert run.commands[0]["exit_code"] == 0
    assert run.e2e[0]["exit_code"] is None and "cannot run in a hosted sandbox" in run.e2e[0]["visual"]["summary"]
    request = json.loads(driver.calls[0]["uploads"]["environment-request.json"])
    assert request["change"]["e2e"] == []


def test_visual_requests_stay_invisible_to_local_workers(tmp_path, root):
    from factory import visual
    run, _, _ = run_verifier(tmp_path, root, {"app/main.py": "X = 1\n"})
    assert run.e2e[0]["visual"]["status"] == "passed"
    assert visual.pending(tmp_path) == [] and list((tmp_path / "verify" / "hosted" / "visual").glob("*/closed"))


@pytest.mark.parametrize("target", ["local", "preview"])
def test_both_e2e_targets_execute_inside_the_hosted_sandbox(tmp_path, root, target):
    contract = spec()
    contract["test_plan"]["e2e"][0]["target"] = target
    run, _, _ = run_verifier(tmp_path, root, {}, the_spec=contract)
    assert run.e2e[0]["target"] == target
    assert run.e2e[0]["exit_code"] == 0 and run.e2e[0]["visual"]["status"] == "passed"


def test_wrong_spec_digest_invalidates_same_head_evidence(tmp_path, root):
    driver = Driver(tmp_path / "unused", tmp_path, tamper=lambda report: report.update(spec_sha256="f" * 64))
    run, _, _ = run_verifier(tmp_path, root, {}, driver=driver)
    assert not run.setup.passed and run.commands[0]["exit_code"] is None
    assert run.e2e[0]["visual"]["status"] == "blocked"


def test_unknown_target_is_visible_and_blocked(tmp_path, root):
    contract = spec()
    contract["test_plan"]["e2e"][0]["target"] = "unknown"
    run, _, _ = run_verifier(tmp_path, root, {}, the_spec=contract)
    assert len(run.e2e) == 1 and run.e2e[0]["exit_code"] is None
    assert "unsupported e2e target" in run.e2e[0]["visual"]["summary"]


def test_an_expired_deadline_never_starts_another_command(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("expired command executed"))
    result = er.command("true", tmp_path, {}, -1, tmp_path / "expired.log")
    assert result["status"] == "not_run"


# ---------------------------------------------------------------------------------------------- the harness itself

def harness(tmp_path, recipe, change):
    root = tmp_path / "sb"
    repo, logs, evidence, outputs = root / "repo", root / "logs", root / "evidence", root / "outputs"
    for d in (repo, logs, evidence, outputs):
        d.mkdir(parents=True)
    write(repo, SEED)
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}", "FACTORY_EVIDENCE_DIR": str(evidence)}
    report = {"status": "blocked", "phases": [], "services": []}
    er.run_change(recipe, change, repo, env, time.monotonic() + 120, logs, evidence, root, outputs, report)
    return report, outputs


def test_harness_runs_nothing_after_a_failed_prepare(tmp_path):
    recipe = {**RECIPE, "commands": {**RECIPE["commands"], "prepare": "exit 3"}}
    report, _ = harness(tmp_path, recipe, {"commands": ["true"], "e2e": [{"index": 0, "command": "true"}], "collect_tests": True})
    out = report["change"]["results"]
    assert report["status"] == "failed" and out["commands"][0]["status"] == "not_run" and out["e2e"][0]["status"] == "not_run"


def test_harness_reports_exit_codes_junit_outcomes_and_collection_without_raw_output(tmp_path):
    printing = "python -c \"print('printed-' + 'by-the-command')\"; exit 4"       # the output text is not in the command text
    report, _ = harness(tmp_path, RECIPE, {"commands": ["python -m pytest -q tests/test_a.py", printing], "e2e": [], "collect_tests": True})
    out = report["change"]["results"]
    assert out["commands"][0]["exit_code"] == 0 and out["commands"][0]["tests"]["passed"] == ["tests/test_a.py::test_a"]
    assert out["commands"][1]["exit_code"] == 4 and "printed-by-the-command" not in json.dumps(report)
    assert "tests/test_a.py::test_a" in out["collected_tests"]["node_ids"] and out["baseline"]["status"] == "passed"


def test_harness_gives_each_e2e_entry_its_own_evidence_and_never_uploads_odd_files(tmp_path):
    request = {"id": "r" * 32, "head_sha": "a" * 40, "viewport": VISUAL["viewport"], "steps": VISUAL["steps"]}
    odd = "python e2e_shot.py && ln -s /etc/hosts \"$FACTORY_EVIDENCE_DIR/link.png\" && echo x > \"$FACTORY_EVIDENCE_DIR/page.html\""
    _, outputs = harness(tmp_path, RECIPE, {"commands": [], "collect_tests": False,
                                                 "e2e": [{"index": 0, "command": odd, "visual_request": request}]})
    names = sorted(p.name for p in outputs.iterdir())
    assert names == ["e2e-0-result.json", "e2e-0-step1.png", "e2e-0-step2.png"]
    assert json.loads((outputs / "e2e-0-result.json").read_text())["request_id"] == "r" * 32


# ---------------------------------------------------------------------------------------------- configuration and resume

def test_managed_configuration_is_validated(tmp_path, root):
    cfg = make_config(root, tmp_path, preview_mode="managed", managed_verify_budget_usd=float("nan"), test_profile_sha256="abc",
                      vault_id="vlt_forge", test_vault_ids=("vlt_forge",))
    gaps = " ".join(managed_gaps(cfg))
    assert "FACTORY_MANAGED_VERIFY_BUDGET_USD" in gaps and "FACTORY_TEST_PROFILE_SHA256" in gaps and "forge vault" in gaps
    assert managed_gaps(make_config(root, tmp_path, preview_mode="hosted")) == ["FACTORY_PREVIEW='hosted' (expected platform|local|managed)"]
    assert managed_gaps(make_config(root, tmp_path, preview_mode="managed", test_vault_ids=("vlt_a", "vlt_b"))) == []


def test_cli_preview_choice_overrides_the_environment(tmp_path, root):
    cfg = cli.apply_preview_choice(make_config(root, tmp_path), "Managed", "app_x", "A" * 64, ["vlt_a", " vlt_b "], 2.5)
    assert (cfg.preview_mode, cfg.app_id, cfg.test_profile_sha256, cfg.test_vault_ids, cfg.managed_verify_budget_usd) == \
           ("managed", "app_x", "a" * 64, ("vlt_a", "vlt_b"), 2.5)


def test_a_managed_run_never_resumes_with_controller_execution(tmp_path, root, no_host_execution):
    origin = remote(tmp_path)
    driver = Driver(origin, tmp_path)
    ctx = context(tmp_path, root, driver)
    import dataclasses
    ctx = dataclasses.replace(ctx, cfg=make_config(root, tmp_path / "runs", preview_mode="local"))
    with pytest.raises(RuntimeError, match="FACTORY_PREVIEW=managed"):
        run_implement_one(ctx, spec(), str(origin), "main", LocalVerifier(str(origin), tmp_path / "runs" / RUN_ID / "verify"), {})
    assert not driver.calls


def test_managed_prechecks_execute_nothing(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess on the controller")))
    pre = implement.managed_prechecks({"app/x.py": ["token = 'ghp_" + "a" * 36 + "'"]})
    assert pre["secret_scan"]["hits"] and pre["dependency_audit"]["status"] == "skipped" and pre["sast"]["status"] == "skipped"


def test_spec_commands_run_in_the_virtualenv_the_recipe_built(tmp_path):
    """Live emoji-smith: prepare ran `uv sync` into .venv, but the spec's bare `pytest` and `ruff` resolved to the
    sandbox's own tools without the app's dependencies, so every importing module failed collection (exit 2)."""
    build = ("mkdir -p .venv/bin && ln -s \"$(command -v python)\" .venv/bin/python && "
             "printf '#!/bin/sh\\nexit 0\\n' > .venv/bin/venv-only-tool && chmod +x .venv/bin/venv-only-tool")
    recipe = {**RECIPE, "commands": {**RECIPE["commands"], "prepare": build}}
    report, _ = harness(tmp_path, recipe, {"commands": ["venv-only-tool", 'test "$VIRTUAL_ENV" = "$PWD/.venv"'],
                                           "e2e": [{"index": 0, "command": "venv-only-tool"}], "collect_tests": False})
    out = report["change"]["results"]
    assert [c["exit_code"] for c in out["commands"]] == [0, 0] and out["e2e"][0]["exit_code"] == 0
    assert out["command_environment"] == {"virtualenv": ".venv"}
    report, _ = harness(tmp_path / "plain", RECIPE, {"commands": ["venv-only-tool"], "e2e": [], "collect_tests": False})
    assert report["change"]["results"]["commands"][0]["exit_code"] == 127
    assert report["change"]["results"]["command_environment"] == {"virtualenv": None}
