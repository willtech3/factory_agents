#!/usr/bin/env python3
"""Controller-owned lifecycle harness, executed only in a hosted Linux sandbox.

No provider calls or host paths. The controller uploads an immutable Git archive,
profile, and this harness. Raw process logs stay in the sandbox; the downloadable
report contains statuses and media, not command output or environment values.

A request with a `change` section verifies one candidate head of a Factory run
(preview mode "managed"): after the recipe's prepare/start/seed and its baseline
verify, it runs the spec's test_plan commands (each pytest run summarised from its
JUnit report as node ids by outcome), pytest collection, and the spec's e2e entries
against the app inside this sandbox, each visual entry with the controller-issued
browser request and a fresh evidence directory. The baseline check shows the app
runs; it is reported separately and is never evidence for a spec criterion.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

UPLOADS = Path("/mnt/session/uploads")
OUTPUTS = Path("/mnt/session/outputs")
SHELL = ["/bin/bash", "-lc"]          # a login shell, as the recipe author tested it in the sandbox
NODE_ID_RE = re.compile(r"^[^\s:]+::\S+$")
MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".webm", ".mp4", ".gif"}
MAX_NODE_IDS = 20000


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def command(text: str, cwd: Path, env: dict, timeout: float, log: Path) -> dict:
    if timeout <= 0:
        log.write_text("")
        return {"status": "not_run", "reason": "environment deadline exceeded"}
    started = time.monotonic()
    with log.open("wb") as output:
        proc = subprocess.Popen([*SHELL, text], cwd=cwd, env=env,
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


def parse_junit(xml: bytes) -> dict:
    """Node ids by outcome from a pytest JUnit report (junit_family=xunit1); never failure text or output."""
    out = {"passed": [], "failed": [], "skipped": []}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    for tc in root.iter("testcase"):
        node_id = node_id_of(tc.get("file"), tc.get("classname", ""), tc.get("name", ""))
        tags = {child.tag for child in tc}
        key = "failed" if tags & {"failure", "error"} else "skipped" if "skipped" in tags else "passed"
        if len(out[key]) < MAX_NODE_IDS:
            out[key].append(node_id)
    return out


def node_id_of(file, classname: str, name: str) -> str:
    if not file:
        parts = [p for p in classname.split(".") if p]
        return f"{'/'.join(parts)}.py::{name}" if parts else name
    module = Path(file).with_suffix("").as_posix().replace("/", ".")
    if classname == module or not classname:
        chain = []
    elif classname.startswith(module + "."):
        chain = classname[len(module) + 1:].split(".")
    else:
        parts = classname.split(".")
        stem = Path(file).stem
        chain = parts[len(parts) - parts[::-1].index(stem):] if stem in parts else []
    return "::".join([file, *chain, name])


def copy_entry_evidence(index: int, directory: Path, outputs: Path, report: dict) -> list[str]:
    """A visual entry's result.json and media, as e2e-<index>-<name>; links, odd names and oversize files stay behind."""
    names = []
    for p in sorted(directory.iterdir()):
        if p.name == "request.json" or p.is_symlink() or not p.is_file():
            continue
        if p.name != "result.json" and (p.suffix.lower() not in MEDIA_SUFFIXES or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}", p.name)):
            continue
        size = p.stat().st_size
        total = report.get("_uploaded_bytes", 0)
        if size > 30 * 1024 * 1024 or total + size > 64 * 1024 * 1024:
            continue
        report["_uploaded_bytes"] = total + size
        shutil.copyfile(p, outputs / f"e2e-{index}-{p.name}")
        names.append(p.name)
    return names


