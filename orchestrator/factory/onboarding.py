"""App onboarding: bounded agent proposals, controller-owned setup and evidence.

No application command executes on the Console host. Source is a pinned GitHub
archive; proposed setup is reviewed by digest and only runs in a hosted sandbox.
"""
from __future__ import annotations

import base64
import fcntl
import io
import json
import math
import os
import re
import subprocess
import tarfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import jsonschema
from fastapi import HTTPException

from .config import anthropic_client
from .forge.github import GitHubForge
from .sessions import SessionDriver
from .test_environments import fingerprint, snapshot, validate_profile, run_hosted, ensure_environment, recipe_relative

BUSY = {"discovering", "verifying", "publishing"}
APP_ID = re.compile(r"app_[a-f0-9]{12}")
MAX_SOURCE = 100 * 1024 * 1024


def secret_scope(secret):
    return fingerprint({k: secret[k] for k in ("name", "allowed_hosts", "injection")})


def owner_alive(record):
    try:
        os.kill(int(record.get("owner_pid") or 0), 0)
        return bool(record.get("owner_pid"))
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_path(path: str) -> str:
    p = PurePosixPath(path)
    if (not path or p.is_absolute() or ".." in p.parts or str(p) != path or "\\" in path
            or any(x in {".git", ".ssh", ".aws", "node_modules"} for x in p.parts)
            or (p.name.startswith(".env") and p.name not in {".env.example", ".env.sample", ".env.template"}) or p.suffix.lower() in {".pem", ".key"}):
        raise ValueError("Unsupported file path in onboarding proposal")
    return str(p)


def git(repo: Path, *args):
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args], cwd=repo,
                          capture_output=True, check=True, timeout=60).stdout.decode().strip()


