"""What a Gate 2 approval may select, and the owner's notes that travel with it to implementation.

One place for the rules every decision surface (the Console, `factory decide`) and the runner apply alike:

- `subset_problems`: approve_subset must be closed under dependencies among the specs the gate presented. A selected
  spec whose prerequisite was presented but left out would otherwise be built as if that prerequisite were already on
  the default branch (dependencies outside the run's specs still are, as before).
- `approval_guidance`: the comments on an approval, routed per spec (`spec_x:` or `wi-x:` lines to that spec, other
  lines to every spec) and bounded, so the implementer hears what the owner said without the spec changing.
- `presented_specs`: the exact specs a Gate 2 showed, found in the run store by the sha256 the gate presented.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MAX_GUIDANCE_CHARS = 4000
GUIDANCE_HEADER = ("# The owner's notes at Gate 2 approval\n"
                   "Guidance for building the approved spec. The spec stays authoritative: these notes never change its acceptance "
                   "criteria, allowed paths or test plan. Where a note conflicts with the spec, follow the spec and say so in "
                   "implementation_report.json notes.\n\n")


def subset_problems(specs: dict[str, dict[str, Any]], approved: Iterable[str]) -> list[str]:
    """Why `approved` is not a selection the gate can accept, or [] when it is: every selected spec was presented, and every
    prerequisite a selected spec names among the presented specs is selected too."""
    chosen = list(dict.fromkeys(approved))
    if not chosen:
        return ["select at least one spec"]
    problems = []
    unknown = [sid for sid in chosen if sid not in specs]
    if unknown:
        problems.append(f"not presented at this gate: {', '.join(unknown)}")
    selected = set(chosen)
    for sid in chosen:
        if sid not in specs:
            continue
        needs = [d for d in ((specs[sid].get("dependencies") or {}).get("specs") or []) if d in specs and d not in selected]
        if needs:
            problems.append(f"{sid} depends on {', '.join(needs)}, presented at this gate but not selected")
    return problems


def approval_guidance(comments: str, specs: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Per approved spec, the owner's approval notes as markdown: lines addressed `spec_x:` or `wi-x:` go to that spec,
    other lines to every spec. Each is capped at MAX_GUIDANCE_CHARS; specs with nothing to hear are left out."""
    if not (comments or "").strip():
        return {}
    by_item = {s.get("work_item_id"): sid for sid, s in specs.items()}
    general: list[str] = []
    addressed: dict[str, list[str]] = {sid: [] for sid in specs}
    for raw in comments.splitlines():
        line = raw.strip()
        if not line:
            continue
        head, sep, rest = line.partition(":")
        target = head.strip() if head.strip() in specs else by_item.get(head.strip())
        if sep and target:
            if rest.strip():
                addressed[target].append(rest.strip())
        elif sep and head.strip().startswith(("spec_", "wi-")):
            # Notes addressed to an unselected contract must not become instructions
            # for every selected contract after approve_subset narrows the set.
            continue
        else:
            general.append(line)
    out = {}
    for sid in specs:
        lines = general + addressed[sid]
        if not lines:
            continue
        body = "\n".join(f"- {line}" for line in lines)
        if len(body) > MAX_GUIDANCE_CHARS:
            body = body[:MAX_GUIDANCE_CHARS].rstrip() + f"\n- (notes cut at {MAX_GUIDANCE_CHARS} characters; the full text is in the gate decision)"
        out[sid] = GUIDANCE_HEADER + body + "\n"
    return out


def presented_specs(runs_dir: Path, run_id: str, artifacts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The specs a Gate 2 presented, read back by the sha256 it showed: `specify/final-<spec_id>.json` when it still holds
    those bytes, else any stored spec with that hash. ValueError when one cannot be found."""
    folder = runs_dir / run_id / "artifacts" / "specify"
    out = {}
    for a in artifacts:
        name, sha = a.get("name", ""), a.get("sha256", "")
        if not name.endswith(".json") or name == "verdict.json":
            continue
        sid = name[:-len(".json")]
        candidates = [folder / f"final-{sid}.json", *sorted(p for p in folder.glob("*.json") if p.name != f"final-{sid}.json")] if folder.is_dir() else []
        content = next((p.read_bytes() for p in candidates if p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest() == sha), None)
        if content is None:
            raise ValueError(f"the spec {sid} this gate presented (sha256 {sha[:12]}) is not in the run store")
        out[sid] = json.loads(content)
    return out
