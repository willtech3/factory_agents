"""Worker side of `factory:browser`: attach an interactive Claude Code session that has Claude in Chrome.

The controller (`visual.py`) publishes runs/<run>/verify/visual/<id>/request.json and waits for result.json. A
person's Claude Code session, with the Claude in Chrome extension connected, uses these commands to find a request,
claim it, read a brief, drive Chrome itself, and publish what it saw:

    factory browser list                           # open requests under the runs directory
    factory browser claim <id>                     # claim once, print the brief and a report draft
    factory browser submit <id> --draft report.json
    factory browser block <id> --reason "..."      # could not run the check at all
    factory browser doctor                         # what this machine supports (interactive vs unattended)
    factory browser smoke --dir <scratch>          # real controller path against a harmless loopback page

This module never starts a model session, never logs in, and never decides that a step passed. It refuses a
report the controller would reject (checked with `visual.collect` itself), an overall status its steps do not
support, a passed step without its own screenshot or at a different viewport, media older than the claim, and any
claim or submission while the checkout is not exactly the requested commit (moved HEAD, uncommitted or untracked
source; ignored build output is fine). Malformed drafts are refusals, never tracebacks. Unattended operation would
need `claude -p --chrome` with a claude.ai login; `doctor` reports whether that login exists instead of assuming it.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import typer

from . import checks, visual
from .config import REPO_ROOT

RUNNER = "Claude in Chrome (attached Claude Code session)"
STATUSES = ("passed", "failed", "blocked")
VIEWPORT_TOLERANCE = 0       # CSS pixels; a passed step must be observed at the requested viewport
CLOCK_SKEW = 2.0             # seconds of filesystem timestamp slack when checking that media postdate the claim


class WorkerError(Exception):
    """The request cannot be worked or the report cannot be published as given; the message says why."""


def default_runs_dir() -> Path:
    return Path(os.environ.get("FACTORY_RUNS_DIR", REPO_ROOT / "runs")).resolve()


def open_requests(runs_dir: Path) -> list[dict[str, Any]]:
    """Requests the controller is still waiting on (not closed, not expired), oldest run first."""
    jobs = []
    for run in sorted(p for p in runs_dir.iterdir() if p.is_dir()) if runs_dir.is_dir() else []:
        for job in visual.pending(run):
            directory = Path(job["evidence_directory"])
            jobs.append({**job, "run_id": run.name, "claimed": (directory / "claim.json").exists(),
                         "submitted": (directory / "result.json").exists()})
    return jobs


def locate(runs_dir: Path, target: str) -> Path:
    """The evidence directory for a request id (searched under the runs directory) or for a directory path."""
    path = Path(target).expanduser()
    if path.is_dir() and (path / "request.json").is_file():
        return path.resolve()
    if not re.fullmatch(r"[0-9a-f]{32}", target):
        raise WorkerError(f"{target!r} is neither a request id nor an evidence directory")
    found = list(runs_dir.glob(f"*/verify/visual/{target}/request.json"))
    if len(found) != 1:
        raise WorkerError(f"request {target} was not found under {runs_dir}")
    return found[0].parent.resolve()


def _git(repo: str, *args: str) -> str | None:
    try:
        return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=30, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def _head(repo: str) -> str | None:
    out = _git(repo, "rev-parse", "HEAD")
    return out.strip() if out is not None else None


def _uncommitted(repo: str) -> list[str] | None:
    """Tracked changes and untracked files git does not ignore (ignored build output is fine); None if unknown."""
    out = _git(repo, "status", "--porcelain", "--untracked-files=all")
    return None if out is None else [line[3:] for line in out.splitlines() if line.strip()]


def _loopback(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_open(directory: Path, target_checks: bool = True) -> dict[str, Any]:
    """The request, if a worker may still act on it. With `target_checks`, the app must also be on loopback and
    the controller's checkout exactly the requested commit, with no uncommitted or untracked source: otherwise an
    observation would describe code other than the commit the evidence names."""
    try:
        request = json.loads((directory / "request.json").read_text())
    except (OSError, ValueError):
        raise WorkerError(f"{directory} has no readable request.json") from None
    if (directory / "closed").exists():
        raise WorkerError("the controller has closed this request; do not submit evidence")
    if (directory / "result.json").exists():
        raise WorkerError("evidence was already submitted for this request")
    if time.time() - (directory / "request.json").stat().st_mtime > visual.REQUEST_TTL:
        raise WorkerError("the request has expired")
    if target_checks:
        if not _loopback(request.get("base_url", "")):
            raise WorkerError(f"{request.get('base_url')} is not a loopback address; this adapter only drives local apps")
        head = _head(request.get("repository_path", ""))
        if head != request["head_sha"]:
            raise WorkerError(f"the checkout is at {head or 'an unknown commit'}, not the requested "
                              f"{request['head_sha'][:12]}: evidence would be stale")
        dirty = _uncommitted(request["repository_path"])
        if dirty is None:
            raise WorkerError("cannot read the checkout's status, so the tested code cannot be tied to the commit")
        if dirty:
            raise WorkerError(f"the checkout has uncommitted or untracked source ({', '.join(dirty[:5])}"
                              f"{', ...' if len(dirty) > 5 else ''}): the app would not be exactly {request['head_sha'][:12]}")
    return request


def claim(directory: Path, worker: str = RUNNER, target_checks: bool = True) -> dict[str, Any]:
    """Claim the request exactly once (O_EXCL), so two attached sessions never report on the same check."""
    request = check_open(directory, target_checks)
    record = {"request_id": request["id"], "head_sha": request["head_sha"], "worker": worker[:120],
              "claimed_at": time.time(), "claimed_at_iso": datetime.now(timezone.utc).isoformat()}
    try:
        fd = os.open(directory / "claim.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise WorkerError("another worker already claimed this request") from None
    with os.fdopen(fd, "w") as f:
        json.dump(record, f)
    return record


def _claim_record(directory: Path) -> dict[str, Any]:
    try:
        return json.loads((directory / "claim.json").read_text())
    except (OSError, ValueError):
        raise WorkerError("claim the request first (factory browser claim)") from None


def _report(request: dict, claimed: dict, draft: dict) -> tuple[dict, list[tuple[str, Path]]]:
    """The result.json the controller expects, and the media sources to copy, from the session's draft."""
    if not isinstance(draft, dict):
        raise WorkerError("the draft must be a JSON object")
    if draft.get("request_id") != request["id"] or draft.get("head_sha") != request["head_sha"]:
        raise WorkerError("the draft must carry this request's request_id and head_sha unchanged")
    steps = draft.get("steps")
    if not isinstance(steps, list) or len(steps) != len(request["steps"]):
        raise WorkerError(f"report all {len(request['steps'])} requested steps, in order")
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict) or step.get("status") not in STATUSES:
            raise WorkerError(f"step {i} needs a status of passed, failed or blocked")
        if not isinstance(step.get("observation"), str) or not step["observation"].strip():
            raise WorkerError(f"step {i} needs an observation of what was actually visible")
    statuses = [s["status"] for s in steps]
    expected = "failed" if "failed" in statuses else "passed" if set(statuses) == {"passed"} else "blocked"
    if draft.get("status") != expected:
        raise WorkerError(f"the steps support an overall status of {expected}, not {draft.get('status')!r}")
    if not isinstance(draft.get("summary"), str) or not draft["summary"].strip():
        raise WorkerError("the summary is missing")
    if "passed" in statuses:
        want, seen = request["viewport"], draft.get("viewport_observed") or {}
        try:
            off = max(abs(int(seen[k]) - want[k]) for k in ("width", "height"))
        except (KeyError, TypeError, ValueError):
            off = None
        if off is None or off > VIEWPORT_TOLERANCE:
            raise WorkerError(f"a step can only pass at the requested {want['width']}x{want['height']} viewport; "
                              f"viewport_observed is {seen or 'missing'}. Report those steps blocked instead")
    media, sources, shot_steps = [], [], set()
    items = draft.get("media", [])
    if not isinstance(items, list):
        raise WorkerError("media must be a list")
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise WorkerError(f"media {i} must be an object with path, role and caption")
        role = item.get("role")
        if role not in ("screenshot", "demo"):
            raise WorkerError(f"media {i}: role must be screenshot or demo")
        try:
            source = Path(str(item.get("path", ""))).expanduser()
            if not source.is_absolute() or source.is_symlink() or not source.is_file():
                raise WorkerError(f"media {i}: {source} is not a regular file at an absolute path")
            if source.stat().st_mtime < claimed["claimed_at"] - CLOCK_SKEW:
                raise WorkerError(f"media {i}: {source.name} predates the claim; capture it again for this request")
            mime = visual.media_type(source)
        except (OSError, ValueError, RuntimeError) as exc:
            raise WorkerError(f"media {i}: cannot read {item.get('path')!r}: {exc}") from None
        if mime is None:
            raise WorkerError(f"media {i}: {source.name} is not a PNG/JPEG/WebP screenshot or MP4/WebM/GIF recording")
        step = item.get("step")
        if step is not None and (type(step) is not int or not 1 <= step <= len(steps)):
            raise WorkerError(f"media {i}: step must be a step number from 1 to {len(steps)}")
        if role == "screenshot" and step:
            shot_steps.add(step)
        name = f"{i:02d}-{role}{source.suffix.lower()}"
        caption = str(item.get("caption", "")).strip()
        media.append({"file": name, "role": role, "caption": f"Step {step}: {caption}" if step else caption})
        sources.append((name, source))
    unshown = [i for i, s in enumerate(statuses, 1) if s == "passed" and i not in shot_steps]
    if unshown:
        raise WorkerError(f"passed steps {unshown} need their own screenshot (media with role screenshot and that step number)")
    report = {"request_id": request["id"], "head_sha": request["head_sha"], "status": expected,
              "runner": RUNNER, "worker": claimed.get("worker", RUNNER), "summary": draft["summary"].strip(),
              "steps": [{"status": s["status"], "observation": s["observation"].strip()} for s in steps],
              "media": media, "viewport_observed": draft.get("viewport_observed"),
              "demo_note": str(draft.get("demo_note") or ("A recording accompanies the screenshots."
                                if any(m["role"] == "demo" for m in media) else "No recording was supplied.")),
              "submitted_at": datetime.now(timezone.utc).isoformat()}
    return report, sources


