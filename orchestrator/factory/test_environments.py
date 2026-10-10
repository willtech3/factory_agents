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
from typing import Any

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


def profile_path(repo: Path, manifest: dict) -> Path:
    rel = manifest.get("test_environment", {}).get("profile", PROFILE_PATH)
    p = repo / rel
    if p.is_symlink() or not p.resolve().is_relative_to(repo.resolve()):
        raise ValueError("test environment profile must be inside the repository")
    return p


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


def ensure_environment(client, profile: dict, cache_dir: Path) -> str:
    """Reuse an unchanged definition; every session still gets a fresh container."""
    import fcntl
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = fingerprint(profile)
    record = cache_dir / f"{digest}.json"
    config = environment_config(profile)
    with (cache_dir / f"{digest}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if record.exists():
            current = json.loads(record.read_text())
            remote = client.beta.environments.retrieve(current["environment_id"], timeout=60)
            actual = remote.config.model_dump(exclude_none=True)
            # Compare the fields we set. Remote defaults may add harmless fields.
            net = actual.get("networking", {})
            expected = config["networking"]
            same = actual.get("type") == "cloud" and net.get("type") == "limited"
            same = same and set(net.get("allowed_hosts", [])) == set(expected["allowed_hosts"])
            same = same and net.get("allow_package_managers", False) == expected["allow_package_managers"]
            same = same and not net.get("allow_mcp_servers", False) and not getattr(remote, "archived_at", None)
            if same:
                return remote.id
            # A human edit is not overwritten. Create a distinct verified definition.
        env = client.beta.environments.create(
            name=f"factory-{profile['name'][:28]}-{digest[:8]}-{uuid.uuid4().hex[:6]}", config=config,
            metadata={"factory_purpose": "test", "profile_sha256": digest}, timeout=60,
        )
        temporary = record.with_suffix(".tmp")
        temporary.write_text(json.dumps({"environment_id": env.id, "profile_sha256": digest, "config": config}, indent=2))
        temporary.replace(record)
        return env.id


SYSTEM = """You operate a Factory-owned ephemeral test sandbox. Run only the uploaded
controller harness and report its result. Do not edit application source, profile,
harness, evidence or results; do not create commits, push, deploy, delegate, or
change permissions. Do not read, print, copy or search for credential values.
Start the harness once. It enforces the reviewed profile's lifecycle timeouts;
do not wrap it in a shorter timeout or terminate it because a tool call is slow.
If your tool returns a running process handle, wait on that same process.
Read-only diagnosis of failed command exit codes is permitted. If a command needs
approval, stop and report the specific blocker; do not substitute another route.
The user approved browser automation inside this isolated cloud sandbox. The
controller harness provisions the application, synthetic data, browser and media.
Local-machine browser preferences do not forbid this hosted test workflow.
"""


def run_hosted(cfg, repo: Path, output_dir: Path, *, profile_relative: str = PROFILE_PATH,
               revision: str = "HEAD", budget_usd: float, vault_ids: tuple[str, ...] = (), client=None, on_started=None, on_finished=None) -> dict:
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
    environment_id = ensure_environment(client, profile, cfg.runs_dir / "_test_environments")
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
            environment_id=environment_id, vault_ids=list(vault_ids), system_override=SYSTEM,
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
        media_ok, _ = check_evidence(profile, output_dir)
        phases = {p.get("phase"): p.get("status") for p in report.get("phases", []) if isinstance(p, dict)}
        phases_ok = all(phases.get(p) == "passed" for p in ("prepare", "start", "seed", "verify", "stop"))
        passed = bound and report.get("status") == "passed" and media_ok and phases_ok
        response = {**descriptor, "status": "passed" if passed else "blocked" if not bound else report.get("status", "blocked"),
                    "session_id": result.session_id, "cost_usd": result.list_cost_usd,
                    "stop_reason": result.stop_reason, "artifacts": saved,
                    "detail": "Sandbox recipe completed; inspect browser evidence for acceptance." if passed else "Environment verification did not pass; inspect recorded phase statuses."}
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
