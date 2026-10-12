"""Provider-independent application recipes and hosted verification.

Existing repositories and greenfield scaffolds use the same profile. This module
never executes a recipe on the controller host. A schema-valid profile is a plan,
not evidence that the app, dependencies or browser work.
"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
import uuid
from pathlib import Path, PurePosixPath

import jsonschema

PROFILE_PATH = "test_environment.json"
MAX_SOURCE_BYTES = 100 * 1024 * 1024


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_profile(profile: dict, schema_dir: Path) -> list[str]:
    schema = json.loads((schema_dir / "test_environment.schema.json").read_text())
    # Do not echo invalid values: someone may accidentally put a credential here.
    errors = [f"{'/'.join(map(str, e.path)) or '$'}: invalid {e.validator}"
              for e in jsonschema.Draft202012Validator(schema).iter_errors(profile)]
    if errors:
        return errors
    for key, values in (
        ("service id", [s["id"] for s in profile["services"]]),
        ("service port", [s["port"] for s in profile["services"]]),
        ("secret name", [s["name"] for s in profile["secrets"]]),
    ):
        if len(set(values)) != len(values):
            errors.append(f"duplicate {key}")
    if "browser" in profile and "app" not in {s["id"] for s in profile["services"]}:
        errors.append("services must identify the browser target as app")
    hosts = set(profile["network"]["allowed_hosts"])
    for secret in profile["secrets"]:
        if not set(secret["allowed_hosts"]) <= hosts:
            errors.append(f"secret {secret['name']} requires a host absent from the environment allowlist")
    return errors


def snapshot(repo: Path, revision: str = "HEAD") -> tuple[str, bytes]:
    """Only committed source enters a sandbox; never copy the host checkout/env."""
    head = subprocess.run(["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
                          cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    source = subprocess.run(["git", "archive", "--format=tar", head], cwd=repo,
                            check=True, capture_output=True).stdout
    if len(source) > MAX_SOURCE_BYTES:
        raise ValueError("repository snapshot exceeds the 100 MiB test limit")
    with tarfile.open(fileobj=io.BytesIO(source)) as archive:
        for member in archive.getmembers():
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
                raise ValueError("repository snapshot contains unsupported paths or links")
            if name.name.startswith(".env") and name.name not in {".env.example", ".env.sample", ".env.template"}:
                raise ValueError("repository tracks a runtime dotenv file; exclude it before cloud testing")
    return head, source


def read_snapshot_profile(source: bytes, relative_path: str, schema_dir: Path) -> dict:
    path = PurePosixPath(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("profile path must be relative to the repository")
    with tarfile.open(fileobj=io.BytesIO(source)) as archive:
        try:
            member = archive.getmember(str(path))
        except KeyError as exc:
            raise ValueError("test environment profile is not committed at this revision") from exc
        if not member.isfile() or member.size > 256 * 1024:
            raise ValueError("invalid test environment profile file")
        profile = json.load(archive.extractfile(member))
    errors = validate_profile(profile, schema_dir)
    if errors:
        raise ValueError("; ".join(errors))
    return profile


def recipe_relative(manifest: dict | None) -> str:
    """Where the recipe lives: the service manifest's test_environment.profile, else test_environment.json."""
    rel = ((manifest or {}).get("test_environment") or {}).get("profile") or PROFILE_PATH
    path = PurePosixPath(rel) if isinstance(rel, str) else None
    if path is None or path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("test_environment.profile must be a path inside the repository")
    return str(path)