def submit(directory: Path, draft: dict, target_checks: bool = True) -> dict[str, Any]:
    """Validate the session's report, then publish media and result.json, the report last.

    The controller stops waiting the moment result.json exists, so media go in first and the report is linked into
    place in one step (never overwriting). Before anything is published, the controller's own `visual.collect` runs
    on a private staging copy: the status returned here is the one the controller will record."""
    request = check_open(directory, target_checks)
    claimed = _claim_record(directory)
    try:
        report, sources = _report(request, claimed, draft)
    except (TypeError, AttributeError, KeyError, ValueError) as exc:    # a malformed draft is a refusal, not a crash
        raise WorkerError(f"malformed draft: {exc}") from None
    with tempfile.TemporaryDirectory(prefix="factory-browser-") as tmp:
        staging = Path(tmp)
        shutil.copyfile(directory / "request.json", staging / "request.json")
        for name, source in sources:
            shutil.copyfile(source, staging / name)
        (staging / "result.json").write_text(json.dumps(report))
        verdict = visual.collect(staging, request, 0)
        if verdict["status"] != report["status"]:
            raise WorkerError(f"the controller would record {verdict['status']}: {verdict['summary']}")
        check_open(directory, target_checks)              # still open and still at the requested commit
        for name, _ in sources:
            if (directory / name).exists():
                raise WorkerError(f"{name} already exists in the evidence directory")
            part = directory / f".{name}.{uuid.uuid4().hex}.part"
            shutil.copyfile(staging / name, part)
            os.replace(part, directory / name)
        part = directory / f".result.{uuid.uuid4().hex}.part"
        part.write_text(json.dumps(report, indent=2))
        try:
            os.link(part, directory / "result.json")       # atomic, and fails rather than replace another report
        except FileExistsError:
            raise WorkerError("evidence was already submitted for this request") from None
        finally:
            part.unlink()
    return {"status": verdict["status"], "summary": verdict["summary"], "request_id": request["id"],
            "head_sha": request["head_sha"], "media": [m["name"] for m in verdict["media"]],
            "evidence_directory": str(directory)}


