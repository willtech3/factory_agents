"""Deterministic security pre-checks the orchestrator runs before the security reviewer sees a change.

They are cheap, exact and tool-dependent: a secret scan over the added lines (always), a dependency audit and a
SAST pass when the scanners exist on the orchestrator host. Their output is uploaded to the security reviewer as
`security_prechecks.json` (inputs to judge, not verdicts) and the secret scan is also a merge check that nobody
can override: a committed credential is a blocking defect whatever a reviewer thinks of it.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

# High-confidence credential shapes: a match is a failed check. (Vendor prefixes are stable and documented.)
HIGH_CONFIDENCE = {
    "aws_access_key": re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github_token": re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b"),
    "github_fine_grained": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{80,}\b"),
    "slack_token": re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b"),
    "anthropic_key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    "openai_key": re.compile(r"\bsk-(proj-)?[A-Za-z0-9]{32,}\b"),
    "stripe_key": re.compile(r"\b(sk|rk)_(live|test)_[0-9A-Za-z]{20,}\b"),
    "gcp_service_account": re.compile(r'"type"\s*:\s*"service_account"'),
    "private_key_block": re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
}
# Suspect shapes: reported to the reviewer, never a failed check on their own (too many false positives in tests).
SUSPECT = re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token|auth)\b\s*[:=]\s*['\"][^'\"]{12,}['\"]")


def secret_scan(added_lines_by_file: dict[str, list[str]]) -> dict[str, Any]:
    """High-confidence hits (fail) and suspect assignments (report) in the lines a change adds."""
    hits: list[dict[str, Any]] = []
    suspects: list[dict[str, Any]] = []
    for path, lines in added_lines_by_file.items():
        for n, line in enumerate(lines, 1):
            for kind, rx in HIGH_CONFIDENCE.items():
                if rx.search(line):
                    hits.append({"file": path, "added_line_index": n, "kind": kind, "excerpt": _redact(line)})
            if SUSPECT.search(line) and not any(h["file"] == path and h["added_line_index"] == n for h in hits):
                suspects.append({"file": path, "added_line_index": n, "excerpt": _redact(line)})
    return {"hits": hits, "suspects": suspects}


def _redact(line: str) -> str:
    """Enough to find the line, never the whole credential."""
    s = line.strip()
    return s[:24] + ("…" if len(s) > 24 else "")


def dependency_audit(repo: Path, timeout: int = 180) -> dict[str, Any]:
    """Known-vulnerable dependencies via osv-scanner or pip-audit when present; skipped otherwise (and offline)."""
    if shutil.which("osv-scanner"):
        return _run("osv-scanner", ["osv-scanner", "--format", "json", "-r", str(repo)], timeout, ok_codes=(0, 1))
    if shutil.which("pip-audit"):
        return _run("pip-audit", ["pip-audit", "-f", "json", "-r", str(repo / "requirements.txt")] if (repo / "requirements.txt").is_file()
                    else ["pip-audit", "-f", "json"], timeout, ok_codes=(0, 1))
    return {"tool": None, "status": "skipped", "reason": "no dependency scanner on PATH (osv-scanner or pip-audit)"}


def sast(repo: Path, changed_files: list[str], timeout: int = 300) -> dict[str, Any]:
    """Static analysis of the changed files via semgrep or bandit when present; skipped otherwise."""
    py = [f for f in changed_files if f.endswith(".py") and (repo / f).is_file()]
    if shutil.which("semgrep"):
        return _run("semgrep", ["semgrep", "--config", "auto", "--json", "--quiet", *(changed_files or ["."])], timeout, cwd=repo, ok_codes=(0, 1))
    if shutil.which("bandit") and py:
        return _run("bandit", ["bandit", "-q", "-f", "json", *py], timeout, cwd=repo, ok_codes=(0, 1))
    return {"tool": None, "status": "skipped", "reason": "no SAST tool on PATH (semgrep or bandit)" if (py or not changed_files) else "no Python files changed"}


def _run(tool: str, cmd: list[str], timeout: int, cwd: Path | None = None, ok_codes=(0,)) -> dict[str, Any]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"tool": tool, "status": "error", "reason": str(e)[:200]}
    out: dict[str, Any] = {"tool": tool, "status": "ran" if p.returncode in ok_codes else "error", "exit_code": p.returncode,
                           "stderr_tail": p.stderr[-800:]}
    try:
        out["results"] = json.loads(p.stdout) if p.stdout.strip() else {}
    except json.JSONDecodeError:
        out["stdout_tail"] = p.stdout[-2000:]
    return out


MANIFESTS = ("pyproject.toml", "requirements", "uv.lock", "poetry.lock", "Pipfile", "package.json", "package-lock.json", "pnpm-lock.yaml",
             "yarn.lock", "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "Gemfile", "Dockerfile", "docker-compose", ".github/workflows/")
DANGEROUS = re.compile(r"pickle\.loads?\(|marshal\.loads?\(|yaml\.(load|unsafe_load)\(|jsonpickle\.decode|\beval\(|\bexec\(|shell=True|"
                       r"os\.(system|popen)\(|subprocess\.(call|run|Popen)\(.*shell|importlib\.import_module\(|\.from_statement\(|text\(f?\"|"
                       r"cursor\.execute\(f|\.execute\(.*%|\.format\(.*(SELECT|INSERT|UPDATE|DELETE)|autoescape\s*=\s*False|jinja2\.Template\(|"
                       r"verify\s*=\s*False|allow_origins\s*=\s*\[\s*[\"']\*|debug\s*=\s*True|hashlib\.(md5|sha1)\(|random\.(random|randint|choice)\(|"
                       r"\betree\.|lxml|re\.compile\((?!r?[\"'])|follow_redirects\s*=\s*True|send_file\(|FileResponse\(|open\(.*request")


def dependency_delta(added_lines_by_file: dict[str, list[str]], deleted_files: list[str] | None = None) -> dict[str, Any]:
    """Added lines in manifests, lockfiles, Dockerfiles and workflows: the supply-chain surface this change touches."""
    touched = {f: lines for f, lines in added_lines_by_file.items() if any(m in f for m in MANIFESTS)}
    return {"files": sorted(touched), "added_lines": {f: lines[:200] for f, lines in touched.items()},
            "deleted_manifest_files": [f for f in (deleted_files or []) if any(m in f for m in MANIFESTS)]}


def dangerous_api_grep(added_lines_by_file: dict[str, list[str]]) -> list[dict[str, Any]]:
    """A mechanical starting list of sinks in the added lines; candidates for the reviewer, never findings."""
    hits = []
    for path, lines in added_lines_by_file.items():
        for n, line in enumerate(lines, 1):
            m = DANGEROUS.search(line)
            if m:
                hits.append({"file": path, "added_line_index": n, "pattern": m.group(0)[:40], "excerpt": line.strip()[:160]})
    return hits[:300]


def prechecks(repo: Path, changed_files: list[str], added_lines_by_file: dict[str, list[str]], deleted_files: list[str] | None = None) -> dict[str, Any]:
    return {"secret_scan": secret_scan(added_lines_by_file), "dependency_delta": dependency_delta(added_lines_by_file, deleted_files),
            "dependency_audit": dependency_audit(repo), "sast": sast(repo, changed_files), "dangerous_api_grep": dangerous_api_grep(added_lines_by_file),
            "note": "candidates to triage, never verdicts; a tool marked skipped means the reviewer covers that area by hand"}