def read_snapshot_json(source: bytes, relative_path: str, limit: int = 256 * 1024) -> dict | None:
    """A committed JSON object from the snapshot, or None when the file is not committed at this revision."""
    path = PurePosixPath(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("path must be relative to the repository")
    with tarfile.open(fileobj=io.BytesIO(source)) as archive:
        try:
            member = archive.getmember(str(path))
        except KeyError:
            return None
        if not member.isfile() or member.size > limit:
            raise ValueError(f"{relative_path} is not a regular file below {limit // 1024} KiB")
        value = json.load(archive.extractfile(member))
    if not isinstance(value, dict):
        raise ValueError(f"{relative_path} is not a JSON object")
    return value


def environment_config(profile: dict) -> dict:
    return {"type": "cloud", "networking": {"type": "limited", **profile["network"]}}


FACTORY_PURPOSES = ("test", "discovery")   # metadata factory_purpose: the only environments Factory reads, reuses or archives
APP_LABELS = ("factory_app_id", "factory_app_name", "factory_repository")
MAX_ENVIRONMENT_PAGES = 20


def factory_environments(client, include_archived: bool = False) -> list:
    """Every environment Factory created in the workspace, across pages. Hand-made ones are never returned."""
    page = client.beta.environments.list(limit=100, include_archived=include_archived, timeout=60)
    found = []
    for _ in range(MAX_ENVIRONMENT_PAGES):
        found.extend(e for e in page.data if (e.metadata or {}).get("factory_purpose") in FACTORY_PURPOSES)
        if not page.has_next_page():
            break
        page = page.get_next_page()
    return found


def same_config(remote, config: dict) -> bool:
    """Compare the fields we set. Remote defaults may add harmless fields; an archived definition is never reused."""
    actual = remote.config.model_dump(exclude_none=True)
    net = actual.get("networking", {})
    expected = config["networking"]
    same = actual.get("type") == "cloud" and net.get("type") == "limited"
    same = same and set(net.get("allowed_hosts", [])) == set(expected["allowed_hosts"])
    same = same and net.get("allow_package_managers", False) == expected["allow_package_managers"]
    return same and not net.get("allow_mcp_servers", False) and not getattr(remote, "archived_at", None)


def app_labels(app: dict | None) -> dict:
    """Which app an environment executes, so any Console sharing the workspace can list it (Apps tab)."""
    if not app or not app.get("app_id"):
        return {}
    labels = {"factory_app_id": app["app_id"], "factory_app_name": (app.get("name") or "")[:200],
              "factory_repository": (app.get("repository_url") or "")[:500]}
    return {k: v for k, v in labels.items() if v}


def label_environment(client, env, labels: dict) -> None:
    """Add what is missing. An environment already labelled for another app (identical recipe) keeps its owner."""
    meta = env.metadata or {}
    owned = meta.get("factory_app_id")
    delta = {k: v for k, v in labels.items()
             if meta.get(k) != v and (k not in APP_LABELS or not owned or owned == labels.get("factory_app_id"))}
    if delta:
        try:
            client.beta.environments.update(env.id, metadata=delta, timeout=60)
        except Exception:  # noqa: BLE001, S110  (labels are bookkeeping; the definition itself is unchanged and usable)
            pass


def ensure_environment(client, profile: dict, cache_dir: Path, *, purpose: str = "test", app: dict | None = None) -> str:
    """Reuse an unchanged definition; every session still gets a fresh container.

    Reuse is looked up in the workspace, not only in this machine's cache: another Console, checkout or worktree
    that verified the same recipe already created its definition, and a second copy is only clutter."""
    import fcntl
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = fingerprint(profile)
    record = cache_dir / f"{digest}.json"
    config = environment_config(profile)
    labels = {"factory_purpose": purpose, "profile_sha256": digest, **app_labels(app)}
    with (cache_dir / f"{digest}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        env = None
        if record.exists():
            current = json.loads(record.read_text())
            try:
                remote = client.beta.environments.retrieve(current["environment_id"], timeout=60)
                env = remote if same_config(remote, config) else None
            except Exception as exc:
                if getattr(exc, "status_code", None) != 404:   # deleted in the Console: look it up afresh below
                    raise
        if env is None:
            # A human edit is not overwritten: only an unchanged definition of this exact recipe is reused.
            matches = [e for e in factory_environments(client)
                       if (e.metadata or {}).get("profile_sha256") == digest and same_config(e, config)]
            mine = [e for e in matches if app and (e.metadata or {}).get("factory_app_id") == app.get("app_id")]
            env = max(mine or matches, key=lambda e: str(e.created_at), default=None)
        if env is None:
            env = client.beta.environments.create(
                name=f"factory-{profile['name'][:28]}-{digest[:8]}-{uuid.uuid4().hex[:6]}", config=config,
                metadata=labels, timeout=60,
            )
        else:
            label_environment(client, env, labels)
        temporary = record.with_suffix(".tmp")
        temporary.write_text(json.dumps({"environment_id": env.id, "profile_sha256": digest, "config": config}, indent=2))
        temporary.replace(record)
        return env.id


SANDBOX_SYSTEM = """You operate a Factory-owned ephemeral test sandbox. Run only the uploaded
controller harness and report its result. Do not edit application source, profile,
harness, evidence or results; do not create commits, push, deploy, delegate, or
change permissions. Do not read, print, copy or search for credential values.
Start the harness once. It enforces the reviewed profile's lifecycle timeouts;
do not wrap it in a shorter timeout or terminate it because a tool call is slow.
If your tool returns a running process handle, wait on that same process.
Read-only diagnosis of failed command exit codes is permitted. If a command needs
approval, stop and report the specific blocker; do not substitute another route.
"""
BROWSER_SYSTEM = """The user approved browser automation inside this isolated cloud sandbox. The
controller harness provisions the application, synthetic data, browser and media.
Local-machine browser preferences do not forbid this hosted test workflow.
"""
DIAGNOSIS_SYSTEM = """If the harness exits non-zero, diagnose once before you stop. Read the failed phase's
log in /tmp/factory-test/logs/ and any service log the recipe's start command writes,
then write /mnt/session/outputs/diagnosis.json as {"phase": "<prepare|start|services|seed|verify|stop>",
"summary": "<at most 1200 characters>"}. Name the failing command or service, the error,
and the likely cause in the recipe or the app. Quote at most three short error lines.
Never include environment values, tokens, keys or other credential-like text. Do not
fix, retry or rerun anything.
"""
SYSTEM = SANDBOX_SYSTEM + BROWSER_SYSTEM      # change runs: their e2e entries may drive a browser
CHECK_STATUSES = {"passed", "failed", "blocked", "skipped"}
DIAGNOSIS_PHASES = {"prepare", "start", "services", "seed", "verify", "stop"}


def onboarding_system(profile: dict) -> str:
    """Browser wording only for browser recipes: an API, bot, worker, CLI or library is not a browser test."""
    return SANDBOX_SYSTEM + (BROWSER_SYSTEM if "browser" in profile else "") + DIAGNOSIS_SYSTEM


def functional_checks(profile: dict, output_dir: Path) -> list[dict] | None:
    """Each declared functional check with what verification-result.json reported for it (None for browser recipes).
    A non-browser app has no screenshots: these rows are its evidence, for the person and for a repair."""
    if "verification" not in profile:
        return None
    try:
        report = json.loads((output_dir / "verification-result.json").read_text())
    except (OSError, ValueError):
        report = None
    reported = report.get("checks") if isinstance(report, dict) else None
    by_id: dict[str, dict] = {}
    for check in reported if isinstance(reported, list) else []:
        if isinstance(check, dict) and isinstance(check.get("id"), str):
            by_id.setdefault(check["id"], check)
    rows = []
    for declared in profile["verification"]["checks"]:
        got = by_id.get(declared["id"])
        status = got.get("status") if got else None
        observation = got.get("observation") if got else None
        rows.append({"id": declared["id"], "description": declared["description"],
                     "status": status if isinstance(status, str) and status in CHECK_STATUSES else "not_reported" if got is None else "invalid",
                     "observation": observation.strip()[:600] if isinstance(observation, str) else ""})
    return rows


def read_diagnosis(output_dir: Path) -> dict | None:
    """The sandbox agent's bounded explanation of a failed run. Commentary, never evidence: raw logs stay in the sandbox."""
    p = output_dir / "diagnosis.json"
    try:
        if p.is_symlink() or not p.is_file() or p.stat().st_size > 16 * 1024:
            return None
        value = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("summary"), str) or not value["summary"].strip():
        return None
    phase = value.get("phase")
    if not isinstance(phase, str) or phase not in DIAGNOSIS_PHASES:
        phase = None
    return {"phase": phase, "summary": value["summary"].strip()[:1500]}


def explain(report: dict, *, bound: bool, passed: bool, browser: bool, checks: list[dict] | None,
            evidence_detail: str, diagnosed: bool) -> str:
    """What happened, in the order the harness ran it: the first failed phase, a service that never became ready (start
    exits 0 when it backgrounds a process that then crashes), missing credentials, then the evidence and each check."""
    if passed:
        return ("Sandbox recipe completed; inspect browser evidence for acceptance." if browser
                else "Every lifecycle phase and every declared functional check passed in the hosted sandbox.")
    if not bound:
        return "The hosted result is missing or not bound to this source, recipe and archive, so nothing it reports counts."
    phases = [p for p in report.get("phases", []) if isinstance(p, dict)]
    failed = next((p for p in phases if p.get("status") != "passed"), None)
    stalled = [str(s.get("service")) for s in report.get("services", []) if isinstance(s, dict) and s.get("status") != "passed"]
    if failed and failed.get("phase") != "stop":
        how = f"exit {failed['exit_code']}" if failed.get("exit_code") is not None else str(failed.get("reason") or failed.get("status"))
        text = f"The {failed.get('phase', 'setup')} command did not pass ({how})."
    elif stalled:
        text = (f"The start command returned, but {', '.join(stalled)} never answered its health check in the readiness "
                "window. The process most likely exited after launch or listens on another port or path.")
    elif report.get("missing_secrets"):
        text = "Required test credentials were not attached: " + ", ".join(map(str, report["missing_secrets"]))[:300] + "."
    elif failed:
        text = f"The recipe ran, but its stop command did not pass ({failed.get('reason') or failed.get('status')})."
    else:
        text = f"The recipe ran, but its evidence was not accepted: {evidence_detail}."
    bad = [c for c in checks or [] if c["status"] != "passed"]
    if bad:
        text += " Checks not passed: " + "; ".join(f"{c['id']} ({c['status'].replace('_', ' ')})" for c in bad) + "."
    text += (" The sandbox agent's diagnosis is attached." if diagnosed
             else " Raw logs stay in the sandbox; open the verification session for more.")
    return text


def run_hosted(cfg, repo: Path, output_dir: Path, *, profile_relative: str = PROFILE_PATH,
               revision: str = "HEAD", budget_usd: float, vault_ids: tuple[str, ...] = (), client=None, on_started=None, on_finished=None,
               app: dict | None = None) -> dict:
    from .config import anthropic_client
    from .sessions import SessionDriver
    if not 0 < budget_usd <= 60:
        raise ValueError("environment trial budget must be explicitly set between 0 and 60 USD")
    head, source = snapshot(repo, revision)
    profile = read_snapshot_profile(source, profile_relative, cfg.schemas_dir)
    if any(s["required"] for s in profile["secrets"]) and not vault_ids:
        raise ValueError("required test credentials are not configured; connect a test vault before running")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("use a fresh evidence directory for each test")
    output_dir.mkdir(parents=True, exist_ok=True)
    client = client or anthropic_client(cfg)
    digest = fingerprint(profile)
    environment_id = ensure_environment(client, profile, cfg.runs_dir / "_test_environments", app=app)
    descriptor = {"head_sha": head, "profile_sha256": digest, "source_sha256": hashlib.sha256(source).hexdigest(),
                  "profile_path": profile_relative, "environment_id": environment_id, "provider": "anthropic-cloud"}
    (output_dir / "request.json").write_text(json.dumps(descriptor, indent=2))
    def started(info):
        (output_dir / "session.json").write_text(json.dumps(info, indent=2))
        if on_started:
            on_started(info)
    driver = SessionDriver(cfg, client=client, on_session_started=started, on_session_finished=on_finished)
    try:
        result = driver.run(
            agent_key="implementer", title=f"Factory environment check: {profile['name']}",
            run_id=f"envcheck_{digest[:12]}", stage="environment-verification",
            uploads={"test-source.tar": source, "environment-request.json": json.dumps(descriptor).encode(),
                     "tools/environment_runner.py": (Path(__file__).parent / "sandbox_tools/environment_runner.py").read_bytes()},
            description="Run `python3 -I /mnt/session/uploads/tools/environment_runner.py`. All application, database and browser execution must stay inside this cloud sandbox. Report its exit status; leave its outputs intact. Never run against the user's computer or production.",
            rubric_md=None, max_iterations=1, budget_usd=budget_usd,
            environment_id=environment_id, vault_ids=list(vault_ids), system_override=onboarding_system(profile), metadata=app_labels(app),
        )
        if on_finished:
            on_finished({"session_id": result.session_id, "list_cost_usd": result.list_cost_usd, "stop_reason": result.stop_reason})
        saved = []
        for name, content in result.outputs.items():
            if Path(name).name != name or name in {"request.json", "controller-result.json"}:
                continue
            if len(content) > 32 * 1024 * 1024:
                continue
            (output_dir / name).write_bytes(content)
            saved.append(name)
        try:
            report = json.loads(result.outputs.get("environment-result.json", b"{}"))
        except ValueError:
            report = {}
        if not isinstance(report, dict):
            report = {}
        bound = all(report.get(k) == descriptor[k] for k in ("head_sha", "profile_sha256", "source_sha256"))
        from .sandbox_tools.environment_runner import check_evidence
        media_ok, media_detail = check_evidence(profile, output_dir)
        phases = {p.get("phase"): p.get("status") for p in report.get("phases", []) if isinstance(p, dict)}
        phases_ok = all(phases.get(p) == "passed" for p in ("prepare", "start", "seed", "verify", "stop"))
        passed = bound and report.get("status") == "passed" and media_ok and phases_ok
        # Check rows only once verify ran: before that, "not reported" on every check would bury the real cause.
        checks = functional_checks(profile, output_dir) if bound and "verify" in phases else None
        diagnosis = None if passed else read_diagnosis(output_dir)
        response = {**descriptor, "status": "passed" if passed else "blocked" if not bound else report.get("status", "blocked"),
                    "session_id": result.session_id, "cost_usd": result.list_cost_usd,
                    "stop_reason": result.stop_reason, "artifacts": saved,
                    "detail": explain(report, bound=bound, passed=passed, browser="browser" in profile, checks=checks,
                                      evidence_detail=media_detail, diagnosed=diagnosis is not None)}
        if bound:
            response.update(phases=[{k: p[k] for k in ("phase", "status", "exit_code", "seconds", "reason") if k in p}
                                    for p in report.get("phases", []) if isinstance(p, dict)],
                            services=[{k: str(s[k])[:200] for k in ("service", "status", "reason") if k in s}
                                      for s in report.get("services", []) if isinstance(s, dict)],
                            harness_reason=str(report.get("reason") or "")[:300] or None)
        if checks is not None:
            response["checks"] = checks
        if diagnosis:
            response["diagnosis"] = diagnosis
        if not passed and response["status"] == "passed":
            response["status"] = "blocked"
        try:
            client.beta.sessions.archive(result.session_id, timeout=60)
            response["session_archived"] = True
        except Exception:
            response["session_archived"] = False
            response["cleanup_warning"] = "Session archival needs retry."
        (output_dir / "controller-result.json").write_text(json.dumps(response, indent=2))
        return response
    finally:
        # Preserve the reusable environment definition and remote audit trail.
        # The harness stops services; the completed session is archived above.
        driver.interrupt_active()