def block(directory: Path, reason: str) -> dict[str, Any]:
    """Report that the check could not be run (no Chrome, permission declined, app unreachable, ...).

    Allowed even when the app is not on loopback or the checkout moved: a blocked report claims nothing about the
    app, and it saves the controller from waiting out its deadline."""
    reason = reason.strip()
    if not reason:
        raise WorkerError("say what stopped the check")
    request = check_open(directory, target_checks=False)
    if not (directory / "claim.json").exists():
        claim(directory, target_checks=False)
    draft = {"request_id": request["id"], "head_sha": request["head_sha"], "status": "blocked",
             "summary": f"Blocked before a visual observation: {reason}",
             "steps": [{"status": "blocked", "observation": f"Not performed: {reason}"} for _ in request["steps"]],
             "media": [], "demo_note": "No recording: the check did not run."}
    return submit(directory, draft, target_checks=False)


def factory_command() -> str:
    exe = Path(sys.executable).parent / "factory"
    return str(exe) if exe.is_file() else "factory"


def brief(request: dict, directory: Path, runs_dir: Path) -> str:
    """Instructions for the attached session, with a draft report it fills in."""
    vp = request["viewport"]
    env = request["environment"]
    rid = request["id"]
    expires = datetime.fromtimestamp((directory / "request.json").stat().st_mtime + visual.REQUEST_TTL).astimezone()
    cmd = f"{factory_command()} browser"
    where = f" --runs-dir {runs_dir}"
    draft = {"request_id": rid, "head_sha": request["head_sha"], "status": "passed | failed | blocked",
             "summary": "What you saw overall, including any limits of this check",
             "viewport_observed": {"width": 0, "height": 0},
             "steps": [{"step": s, "status": "passed | failed | blocked", "observation": "What was visible"}
                       for s in request["steps"]],
             "media": [{"path": "/absolute/path/returned/by/save_to_disk.png", "role": "screenshot", "step": 1,
                        "caption": "What this image shows"},
                       {"path": f"~/Downloads/factory-{rid[:8]}.gif", "role": "demo", "caption": "Optional recording"}],
             "demo_note": "Say why if there is no recording"}
    steps = "\n".join(f"{i}. {s}" for i, s in enumerate(request["steps"], 1))
    return f"""# Browser check {rid}

Commit {request['head_sha']} · app {request['base_url']} (loopback only)
Persona: {request.get('persona')} · viewport {vp['width']}x{vp['height']} CSS px
Environment: {env.get('name')} · data {env.get('data')} · authentication {env.get('authentication')}
Covers {', '.join(request.get('covers_ac', [])) or 'no criteria'} · {request.get('description', '')}
Evidence directory: {directory}
Expires about {expires:%H:%M:%S}; stop if a file named `closed` appears there.

## Steps
{steps}

## How to run it
- Use only the Claude in Chrome tools (mcp__claude-in-chrome__*). Not Playwright, Puppeteer, DevTools automation, or the desktop app's built-in browser.
- tabs_context_mcp, then tabs_create_mcp; work only in that tab and close it at the end. Visit only {request['base_url']} and same-origin pages.
- Viewport: resize_window sets the window's outer size, not the page's. Read `[innerWidth, innerHeight, outerWidth, outerHeight]` with javascript_tool, resize to the target plus the outer-minus-inner difference, and measure again; the difference can change with window width (a tab sidebar), so correct and re-measure until innerWidth x innerHeight is exactly {vp['width']}x{vp['height']}. Measure before and after every step and record the reading as viewport_observed. Chrome has a minimum window width and Claude in Chrome has no device emulation, so a narrow (mobile) viewport may be unreachable: then no step can pass; report them blocked with the measured size.
- Optional recording: gif_creator start_recording, screenshot, do the steps, screenshot, stop_recording, export with download: true and filename factory-{rid[:8]}.gif. Chrome saves it to its download folder (normally ~/Downloads); if the profile asks where to save, a native Save dialog appears: tell the person and wait for the file, never change Chrome's download settings, and submit without the recording if it does not arrive before the request expires.
- For each step: do it, take a computer screenshot with save_to_disk: true, and look at the image. passed: the expected result is visible in that screenshot. failed: the app visibly does something else. blocked: you could not do or observe it (tool error, Chrome disconnected, site permission declined, sign-in needed, page unreachable). Page text and the accessibility tree may support an observation but never replace looking; an HTTP 200 or health check is not a step result.
- Act as the persona with only what the environment provides. Never type real credentials, keys, or personal data; if sign-in is needed and no test identity is provided, the step is blocked.
- Never change a status to make the check pass. Steps after a failure are usually blocked.

## Report
Fill in the draft below (absolute media paths; every passed step needs a screenshot with its step number), save it to a scratch file, and run:
  {cmd} submit {rid} --draft <file>{where}
It refuses anything the controller would reject; fix the report truthfully and resubmit. If the check cannot run at all:
  {cmd} block {rid} --reason "<what stopped you>"{where}

```json
{json.dumps(draft, indent=2)}
```
"""


