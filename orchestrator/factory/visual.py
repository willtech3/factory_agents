"""Browser evidence protocol. The controller owns identity; workers supply observations and media.

A visual e2e command receives FACTORY_VISUAL_REQUEST and FACTORY_EVIDENCE_DIR. It writes result.json
in that fresh directory. `factory:browser` instead waits for a connected browser operator to do so.
Neither exit 0 nor a recording alone proves the UI criteria: the report must cover every requested step.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_FILE = 30 * 1024 * 1024
REQUEST_TTL = 900          # seconds an open request stays listed for workers (the default e2e deadline)
MAX_TOTAL = 64 * 1024 * 1024
MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
         ".mp4": "video/mp4", ".webm": "video/webm", ".gif": "image/gif"}


def new_request(workdir: Path, repo: Path, head: str, url: str, entry: dict) -> tuple[Path, dict]:
    directory = workdir / "visual" / uuid.uuid4().hex
    directory.mkdir(parents=True)
    request = {"version": 1, "id": directory.name, "head_sha": head, "base_url": url,
               "repository_path": str(repo), "covers_ac": entry.get("covers_ac", []),
               "description": entry.get("description", "Browser check"), **entry["visual"],
               "factory_runner": str(entry.get("command", "")).startswith("factory:journey"),
               "created_at": datetime.now(timezone.utc).isoformat()}
    (directory / "request.json").write_text(json.dumps(request, indent=2))
    return directory, request


def wait_for_browser(directory: Path, timeout: int) -> dict:
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if (directory / "result.json").is_file():
                return {"exit_code": 0, "stdout_tail": "Browser worker returned evidence", "stderr_tail": ""}
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        return {"exit_code": None, "stdout_tail": "", "stderr_tail": "Browser worker did not return evidence before the deadline"}
    finally:
        (directory / "closed").touch()


def media_type(path: Path) -> str | None:
    """Only browser media, checked by both suffix and signature; never render uploaded HTML/SVG."""
    mime = MEDIA.get(path.suffix.lower())
    if not mime:
        return None
    with path.open("rb") as f:
        h = f.read(32)
    valid = {"image/png": h.startswith(b"\x89PNG\r\n\x1a\n"), "image/jpeg": h.startswith(b"\xff\xd8\xff"),
             "image/webp": h.startswith(b"RIFF") and h[8:12] == b"WEBP",
             "image/gif": h.startswith((b"GIF87a", b"GIF89a")), "video/webm": h.startswith(b"\x1aE\xdf\xa3"),
             "video/mp4": h[4:8] == b"ftyp"}
    return mime if valid.get(mime) else None


def collect(directory: Path, request: dict, exit_code: int | None) -> dict:
    # summary_source says who wrote the summary: the worker's own words ("worker"), or the controller's reason for
    # refusing or not having its report ("controller"). The Console shows the worker's text as its interpretation only.
    result = {**request, "status": "blocked", "summary": "No browser evidence returned", "summary_source": "controller", "media": [],
              "steps_observed": [], "recorded_at": datetime.now(timezone.utc).isoformat()}
    try:
        p = directory / "result.json"
        if p.is_symlink() or p.stat().st_size > 128 * 1024:
            raise ValueError("result.json must be a regular report below 128 KiB")
        report = json.loads(p.read_text())
        if report.get("request_id") != request["id"] or report.get("head_sha") != request["head_sha"]:
            raise ValueError("browser evidence belongs to a different request or commit")
        status = report.get("status")
        if status not in ("passed", "failed", "blocked"):
            raise ValueError("browser status must be passed, failed or blocked")
        if not isinstance(report.get("summary"), str) or not report["summary"].strip():
            raise ValueError("browser observation summary is missing")
        result.update(status=status, summary=report["summary"][:2000], summary_source="worker", runner=str(report.get("runner", "unspecified"))[:120])
        # Credential hygiene: no media from a check during which a test credential was on screen; the factory's own
        # runner must say so either way (post-mortem 2026-10-11 §1).
        seen = report.get("viewport_observed")
        if isinstance(seen, dict) and all(isinstance(seen.get(k), int) and not isinstance(seen.get(k), bool) for k in ("width", "height")):
            result["viewport_observed"] = {"width": seen["width"], "height": seen["height"]}
        if report.get("secret_seen") is True or (request.get("factory_runner") and report.get("secret_seen") is not False):
            result["credential_seen"] = True
            raise ValueError("a test credential was visible during the check (or the runner did not say), so no media from it is kept")
        steps = report.get("steps", [])
        if not isinstance(steps, list) or len(steps) != len(request["steps"]):
            raise ValueError("browser report must account for every planned step")
        for i, step in enumerate(steps):
            if not isinstance(step, dict) or step.get("status") not in ("passed", "failed", "blocked") or not step.get("observation"):
                raise ValueError("each step needs a status and a visual observation")
            result["steps_observed"].append({"step": request["steps"][i], "status": step["status"], "observation": str(step["observation"])[:2000]})
        total = 0
        files = report.get("media", [])
        if not isinstance(files, list) or len(files) > 12:
            raise ValueError("at most 12 media files may accompany a browser check")
        for item in files:
            name = item.get("file", "")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}", name) or name in ("request.json", "result.json"):
                raise ValueError("media filenames must be plain basenames")
            path = directory / name
            if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
                raise ValueError("media must be a regular file inside the evidence directory")
            size = path.stat().st_size
            total += size
            if not 0 < size <= MAX_FILE or total > MAX_TOTAL:
                raise ValueError("browser evidence exceeds the 30 MiB/file or 64 MiB/check limit")
            mime = media_type(path)
            if mime is None:
                raise ValueError("unsupported or invalid browser media")
            role = item.get("role")
            if role not in ("screenshot", "demo") or (role == "screenshot" and mime not in ("image/png", "image/jpeg", "image/webp")):
                raise ValueError("media role must match a screenshot or a demonstration")
            result["media"].append({"name": name, "role": role, "mime_type": mime, "bytes": size,
                                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                    "caption": str(item.get("caption", ""))[:500], "source": str(path)})
        if status == "passed":
            if exit_code != 0 or any(s["status"] != "passed" for s in result["steps_observed"]):
                raise ValueError("a failed command or step cannot pass the browser check")
            if not any(m["role"] == "screenshot" for m in result["media"]):
                raise ValueError("a passing browser check needs an observed screenshot")
        result["demo_note"] = str(report.get("demo_note", "Recording not supplied; screenshots are available."))[:500]
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        result.update(status="blocked", summary=f"Browser evidence unavailable: {exc}", summary_source="controller")
        result["media"] = []
    return result


def persist(ctx, spec_id: str, round_no: int, head: str, rows: list[dict]) -> dict[str, bytes]:
    """Keep validated media immutable, give reviewers stills, and emit current-head evidence for the Console."""
    uploads = {}
    for index, row in enumerate(rows):
        evidence = row.get("visual")
        if not evidence:
            continue
        prefix = f"{spec_id}-r{round_no}-v{index}-{evidence.get('id', 'blocked')[:12]}-"
        for media in evidence.get("media", []):
            source = Path(media.pop("source"))
            content = source.read_bytes()
            if hashlib.sha256(content).hexdigest() != media["sha256"]:
                evidence.update(status="blocked", summary="Browser media changed after verification", summary_source="controller")
                continue
            name = prefix + media["name"]
            ctx.store.put_artifact(ctx.run_id, "visual", name, content)
            media["artifact"] = f"visual/{name}"
            if media["role"] == "screenshot":
                uploads[f"visual/{name}"] = content
        evidence["head_sha"] = head
        name = prefix + "evidence.json"
        ctx.store.put_artifact(ctx.run_id, "visual", name, json.dumps(evidence, indent=2).encode())
        ctx.event("visual.verified", {"head_sha": head, "status": evidence["status"], "path": f"visual/{name}",
                                      "description": evidence.get("description", "Browser check")}, spec_id=spec_id)
    uploads["visual_evidence.json"] = json.dumps([r["visual"] for r in rows if r.get("visual")], indent=2).encode()
    return uploads


def summaries(root: Path, events: list) -> list[dict]:
    latest = {}
    for ev in events:
        if ev.kind in ("verification.started", "visual.verified") and ev.spec_id:
            latest[ev.spec_id] = ev.payload.get("head_sha")
    out = []
    for ev in reversed(events):
        if ev.kind != "visual.verified":
            continue
        try:
            path = (root / ev.payload["path"]).resolve()
            if not path.is_relative_to(root.resolve()):
                continue
            evidence = json.loads(path.read_text())
            evidence.update(spec_id=ev.spec_id, current=evidence.get("head_sha") == latest.get(ev.spec_id))
            out.append(evidence)
        except (OSError, ValueError, KeyError):
            continue
    return out


def pending(run_path: Path) -> list[dict]:
    jobs = []
    for p in (run_path / "verify" / "visual").glob("*/request.json"):
        if (p.parent / "closed").exists() or time.time() - p.stat().st_mtime > REQUEST_TTL:
            continue
        try:
            request = json.loads(p.read_text())
            jobs.append({**request, "request_path": str(p), "evidence_directory": str(p.parent)})
        except (OSError, ValueError):
            continue
    return jobs
