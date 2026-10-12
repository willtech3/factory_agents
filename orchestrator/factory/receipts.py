"""A prompt or rubric change is gated on evals (ADR-0016).

`evals/receipts.json` holds, per agent role, the sha256 of the prompt and rubrics it was last evaluated with and how:

- `replayed`: `evals/run_eval.py --recorded <dataset> --agent <role> --replay --receipt` asked the current agent version
  the recorded sessions again and found no regression against what the recorded versions produced (the summary is kept);
- `waived`: a person decided the change needs no eval (`factory eval waive --reason ...`), and the reason is kept.

`stale` is what CI checks (`python evals/gate.py`): a role whose prompt or rubric bytes differ from its receipt fails the
build until one of the two is recorded, so a prompt change is never merged on nobody's say-so.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RECEIPTS = Path("evals") / "receipts.json"
ROLE_FILES = {
    "decomposer": ("agents/decomposer.md", "rubrics/decomposition.md"),
    "spec_writer": ("agents/spec_writer.md", "rubrics/spec.md"),
    "spec_judge": ("agents/spec_judge.md", "rubrics/spec.md"),
    "implementer": ("agents/implementer.md", "rubrics/implementation.md"),
    "reviewer": ("agents/reviewer.md", "rubrics/review_quality.md"),
    "security_reviewer": ("agents/security_reviewer.md",),
    "merge_judge": ("agents/merge_judge.md", "rubrics/review_quality.md"),
}
DECISIONS = ("replayed", "waived")


def fingerprints(root: Path, role: str) -> dict[str, str]:
    return {rel: hashlib.sha256((root / rel).read_bytes()).hexdigest() for rel in ROLE_FILES[role]}


def load(root: Path) -> dict[str, Any]:
    path = root / RECEIPTS
    return json.loads(path.read_text()) if path.exists() else {}


def stale(root: Path) -> dict[str, list[str]]:
    """Role -> the files whose bytes its receipt does not cover (every file, when it has none)."""
    receipts = load(root)
    out = {}
    for role in ROLE_FILES:
        have = (receipts.get(role) or {}).get("files") or {}
        changed = [rel for rel, sha in fingerprints(root, role).items() if have.get(rel) != sha]
        if changed:
            out[role] = changed
    return out


def record(root: Path, role: str, decision: str, **details: Any) -> dict[str, Any]:
    """Write `role`'s receipt for the prompt and rubric bytes as they are now."""
    if decision not in DECISIONS:
        raise ValueError(f"decision is one of {DECISIONS}")
    if decision == "waived" and not str(details.get("reason") or "").strip():
        raise ValueError("a waiver needs its reason")
    receipts = load(root)
    receipts[role] = {"files": fingerprints(root, role), "decision": decision,
                      "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"), **details}
    path = root / RECEIPTS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(receipts.items())), indent=2) + "\n")
    return receipts[role]


__all__ = ["DECISIONS", "RECEIPTS", "ROLE_FILES", "fingerprints", "load", "record", "stale"]