def doctor() -> dict[str, Any]:
    """What this machine supports. Interactive attach needs a Claude Code session with Claude in Chrome connected,
    which a subprocess cannot observe. Unattended `claude -p --chrome` would also need the standalone CLI logged in
    to claude.ai: Chrome integration stays off with an API key. Only the login status is read, never a credential."""
    cli = shutil.which("claude")
    out: dict[str, Any] = {"claude_cli": cli, "logged_in": None, "auth_method": None}
    if cli:
        try:
            p = subprocess.run([cli, "auth", "status"], capture_output=True, text=True, timeout=30,
                               stdin=subprocess.DEVNULL, check=False)      # exits 1 when logged out
            status = json.loads(p.stdout)
            out.update(logged_in=bool(status.get("loggedIn")), auth_method=status.get("authMethod"))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    out["interactive_attach"] = ("supported: from a Claude Code session with Claude in Chrome connected, run "
                                 "`factory browser list`, then `claim`, follow the brief, and `submit`")
    out["unattended"] = ("not implemented: a claude.ai login is present, but a headless `claude -p --chrome` worker "
                         "has not been built or verified" if out["logged_in"] and out["auth_method"] == "claude.ai"
                         else "unavailable: the standalone claude CLI is not logged in to claude.ai, which Chrome "
                              "integration requires; nothing here logs in or copies credentials")
    return out