def unpack_source(data: bytes, destination: Path, *, strip_root=False):
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for m in archive.getmembers():
            parts = PurePosixPath(m.name).parts
            name = "/".join(parts[1:]) if strip_root else m.name
            if not name:
                continue
            name = safe_path(name)
            if not (m.isfile() or m.isdir()):
                raise ValueError("Repository links or special files require a compatible source adapter")
            total += m.size
            if total > MAX_SOURCE:
                raise ValueError("Repository is larger than the hosted onboarding limit")
            p = destination / name
            if m.isdir():
                p.mkdir(parents=True, exist_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(archive.extractfile(m).read())
                p.chmod(0o755 if m.mode & 0o111 else 0o644)


def validate_plan(plan: dict, kind: str, repo: Path, schemas: Path, manifest_path: str = "service_manifest.json") -> dict:
    schema = json.loads((schemas / "onboarding_plan.schema.json").read_text())
    if list(jsonschema.Draft202012Validator(schema).iter_errors(plan)):
        raise ValueError("Onboarding agent returned an invalid plan; discovery must be retried")
    paths = set()
    for f in plan["files"]:
        p = safe_path(f["path"])
        if p.startswith(".github/") or p in {"CLAUDE.md", "AGENTS.md"}:
            raise ValueError("Onboarding cannot change CI or agent security instructions")
        if p in paths:
            raise ValueError("Onboarding proposal contains duplicate file paths")
        paths.add(p)
        if kind == "existing" and not (p.startswith(("verification/factory-onboarding/", ".factory/")) or p == "test_environment.json"):
            raise ValueError("Existing-app proposal changes application or policy files outside onboarding scope")
        if len(f["content"].encode()) > 256 * 1024:
            raise ValueError("Onboarding file is too large")
    for key in ("questions", "journeys", "capabilities"):
        if len({r["id"] for r in plan[key]}) != len(plan[key]):
            raise ValueError("Onboarding plan contains duplicate identifiers")
    path = plan["profile_path"]
    if path:
        path = safe_path(path)
        # Verify the recipe the subsequent change run will actually select, rather than a disconnected file
        # that can pass onboarding but can never be used after its setup PR merges.
        manifest_name = safe_path(manifest_path)
        manifest_text = next((f["content"] for f in plan["files"] if f["path"] == manifest_name), None)
        existing_manifest = repo / manifest_name
        if manifest_text is None and existing_manifest.is_file():
            manifest_text = existing_manifest.read_text()
        manifest = json.loads(manifest_text) if manifest_text is not None else {}
        if not isinstance(manifest, dict):
            raise ValueError("The service manifest must be a JSON object")
        selected_path = recipe_relative(manifest)
        if path != selected_path:
            raise ValueError(f"The change runner selects {selected_path}, not {path}. Use that recipe path, or request "
                             "a reviewed manifest change before verification; an unused recipe cannot onboard the app.")
        proposed = next((f["content"] for f in plan["files"] if f["path"] == path), None)
        p = repo / path
        if proposed is None and (not p.is_file() or p.is_symlink()):
            raise ValueError("Proposed test profile does not exist")
        profile = json.loads(proposed if proposed is not None else p.read_text())
        errors = validate_profile(profile, schemas)
        if errors:
            raise ValueError("Test recipe is invalid: " + "; ".join(errors))
        plan = {**plan, "profile": profile}
    elif not any(q["required"] for q in plan["questions"]):
        raise ValueError("An unavailable test environment must explain the required next action")
    return plan


class AppOnboarding:
    def __init__(self, cfg, client=None):
        self.cfg, self._client = cfg, client
        self.root = cfg.runs_dir / "_apps"
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def client(self):
        if self._client is None:
            self._client = anthropic_client(self.cfg)
        return self._client

    def folder(self, app_id):
        if not APP_ID.fullmatch(app_id):
            raise HTTPException(404, "App not found")
        p = self.root / app_id
        if not (p / "app.json").is_file():
            raise HTTPException(404, "App not found")
        return p

    @contextmanager
    def locked(self, app_id):
        p = self.folder(app_id)
        with (p / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield json.loads((p / "app.json").read_text())

    def write(self, record):
        record["updated_at"] = now()
        p = self.root / record["app_id"] / "app.json"
        temp = p.with_suffix(".tmp")
        temp.write_text(json.dumps(record, indent=2))
        temp.replace(p)

    def update(self, app_id, **values):
        with self.locked(app_id) as r:
            r.update(values)
            self.write(r)
        return self.get(app_id)

    def get(self, app_id):
        r = json.loads((self.folder(app_id) / "app.json").read_text())
        r["remaining_usd"] = round(max(0, r["budget_usd"] - r["spent_usd"] - r.get("reserved_usd", 0)), 2)
        declared = {s["name"]: s for s in r.get("plan", {}).get("profile", {}).get("secrets", [])}
        r["credentials"] = [{"name": c["name"], "status": "configured" if c.get("vault_id") and c.get("scope_sha256") == secret_scope(declared[c["name"]]) else "needs_reconnect"}
                            for c in r.get("credentials", []) if c["name"] in declared]
        r["recoverable"] = r["status"] in BUSY and not owner_alive(r)
        return {k: v for k, v in r.items() if k not in {"vault_id", "reservation", "owner_pid", "previous_attempt", "last_proposal"}}

    def list(self):
        return sorted((self.get(p.parent.name) for p in self.root.glob("app_*/app.json")), key=lambda r: r["updated_at"], reverse=True)

    def create(self, *, kind, name, repository_url, description, budget_usd, revision=""):
        from .web.repositories import repository_name
        if self.cfg.forge != "github":
            raise HTTPException(409, "App onboarding currently supports GitHub repositories")
        repository_name(repository_url, self.cfg)
        if kind not in {"existing", "greenfield"} or not name.strip() or not description.strip():
            raise HTTPException(400, "Choose an app type, a name, and a short description")
        if not math.isfinite(budget_usd) or not 0 < budget_usd <= 60:
            raise HTTPException(400, "Set a total onboarding budget above zero and at most $60")
        if revision and (len(revision) > 180 or not re.fullmatch(r"[A-Za-z0-9_.\-/]+", revision)):
            raise HTTPException(400, "Invalid source revision")
        r = {"app_id": "app_" + uuid.uuid4().hex[:12], "name": name[:160], "kind": kind,
             "repository_url": repository_url.removesuffix(".git"), "description": description[:12000],
             "revision": revision, "status": "connected", "phase": "Ready to inspect the app",
             "budget_usd": budget_usd, "spent_usd": 0, "reserved_usd": 0,
             "answers": {}, "credentials": [], "sessions": [], "evidence": [], "created_at": now()}
        (self.root / r["app_id"]).mkdir(mode=0o700)
        self.write(r)
        return self.get(r["app_id"])

    def forge(self, record):
        from .web.repositories import repository_name
        if not self.cfg.forge_token:
            raise ValueError("Connect GitHub in Factory Settings first")
        return GitHubForge(self.cfg.forge_url, self.cfg.forge_token, repository_name(record["repository_url"], self.cfg))

    def source(self, r):
        """Fetch only a pinned committed archive. Never access the user's app checkout."""
        directory = self.folder(r["app_id"]) / ("source-" + uuid.uuid4().hex[:10])
        directory.mkdir()
        if r["kind"] == "existing":
            forge = self.forge(r)
            try:
                meta = forge._get("")
                if meta.get("archived") or meta.get("disabled"):
                    raise ValueError("This repository is archived or disabled")
                ref = r["revision"] or meta["default_branch"]
                commit = forge._get("/commits/" + quote(ref, safe=""))["sha"]
                with forge.http.stream("GET", forge._r("/tarball/" + commit)) as response:
                    response.raise_for_status()
                    data = bytearray()
                    for part in response.iter_bytes():
                        data.extend(part)
                        if len(data) > MAX_SOURCE:
                            raise ValueError("Repository archive exceeds the onboarding size limit")
                unpack_source(bytes(data), directory, strip_root=True)
                default_branch = meta["default_branch"]
            finally:
                forge.http.close()
        else:
            (directory / "README.md").write_text("# " + r["name"] + "\n\n" + r["description"] + "\n")
            commit, default_branch = "greenfield", "main"
        git(directory, "init", "-q")
        git(directory, "config", "user.name", "Factory onboarding")
        git(directory, "config", "user.email", "factory-onboarding@example.invalid")
        git(directory, "add", ".")
        git(directory, "commit", "-qm", "chore: snapshot app for hosted onboarding")
        self.update(r["app_id"], source_dir=directory.name, source_sha=commit, default_branch=default_branch)
        return directory

    def _started(self, app_id, info):
        with self.locked(app_id) as r:
            r["sessions"].append({**info, "cost_usd": None})
            if info.get("stage") == "environment-verification":
                r["phase"] = "Running the setup and checks in the hosted sandbox"
            self.write(r)

    def _finished(self, app_id, info):
        with self.locked(app_id) as r:
            sid = info["session_id"]
            row = next((s for s in r["sessions"] if s["session_id"] == sid), None)
            if row and row.get("accounted"):
                return
            cost = info.get("list_cost_usd")
            charge = float(cost) if cost is not None else float(r.get("reserved_usd", 0))
            r["spent_usd"] = round(r["spent_usd"] + charge, 4)
            r["reserved_usd"] = 0
            if row:
                row.update(cost_usd=cost, accounted=True, charged_usd=charge, stop_reason=info.get("stop_reason"))
            if cost is None:
                r["cost_warning"] = "Usage is unavailable; the reserved session limit counts against this budget until reconciled."
            self.write(r)

    def start(self, app_id, operation, plan_sha256=None):
        with self.locked(app_id) as r:
            if r["status"] in BUSY:
                raise HTTPException(409, "This app already has an operation in progress")
            if operation not in {"discover", "verify", "publish"}:
                raise HTTPException(400, "Unknown onboarding operation")
            if operation != "discover":
                if not plan_sha256 or plan_sha256 != r.get("plan_sha256"):
                    raise HTTPException(409, "The setup plan changed. Review the current plan before continuing.")
                if operation == "verify" and (not r["plan"].get("profile") or any(q["required"] and not r["answers"].get(q["id"]) for q in r["plan"]["questions"])):
                    raise HTTPException(409, "Resolve the required setup questions and inspect the revised plan first")
            if operation == "publish" and r.get("pr_url"):
                return self.get(app_id)
            available = r["budget_usd"] - r["spent_usd"]
            cap = min(3.0 if operation == "discover" else 4.0, available)
            if operation != "publish" and cap < 0.10:
                raise HTTPException(409, "The onboarding budget is exhausted; no new session was started")
            previous_attempt = {"plan": r.get("plan") or r.get("last_proposal"), "verification": r.get("verification"), "error": r.get("error")}
            r.update(status={"discover": "discovering", "verify": "verifying", "publish": "publishing"}[operation],
                     phase={"discover": "Inspecting source and preparing a setup proposal", "verify": "Provisioning a fresh hosted test environment", "publish": "Publishing the setup for review"}[operation],
                     error=None, reserved_usd=0 if operation == "publish" else round(cap, 2), owner_pid=os.getpid())
            if operation == "discover":
                r["previous_attempt"] = previous_attempt
                for key in ("plan", "plan_sha256", "verification"):
                    r.pop(key, None)
                r["evidence"] = []
            self.write(r)
        threading.Thread(target=self._job, args=(app_id, operation, cap), daemon=True, name=f"onboarding-{app_id}").start()
        return self.get(app_id)

    def recover(self, app_id):
        with self.locked(app_id) as r:
            if r["status"] not in BUSY or owner_alive(r):
                raise HTTPException(409, "The current controller is still running; no recovery was attempted")
            for session in r["sessions"]:
                if not session.get("accounted"):
                    try:
                        self.client.beta.sessions.events.send(session_id=session["session_id"], events=[{"type": "user.interrupt"}], timeout=30)
                    except Exception:
                        raise HTTPException(502, "Could not stop the previous hosted session. Recovery is blocked to avoid duplicate work.") from None
            if any(not s.get("accounted") for s in r["sessions"]):
                r["spent_usd"] += r.get("reserved_usd", 0)
                r["cost_warning"] = "Interrupted controller: the reserved limit was charged because final usage is unavailable."
                for session in r["sessions"]:
                    if not session.get("accounted"):
                        session.update(accounted=True, cost_usd=None, stop_reason="controller_recovered")
            r.update(status="failed", phase="Controller recovered; review and retry the last step", reserved_usd=0, owner_pid=None,
                     error="The previous Console process stopped. Its pending sessions were interrupted; no readiness was granted.")
            self.write(r)
        return self.get(app_id)

    def _job(self, app_id, operation, cap):
        try:
            getattr(self, "_" + operation)(app_id, cap)
        except Exception as exc:
            # SDK/HTTP exceptions may carry request bodies or authentication URLs.
            detail = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__} during {operation}. Check the linked session; no readiness was granted."
            self.update(app_id, status="failed", phase="Needs attention", error=detail[:1200])
        finally:
            with self.locked(app_id) as r:
                if r.get("reserved_usd"):
                    # A remote start followed by failure must never refund unknown spend.
                    active = [s for s in r["sessions"] if not s.get("accounted")]
                    if active:
                        r["spent_usd"] += r["reserved_usd"]
                        r["cost_warning"] = "The last session's cost is uncertain; its reserved limit was charged."
                    r["reserved_usd"] = 0
                    self.write(r)

    def _discover(self, app_id, cap):
        r = self.get(app_id)
        if "app_onboarding" not in self.cfg.agents:
            raise ValueError("The App Onboarding agent is not configured. Apply agents/app_onboarding.md to the Claude workspace.")
        with self.locked(app_id) as private:
            previous_attempt = private.get("previous_attempt")
        repo = self.source(r)
        _, source = snapshot(repo)
        request = {"kind": r["kind"], "name": r["name"], "description": r["description"], "answers": r["answers"],
                   "hosted_browser_automation": True, "execution_location": "Anthropic hosted Linux sandbox",
                   "previous_attempt": previous_attempt, "feedback": r.get("feedback", ""),
                   "instructions": "Inspect source only and propose a setup. No execution, external access or readiness claims."}
        driver = SessionDriver(self.cfg, client=self.client,
            on_session_started=lambda x: self._started(app_id, x), on_session_finished=lambda x: self._finished(app_id, x))
        # Discovery needs no outbound network or credentials; use a separate limited environment.
        env_profile = {"name": "app-discovery", "network": {"allowed_hosts": [], "allow_package_managers": False}}
        env_id = ensure_environment(self.client, env_profile, self.cfg.runs_dir / "_test_environments")
        result = None
        try:
            result = driver.run(agent_key="app_onboarding", title="Onboard " + r["name"], run_id=app_id, stage="onboarding-discovery",
                uploads={"source.tar": source, "onboarding-request.json": json.dumps(request).encode()},
                description=("Read onboarding-request.json and source.tar. Produce onboarding-plan.json following your instructions "
                             "and the uploaded schemas. Do not run application commands. Reuse a valid existing recipe if present. "
                             f"The change runner selects {self.cfg.service_manifest_path} test_environment.profile, or "
                             "test_environment.json when that pointer is absent. profile_path must match that selection in the "
                             "proposed source. Existing-app onboarding cannot change the manifest; ask a focused question if a "
                             "manifest change or recipe edit outside the allowed onboarding paths is essential."),
                rubric_md=None, max_iterations=1, budget_usd=cap, environment_id=env_id, vault_ids=[],
                schema_names=("onboarding_plan.schema.json", "test_environment.schema.json"))
            self._finished(app_id, {"session_id": result.session_id, "list_cost_usd": result.list_cost_usd, "stop_reason": result.stop_reason})
            raw = result.outputs.get("onboarding-plan.json")
            if not raw or len(raw) > 2 * 1024 * 1024:
                raise ValueError("The onboarding agent did not return a complete setup plan within its session limit")
            try:
                proposal = json.loads(raw)
                self.update(app_id, last_proposal=proposal)
                plan = validate_plan(proposal, r["kind"], repo, self.cfg.schemas_dir, self.cfg.service_manifest_path)
            except (json.JSONDecodeError, UnicodeError):
                raise ValueError("The onboarding agent returned unreadable setup JSON") from None
            required = any(q["required"] and not r["answers"].get(q["id"]) for q in plan["questions"])
            self.update(app_id, plan=plan, plan_sha256=fingerprint(plan), status="needs_input" if required else "setup_ready",
                        phase="Answer the remaining setup questions" if required else "Review the setup, then verify it")
        finally:
            driver.interrupt_active()
            if result is not None:
                try:
                    self.client.beta.sessions.archive(result.session_id, timeout=60)
                except Exception:
                    self.update(app_id, cleanup_warning="Discovery session archival needs retry.")

    def feedback(self, app_id, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 6000:
            raise HTTPException(400, "Describe the setup change in 1–6000 characters; do not include credentials")
        with self.locked(app_id) as r:
            if r["status"] in BUSY:
                raise HTTPException(409, "Wait for the current operation before changing its instructions")
            r.update(feedback=text.strip(), status="needs_input", phase="Feedback saved; discover again to revise the setup", plan_sha256=None)
            self.write(r)
        return self.get(app_id)

    def answers(self, app_id, answers):
        with self.locked(app_id) as r:
            if r["status"] in BUSY:
                raise HTTPException(409, "Wait for the current operation")
            allowed = {q["id"] for q in r.get("plan", {}).get("questions", [])}
            if not set(answers) <= allowed or any(not isinstance(v, str) or len(v) > 6000 for v in answers.values()):
                raise HTTPException(400, "Answer only the current setup questions")
            r["answers"].update(answers)
            r.update(status="needs_input", phase="Inspect again to incorporate these answers", plan_sha256=None)
            self.write(r)
        return self.get(app_id)

    def _candidate(self, r):
        source = self.folder(r["app_id"]) / r["source_dir"]
        candidate = self.folder(r["app_id"]) / ("candidate-" + r["plan_sha256"][:16])
        if candidate.exists():
            return candidate
        candidate.mkdir()
        _, archive = snapshot(source)
        unpack_source(archive, candidate)
        for f in r["plan"]["files"]:
            p = candidate / safe_path(f["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f["content"])
        git(candidate, "init", "-q")
        git(candidate, "config", "user.name", "Factory onboarding")
        git(candidate, "config", "user.email", "factory-onboarding@example.invalid")
        git(candidate, "add", ".")
        git(candidate, "commit", "-qm", "feat: prepare isolated app verification")
        return candidate

    def _verify(self, app_id, cap):
        r = self.get(app_id)
        candidate = self._candidate(r)
        with self.locked(app_id) as private:
            configured = {c["name"] for c in r["credentials"] if c["status"] == "configured"}
            vaults = tuple(c["vault_id"] for c in private["credentials"] if c["name"] in configured)
        required = {s["name"] for s in r["plan"]["profile"]["secrets"] if s["required"]}
        if required - configured:
            raise ValueError("Connect the required test credentials before verification")
        out = self.folder(app_id) / ("evidence-" + uuid.uuid4().hex[:10])
        result = run_hosted(self.cfg, candidate, out, profile_relative=r["plan"]["profile_path"], budget_usd=cap,
                            vault_ids=vaults, client=self.client, on_started=lambda x: self._started(app_id, x),
                            on_finished=lambda x: self._finished(app_id, x))
        result.update(source_sha=r["source_sha"], plan_sha256=r["plan_sha256"])
        try:
            report = json.loads((out / "environment-result.json").read_text())
            result["phases"] = [{k: phase[k] for k in ("phase", "status", "exit_code", "seconds") if k in phase}
                                for phase in report.get("phases", []) if isinstance(phase, dict)]
            failed = next((p for p in result["phases"] if p.get("status") != "passed"), None)
            if failed and result["status"] != "passed":
                result["detail"] = f"Hosted {failed.get('phase', 'setup')} did not pass (exit {failed.get('exit_code', 'unavailable')}). Review the session diagnosis, then revise the recipe and discover again."
        except (OSError, ValueError, TypeError):
            pass
        (out / "controller-result.json").write_text(json.dumps(result, indent=2))
        evidence = [{"name": n, "url": f"/api/apps/{app_id}/evidence/{quote(n)}"} for n in result["artifacts"]]
        limited = bool(r["plan"]["limitations"] or any(not c["available"] for c in r["plan"]["capabilities"]))
        status = ("ready_limited" if limited else "ready") if result["status"] == "passed" else "failed"
        self.update(app_id, status=status, phase="Baseline verified with stated limits" if status == "ready_limited" else "Baseline verified" if status == "ready" else "Hosted verification needs attention",
                    verification=result, evidence=evidence, evidence_dir=out.name,
                    error=None if result["status"] == "passed" else result["detail"])

    def credential(self, app_id, name, value):
        if not value or len(value) > 32000 or any(c in value for c in "\r\n\0"):
            raise HTTPException(400, "Enter a valid single-line test credential")
        with self.locked(app_id) as r:
            if r["status"] in BUSY:
                raise HTTPException(409, "Wait for the current operation before changing a connection")
            secret = next((s for s in r.get("plan", {}).get("profile", {}).get("secrets", []) if s["name"] == name), None)
            if not secret:
                raise HTTPException(400, "This credential is not declared by the reviewed test recipe")
            try:
                old = next((c for c in r["credentials"] if c["name"] == name), None)
                scope = secret_scope(secret)
                # Each declaration gets its own vault; removed or changed scopes are never attached.
                auth = {"type": "environment_variable", "secret_name": name, "secret_value": value,
                        "networking": {"type": "limited", "allowed_hosts": secret["allowed_hosts"]},
                        "injection_location": {"header": secret["injection"] in {"header", "header-and-body"}, "body": secret["injection"] in {"body", "header-and-body"}}}
                if old and old.get("vault_id") and old.get("scope_sha256") == scope:
                    self.client.beta.vaults.credentials.update(old["id"], vault_id=old["vault_id"], auth={"type": "environment_variable", "secret_value": value}, timeout=60)
                else:
                    vault = self.client.beta.vaults.create(display_name=f"Factory test: {r['name']} / {name}", metadata={"factory_app_id": app_id}, timeout=60).id
                    created = self.client.beta.vaults.credentials.create(vault_id=vault, display_name=name, auth=auth, timeout=60)
                    r["credentials"] = [c for c in r["credentials"] if c["name"] != name]
                    r["credentials"].append({"name": name, "id": created.id, "vault_id": vault, "scope_sha256": scope})
                self.write(r)
            except Exception:
                raise HTTPException(502, "The test credential could not be saved to the vault. Its value was not stored by Factory.") from None
        return self.get(app_id)

    def _publish(self, app_id, cap):
        r = self.get(app_id)
        if not r.get("verification") or r["verification"]["status"] != "passed":
            raise ValueError("Verify this setup before publishing its pull request")
        if r["verification"].get("plan_sha256") != r.get("plan_sha256") or r["verification"].get("source_sha") != r.get("source_sha"):
            raise ValueError("The verified source or setup changed; verify the current setup again")
        files = {f["path"]: f["content"].encode() for f in r["plan"]["files"]}
        if not files:
            self.update(app_id, status="ready_limited" if r["plan"]["limitations"] else "ready", phase="Existing setup reused; no setup changes to publish")
            return
        forge = self.forge(r)
        try:
            if r["kind"] == "greenfield":
                if forge.repository_exists():
                    if not r.get("repository_created"):
                        raise ValueError("The requested repository already exists; onboard it as an existing app")
                    default = r.get("default_branch", "main")
                else:
                    created = forge.create_repository(private=True, description=r["description"])
                    default = created["default_branch"]
                    self.update(app_id, repository_created=True, default_branch=default)
                base = forge._ref_sha(default)
            else:
                base = r["source_sha"]
            branch = "factory/onboarding-" + app_id.removeprefix("app_")
            response = forge.http.post(forge._r("/git/refs"), json={"ref": "refs/heads/" + branch, "sha": base})
            if response.status_code not in (201, 422):
                response.raise_for_status()
            forge.commit_files(branch, "feat: add Factory app onboarding and isolated verification", files)
            pr = forge.create_change_request(source_branch=branch, target_branch=(r.get("revision") or r.get("default_branch", "main")) if r["kind"] == "existing" and not re.fullmatch(r"[a-f0-9]{40}", r.get("revision", "")) else r.get("default_branch", "main"),
                title="Add Factory onboarding and isolated app verification",
                description=f"{r['plan']['summary']}\n\nHosted baseline verification passed. Setup fingerprint: `{r['plan_sha256']}`.\n\nLimits: " + "; ".join(r["plan"]["limitations"]), labels=[], reviewers=[], draft=True)
            self.update(app_id, pr_url=pr.url, status="ready_limited" if r["plan"]["limitations"] else "ready", phase="Setup pull request is ready for human review")
        finally:
            forge.http.close()

    def run_settings(self, app_id, target):
        """Resolve trusted per-run bindings server-side; client never supplies vault IDs."""
        r = self.get(app_id)
        if r["status"] not in {"ready", "ready_limited"} or r.get("verification", {}).get("status") != "passed":
            raise HTTPException(409, "Verify this app's current setup before starting a change run")
        verified = r["verification"]
        if verified.get("plan_sha256") != r.get("plan_sha256") or verified.get("source_sha") != r.get("source_sha"):
            raise HTTPException(409, "The onboarding evidence is stale; verify the current setup again")
        if target.get("url", "").removesuffix(".git").rstrip("/").lower() != r["repository_url"].rstrip("/").lower() or target.get("create"):
            raise HTTPException(409, "The selected repository differs from this onboarded app")
        profile = r["plan"]["profile"]
        configured = {c["name"] for c in r["credentials"] if c["status"] == "configured"}
        if {s["name"] for s in profile["secrets"] if s["required"]} - configured:
            raise HTTPException(409, "Reconnect the required test credentials for the current recipe")
        forge = self.forge(r)
        try:
            ref = target.get("default_branch") or r.get("default_branch", "main")
            def read_json(path):
                obj = forge._get("/contents/" + quote(safe_path(path), safe="/") + "?ref=" + quote(ref, safe=""))
                if obj.get("encoding") != "base64" or obj.get("size", 0) > 256 * 1024:
                    raise ValueError("Unsupported setup file")
                return json.loads(base64.b64decode(obj["content"]))
            manifest = read_json(self.cfg.service_manifest_path) if forge.file_exists(ref, self.cfg.service_manifest_path) else {}
            path = manifest.get("test_environment", {}).get("profile", "test_environment.json")
            if path != r["plan"]["profile_path"] or fingerprint(read_json(path)) != verified["profile_sha256"]:
                raise ValueError("Recipe is not the verified version")
        except Exception:
            raise HTTPException(409, "The selected base branch does not contain the verified setup. Merge its setup PR, or onboard and verify the current recipe before starting this run.") from None
        finally:
            forge.http.close()
        with self.locked(app_id) as private:
            vaults = [c["vault_id"] for c in private["credentials"] if c["name"] in configured]
        return {"FACTORY_PREVIEW": "managed", "FACTORY_APP_ID": app_id,
                "FACTORY_TEST_PROFILE_SHA256": verified["profile_sha256"],
                "FACTORY_TEST_VAULT_IDS": ",".join(vaults), "FACTORY_MANAGED_VERIFY_BUDGET_USD": "4"}

    def evidence(self, app_id, name):
        r = self.get(app_id)
        if name not in {x["name"] for x in r["evidence"]} or Path(name).name != name:
            raise HTTPException(404, "Evidence not found")
        p = self.folder(app_id) / r["evidence_dir"] / name
        if not p.is_file() or p.is_symlink():
            raise HTTPException(404, "Evidence not found")
        return p
