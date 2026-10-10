"""Persistence for runs: an append-only JSONL event log plus an artifacts directory.

Layout (relative to FACTORY_RUNS_DIR, default ./runs):

    runs/<run_id>/events.jsonl
    runs/<run_id>/artifacts/<stage>/<name>          # raw files fetched from sessions
    runs/<run_id>/artifacts/<stage>/<name>.sha256

SQLite or Postgres would replace this in production; the interface is small on purpose.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .state import RunEvent, RunState, replay


class ArtifactConflict(FileExistsError):
    """An artefact name already holds different content. Artefacts are named per round and a human may have decided
    on them, so the store never overwrites silently (live run 2's second Gate 2 cycle overwrote the spec and verdict the
    owner had just reviewed; docs/postmortems/2026-10-10-first-live-runs.md issue 1)."""


class RunStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    # ----- events -----
    def _events_path(self, run_id: str) -> Path:
        p = self.root / run_id / "events.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def events(self, run_id: str) -> list[RunEvent]:
        p = self._events_path(run_id)
        if not p.exists():
            return []
        out = []
        for line in p.read_text().splitlines():
            if line.strip():
                d = json.loads(line)
                out.append(RunEvent(**d))
        return out

    def append(self, run_id: str, kind: str, actor: dict[str, Any], payload: dict[str, Any] | None = None,
               **extra: Any) -> RunEvent:
        existing = self.events(run_id)
        seq = existing[-1].seq + 1 if existing else 0
        ev = RunEvent(run_id=run_id, seq=seq, kind=kind, actor=actor, payload=payload or {}, **extra)
        with self._events_path(run_id).open("a") as f:
            f.write(json.dumps(ev.to_dict()) + "\n")
        return ev

    def state(self, run_id: str) -> RunState:
        return replay(run_id, self.events(run_id))

    def list_runs(self) -> Iterable[str]:
        return sorted(p.name for p in self.root.iterdir() if (p / "events.jsonl").exists())

    # ----- artifacts -----
    def artifact_path(self, run_id: str, stage: str, name: str) -> Path:
        p = self.root / run_id / "artifacts" / stage / name
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def put_artifact(self, run_id: str, stage: str, name: str, content: bytes, *, replace: bool = False) -> str:
        """Store `content` under `<stage>/<name>` and return its sha256.

        Writing the same bytes again is a no-op; writing different bytes under an existing name raises
        `ArtifactConflict` unless `replace=True`. Session outputs and gate artefacts are never replaced: they carry a
        round number in their name and a new round gets a new name. `replace` is for the orchestrator's own pointer
        and re-derived files (`final-*`, the re-verification of a head after a resume)."""
        p = self.artifact_path(run_id, stage, name)
        digest = hashlib.sha256(content).hexdigest()
        if p.exists() and not replace:
            existing = hashlib.sha256(p.read_bytes()).hexdigest()
            if existing == digest:
                return digest
            raise ArtifactConflict(f"{run_id}: {stage}/{name} already holds different content "
                                   f"(sha256 {existing[:12]} on disk, {digest[:12]} offered); artefacts are not overwritten")
        p.write_bytes(content)
        p.with_suffix(p.suffix + ".sha256").write_text(digest)
        return digest

    def get_artifact(self, run_id: str, stage: str, name: str) -> bytes:
        return self.artifact_path(run_id, stage, name).read_bytes()

    def get_json(self, run_id: str, stage: str, name: str) -> Any:
        return json.loads(self.get_artifact(run_id, stage, name))

    def sha256(self, run_id: str, stage: str, name: str) -> str:
        return self.artifact_path(run_id, stage, name).with_suffix(
            Path(name).suffix + ".sha256").read_text().strip()