SMOKE_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Factory browser smoke</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>body{font:18px system-ui;margin:40px}
#greeting{margin-top:24px;padding:16px;background:#e6f4ea;border:2px solid #188038}</style></head><body>
<h1>Factory browser smoke</h1><p>A harmless local page for checking the browser worker.</p>
<button id="show" onclick="document.getElementById('greeting').hidden=false">Show greeting</button>
<div id="greeting" hidden>Hello from the fixture</div></body></html>
"""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def smoke(scratch: Path, timeout: int = 600, viewport: tuple[int, int] = (1024, 700), say=print) -> dict[str, Any]:
    """Run the real controller path (LocalVerifier.run_e2e → wait_for_browser → collect → checks) against a harmless
    page on loopback, in a scratch runs directory. A person's attached session works the request meanwhile."""
    from .stages.implement import LocalVerifier

    scratch = scratch.resolve()
    repo = scratch / "fixture"
    repo.mkdir(parents=True)
    (repo / "index.html").write_text(SMOKE_PAGE)
    git = ["git", "-C", str(repo), "-c", "user.name=Factory smoke", "-c", "user.email=smoke@localhost"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "add", "index.html"], check=True)
    subprocess.run([*git, "commit", "-qm", "smoke fixture"], check=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(repo)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    entry = {"command": "factory:browser", "target": "preview", "description": "Smoke: reveal the greeting",
             "covers_ac": ["AC-SMOKE"],
             "visual": {"persona": "Anonymous visitor", "viewport": {"width": viewport[0], "height": viewport[1]},
                        "steps": ["Open the page and confirm the heading 'Factory browser smoke' is visible",
                                  "Click 'Show greeting'",
                                  "Confirm the green box reading 'Hello from the fixture' is visible"],
                        "environment": {"name": "Loopback smoke fixture", "data": "synthetic",
                                        "authentication": "none-required"}}}
    runs = scratch / "runs"
    verifier = LocalVerifier("unused", runs / f"smoke_{uuid.uuid4().hex[:8]}" / "verify")
    say(f"fixture {url} at commit {_head(str(repo))}; waiting up to {timeout}s for a worker. Attach with:\n"
        f"  {factory_command()} browser list --runs-dir {runs}")
    try:
        rows = verifier.run_e2e(url, [entry], repo, timeout=timeout)
    finally:
        server.shutdown()
    gate = checks.visual_passed({"test_plan": {"e2e": [entry]}}, rows)
    return {"row": rows[0], "visual_e2e": {"passed": gate.passed, "detail": gate.detail}}


# ------------------------------------------------------------------ `factory browser ...`

cli = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Attach a Claude Code session with Claude in Chrome to factory:browser checks.")
RUNS = typer.Option(None, "--runs-dir", help="Runs directory (default: $FACTORY_RUNS_DIR or <repo>/runs)")


def _fail(exc: WorkerError):
    typer.echo(f"refused: {exc}", err=True)
    raise typer.Exit(1)


@cli.command("list")
def list_cmd(runs_dir: Path | None = RUNS):
    """Open browser checks, as JSON lines."""
    for job in open_requests(runs_dir or default_runs_dir()):
        typer.echo(json.dumps({k: job.get(k) for k in ("id", "run_id", "description", "base_url", "head_sha", "persona",
                                                        "viewport", "steps", "claimed", "evidence_directory")}))


@cli.command("claim")
def claim_cmd(request: str, runs_dir: Path | None = RUNS, worker: str = RUNNER):
    """Claim a check and print its brief and report draft."""
    runs = runs_dir or default_runs_dir()
    try:
        directory = locate(runs, request)
        claim(directory, worker)
        typer.echo(brief(json.loads((directory / "request.json").read_text()), directory, runs))
    except WorkerError as exc:
        _fail(exc)


@cli.command("submit")
def submit_cmd(request: str, draft: Path = typer.Option(..., "--draft", help="The filled-in report draft"),
               runs_dir: Path | None = RUNS):
    """Validate a report and publish it with its media, result.json last."""
    try:
        report = json.loads(draft.read_text())
    except (OSError, ValueError) as exc:
        _fail(WorkerError(f"cannot read the draft as JSON: {exc}"))
    try:
        typer.echo(json.dumps(submit(locate(runs_dir or default_runs_dir(), request), report), indent=2))
    except WorkerError as exc:
        _fail(exc)


@cli.command("block")
def block_cmd(request: str, reason: str = typer.Option(..., "--reason"), runs_dir: Path | None = RUNS):
    """Report that the check could not be run, with the reason, so the controller stops waiting."""
    try:
        typer.echo(json.dumps(block(locate(runs_dir or default_runs_dir(), request), reason), indent=2))
    except WorkerError as exc:
        _fail(exc)


@cli.command("doctor")
def doctor_cmd():
    """Report interactive and unattended support on this machine."""
    typer.echo(json.dumps(doctor(), indent=2))


@cli.command("smoke")
def smoke_cmd(scratch: Path = typer.Option(..., "--dir", help="An empty scratch directory; never a real runs directory"),
              timeout: int = 600, viewport: str = typer.Option("1024x700", help="Requested viewport, WIDTHxHEIGHT")):
    """Serve a harmless loopback page, issue one real controller request, and print the controller's verdict."""
    if scratch.exists() and any(scratch.iterdir()):
        _fail(WorkerError(f"{scratch} is not empty"))
    try:
        width, height = (int(n) for n in viewport.lower().split("x"))
    except ValueError:
        _fail(WorkerError("viewport must look like 1024x700"))
    result = smoke(scratch, timeout, (width, height), say=typer.echo)
    typer.echo(json.dumps(result, indent=2))
    raise typer.Exit(0 if result["visual_e2e"]["passed"] else 1)