def run_change(profile: dict, change: dict, repo: Path, env: dict, deadline: float, logs: Path, evidence: Path,
               work: Path, outputs: Path, report: dict) -> None:
    phases = report["phases"]
    out = {"commands": [], "collected_tests": None, "e2e": [], "baseline": None, "app_ready": False}
    report["change"] = {**change, "results": out}
    commands, entries = change.get("commands", []), change.get("e2e", [])
    per_command = float(change.get("command_timeout_seconds", 900))
    prepared = command(profile["commands"]["prepare"], repo, env, deadline - time.monotonic(), logs / "prepare.log")
    phases.append({"phase": "prepare", **prepared})
    if prepared["status"] != "passed":
        out["commands"] = [{"index": i, "command": c, "status": "not_run", "reason": "the recipe's prepare phase did not pass"} for i, c in enumerate(commands)]
        out["e2e"] = [{"index": e["index"], "status": "not_run", "reason": "the recipe's prepare phase did not pass"} for e in entries]
        report.update(status="failed", reason="prepare did not pass")
        return
    started = command(profile["commands"]["start"], repo, env, deadline - time.monotonic(), logs / "start.log")
    phases.append({"phase": "start", **started})
    ready = started["status"] == "passed"
    if ready:
        report["services"] = health(profile["services"], min(deadline, time.monotonic() + 90))
        ready = all(s["status"] == "passed" for s in report["services"])
    if ready:
        seeded = command(profile["commands"]["seed"], repo, env, deadline - time.monotonic(), logs / "seed.log")
        phases.append({"phase": "seed", **seeded})
        ready = seeded["status"] == "passed"
    out["app_ready"] = ready
    if ready:
        verified = command(profile["commands"]["verify"], repo, env, deadline - time.monotonic(), logs / "verify.log")
        phases.append({"phase": "verify", **verified})
        ok, detail = check_evidence(profile, evidence) if verified["status"] == "passed" else (False, "baseline verify command did not pass")
        out["baseline"] = {"status": "passed" if ok else "failed", "detail": detail}
    else:
        out["baseline"] = {"status": "blocked", "detail": "the app did not start or its services did not become ready"}
    junit_dir = work / "junit"
    junit_dir.mkdir(exist_ok=True)
    for i, text in enumerate(commands):
        junit = junit_dir / f"cmd-{i}.xml"
        addopts = f"{env.get('PYTEST_ADDOPTS', '')} --junitxml={shlex.quote(str(junit))} -o junit_family=xunit1".strip()
        result = command(text, repo, {**env, "PYTEST_ADDOPTS": addopts}, min(per_command, deadline - time.monotonic()), logs / f"change-cmd-{i}.log")
        row = {"index": i, "command": text, **result}
        if junit.is_file():
            row["pytest"] = parse_junit(junit.read_bytes())
        out["commands"].append(row)
    if change.get("collect_tests"):
        log = logs / "collect.log"
        result = command('PY="$(command -v python || command -v python3)"; "$PY" -m pytest --collect-only -q', repo, env, min(per_command, deadline - time.monotonic()), log)
        ids = [ln.strip() for ln in log.read_text(errors="replace").splitlines() if NODE_ID_RE.match(ln.strip())][:MAX_NODE_IDS]
        out["collected_tests"] = {"exit_code": result.get("exit_code"), "status": result["status"], "node_ids": ids}
    for entry in entries:
        index = int(entry["index"])
        if not ready and (entry.get("target", "preview") == "preview" or entry.get("visual_request")):
            out["e2e"].append({"index": index, "status": "not_run", "reason": "the app did not start or its services did not become ready"})
            continue
        directory = work / f"e2e-{index}"
        directory.mkdir()
        entry_env = {**env, "FACTORY_EVIDENCE_DIR": str(directory)}      # never the baseline's evidence directory
        if entry.get("visual_request"):
            (directory / "request.json").write_text(json.dumps(entry["visual_request"]))
            entry_env["FACTORY_VISUAL_REQUEST"] = str(directory / "request.json")
        result = command(entry["command"], repo, entry_env, min(per_command, deadline - time.monotonic()), logs / f"e2e-{index}.log")
        row = {"index": index, "command": entry["command"], "target": entry.get("target", "preview"), **result}
        if entry.get("visual_request"):
            row["files"] = copy_entry_evidence(index, directory, outputs, report)
        out["e2e"].append(row)
    report.update(status="completed", reason="change verification ran; the controller judges each row")


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
        if request.get("change"):
            run_change(profile, request["change"], repo, env, deadline, logs, evidence, work, outputs, report)
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
        total = report.get("_uploaded_bytes", 0)
        report.pop("_uploaded_bytes", None)
        for p in sorted(evidence.iterdir()):
            if request.get("change") and p.name not in {"browser-result.json", "verification-result.json"}:
                continue        # change runs: baseline media is not per-change evidence; keep only its result files
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
