#!/usr/bin/env python3
"""Controller-owned lifecycle harness, executed only in a hosted Linux sandbox.

No provider calls or host paths. The controller uploads an immutable Git archive,
profile, and this harness. Raw process logs stay in the sandbox; the downloadable
report contains statuses and media, not command output or environment values.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

UPLOADS = Path("/mnt/session/uploads")
OUTPUTS = Path("/mnt/session/outputs")


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def command(text: str, cwd: Path, env: dict, timeout: float, log: Path) -> dict:
    started = time.monotonic()
    with log.open("wb") as output:
        proc = subprocess.Popen(["/bin/bash", "-lc", text], cwd=cwd, env=env,
                                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = proc.wait(timeout=max(1, timeout))
            return {"status": "passed" if code == 0 else "failed", "exit_code": code,
                    "seconds": round(time.monotonic() - started, 2)}
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            return {"status": "blocked", "reason": "timeout"}


def health(services: list[dict], deadline: float) -> list[dict]:
    pending = {s["id"]: s for s in services}
    result = []
    while pending and time.monotonic() < deadline:
        for name, service in list(pending.items()):
            url = f"http://127.0.0.1:{service['port']}{service['health_path']}"
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status == service.get("expect_status", 200):
                        result.append({"service": name, "status": "passed"})
                        del pending[name]
            except Exception:
                pass
        if pending:
            time.sleep(0.5)
    return result + [{"service": name, "status": "blocked", "reason": "readiness timeout"} for name in pending]


def check_browser(profile: dict, evidence: Path) -> tuple[bool, str]:
    try:
        report = json.loads((evidence / "browser-result.json").read_text())
        steps = report["steps"]
        if report.get("status") != "passed" or not steps:
            return False, "browser reported failed or blocked steps"
        covered = set()
        screenshots = set()
        for step in steps:
            vp = step["viewport"]
            covered.add((vp["width"], vp["height"]))
            shot = step["screenshot"]
            if shot in screenshots:
                return False, "each browser step must have its own screenshot"
            screenshots.add(shot)
            p = evidence / shot
            if step["status"] != "passed" or Path(shot).name != shot or p.is_symlink() or not p.is_file():
                return False, "a browser step lacks fresh screenshot evidence"
            with p.open("rb") as f:
                magic = f.read(12)
            if not (magic.startswith(b"\x89PNG\r\n\x1a\n") or magic.startswith(b"\xff\xd8\xff")
                    or magic.startswith(b"RIFF") and magic[8:12] == b"WEBP"):
                return False, "invalid screenshot format"
        needed = {(v["width"], v["height"]) for v in profile["browser"]["viewports"]}
        if not needed <= covered:
            return False, "requested viewport coverage is incomplete"
        demo = report.get("demo")
        if profile["browser"]["record_demo"]:
            if not demo or Path(demo).name != demo or not (evidence / demo).is_file() or (evidence / demo).is_symlink():
                return False, "requested demonstration recording is missing"
            with (evidence / demo).open("rb") as f:
                magic = f.read(12)
            if not (magic.startswith(b"\x1aE\xdf\xa3") or magic[4:8] == b"ftyp"):
                return False, "invalid demonstration recording format"
        return True, "browser steps and requested viewports have evidence"
    except (OSError, ValueError, KeyError, TypeError):
        return False, "missing or malformed browser result"


def check_evidence(profile: dict, evidence: Path) -> tuple[bool, str]:
    if "browser" in profile:
        return check_browser(profile, evidence)
    try:
        report = json.loads((evidence / "verification-result.json").read_text())
        checks = report["checks"]
        expected = {c["id"] for c in profile["verification"]["checks"]}
        got = {c["id"] for c in checks}
        passed = (report["status"] == "passed" and got == expected and len(checks) == len(expected)
                  and all(c["status"] == "passed" and c.get("observation") for c in checks))
        return passed, "Declared functional checks passed" if passed else "Functional coverage is incomplete or failed"
    except (OSError, ValueError, KeyError, TypeError):
        return False, "Missing or malformed functional evidence"


def run(uploads: Path = UPLOADS, outputs: Path = OUTPUTS) -> dict:
    if platform.system() != "Linux" or uploads != UPLOADS:
        raise RuntimeError("this harness runs only in the hosted sandbox")
    request = json.loads((uploads / "environment-request.json").read_text())
    source = uploads / "test-source.tar"
    if hashlib.sha256(source.read_bytes()).hexdigest() != request["source_sha256"]:
        raise RuntimeError("source archive fingerprint mismatch")
    work = Path("/tmp/factory-test")
    work.mkdir(exist_ok=False)
    repo, logs, evidence = work / "repo", work / "logs", work / "evidence"
    repo.mkdir(); logs.mkdir(); evidence.mkdir()
    outputs.mkdir(parents=True, exist_ok=True)
    with tarfile.open(source) as archive:
        for member in archive.getmembers():
            p = Path(member.name)
            if p.is_absolute() or ".." in p.parts or not (member.isfile() or member.isdir()):
                raise RuntimeError("unsafe archive member")
        archive.extractall(repo, filter="data")
    profile = json.loads((repo / request["profile_path"]).read_text())
    if digest(profile) != request["profile_sha256"]:
        raise RuntimeError("profile fingerprint mismatch")
    app = next((s for s in profile["services"] if s["id"] == "app"), None)
    env = dict(os.environ)
    env.update(BASE_URL=f"http://127.0.0.1:{app['port']}" if app else "", FACTORY_EVIDENCE_DIR=str(evidence),
               FACTORY_TEST_HEAD=request["head_sha"], FACTORY_TEST_PROFILE=str(repo / request["profile_path"]))
    deadline = time.monotonic() + profile["timeout_seconds"]
    phases: list[dict] = []
    report = {**request, "status": "blocked", "phases": phases, "services": [],
              "execution": {"os": platform.system(), "architecture": platform.machine(), "location": "hosted-sandbox"}}
    try:
        missing = [s["name"] for s in profile["secrets"] if s["required"] and not env.get(s["name"])]
        if missing:
            report["reason"] = "required test credentials are not attached"
            report["missing_secrets"] = missing
            return report
        for phase in ("prepare", "start", "seed", "verify"):
            if phase == "seed":
                report["services"] = health(profile["services"], min(deadline, time.monotonic() + 90))
                if any(s["status"] != "passed" for s in report["services"]):
                    report["reason"] = "dependent services did not become ready"
                    return report
            result = command(profile["commands"][phase], repo, env, deadline - time.monotonic(), logs / f"{phase}.log")
            phases.append({"phase": phase, **result})
            if result["status"] != "passed":
                report.update(status=result["status"], reason=f"{phase} did not pass")
                return report
        passed, detail = check_evidence(profile, evidence)
        report.update(status="passed" if passed else "blocked", reason=detail)
        return report
    finally:
        stopped = command(profile["commands"]["stop"], repo, env, 30, logs / "stop.log")
        phases.append({"phase": "stop", **stopped})
        if stopped["status"] != "passed":
            report.update(status="blocked", reason="environment teardown failed")
        total = 0
        for p in sorted(evidence.iterdir()):
            # Never upload raw runtime logs, secret files, links, or arbitrary output.
            if p.is_symlink() or not p.is_file() or p.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".webm", ".mp4"} and p.name not in {"browser-result.json", "verification-result.json"}:
                continue
            size = p.stat().st_size
            if size > 32 * 1024 * 1024 or total + size > 64 * 1024 * 1024:
                report.update(status="blocked", reason="evidence size limit exceeded")
                continue
            total += size
            shutil.copyfile(p, outputs / p.name)
        (outputs / "environment-result.json").write_text(json.dumps(report, indent=2))


if __name__ == "__main__":
    result = run()
    print(json.dumps({"status": result["status"], "reason": result.get("reason"), "phases": result["phases"]}))
    sys.exit(0 if result["status"] == "passed" else 1)
