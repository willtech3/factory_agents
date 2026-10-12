"""The managed-verification test bed: a seed repository and its approved recipe, a fake session driver that plays the hosted sandbox by running the real harness (environment_runner.run_change) on the uploaded archive, and the guards that nothing of the repository runs on the orchestrator host. The controller side under test is the real code."""
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
from support.sessions import stage_result

from factory import security
from factory.gates import InMemoryQueue, LogNotifier
from factory.hosted_verifier import HostedVerifier
from factory.sandbox_tools import environment_runner as er
from factory.stages import RunContext, implement
from factory.stages.implement import LocalVerifier
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
    return stage_result(outputs, sid=f"sesn_{len(outputs)}", agent_id="agent_x", cost=cost)

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

def run_verifier(tmp_path, root, files, driver=None, the_spec=None, **cfg):
    repo, base, head = candidate(tmp_path, files)
    driver = driver or Driver(tmp_path / "unused", tmp_path)
    ctx = context(tmp_path, root, driver, **cfg)
    run = HostedVerifier(ctx, tmp_path / "verify").verify(repo, base, head, the_spec or spec(), branch="b", round_no=1, work_item_id="wi-hosted")
    return run, driver, ctx
