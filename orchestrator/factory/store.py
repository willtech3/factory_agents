"""Persistence for runs: an append-only JSONL event log plus an artifacts directory.

Layout (relative to FACTORY_RUNS_DIR, default ./runs):

    runs/<run_id>/events.jsonl
    runs/<run_id>/artifacts/<stage>/<name>          # raw files fetched from sessions
    runs/<run_id>/artifacts/<stage>/<name>.sha256

Event sequence numbers are assigned under a per-run `threading.Lock` (the spec writers and the reviewer passes
emit from worker threads) and an `fcntl.flock` on events.jsonl (the Console and `factory resume` append from other
processes). The last seq is cached per run and trusted only while the file is the size this process last left it,
so another process's appends are seen. Without this, five threads got seqs [1, 1, 1, 1, 2] and replay refused the
run (docs/postmortems/2026-10-10-first-live-runs.md issue 2).

SQLite or Postgres would replace this in production; the interface is small on purpose.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
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
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._tail: dict[str, tuple[int, int]] = {}      # run_id -> (file size after our last append, last seq)

    # ----- events -----
    def _events_path(self, run_id: str) -> Path:
        p = self.root / run_id / "events.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _lock(self, run_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(run_id, threading.Lock())

    def events(self, run_id: str) -> list[RunEvent]:
        p = self._events_path(run_id)
        if not p.exists():
            return []
        with p.open("r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)       # never read a line another process is half-way through writing
            try:
                text = f.read()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        out = []
        for line in text.splitlines():
            if line.strip():
                d = json.loads(line)
                out.append(RunEvent(**d))
        return out

    def append(self, run_id: str, kind: str, actor: dict[str, Any], payload: dict[str, Any] | None = None,
               **extra: Any) -> RunEvent:
        """Append one event with the next contiguous seq. Safe across threads of this process and across processes
        appending to the same run."""
        path = self._events_path(run_id)
        with self._lock(run_id), path.open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                size = os.fstat(f.fileno()).st_size
                cached = self._tail.get(run_id)
                last = cached[1] if cached and cached[0] == size else _last_seq(path, size)
                ev = RunEvent(run_id=run_id, seq=last + 1, kind=kind, actor=actor, payload=payload or {}, **extra)
                f.write(json.dumps(ev.to_dict()) + "\n")
                f.flush()
                self._tail[run_id] = (os.fstat(f.fileno()).st_size, ev.seq)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return ev

    def state(self, run_id: str) -> RunState:
        return replay(run_id, self.events(run_id))

    def list_runs(self) -> Iterable[str]:
        # Old Console versions persisted synthetic demos alongside real runs. Keep
        # their files recoverable without reclassifying them as live work after upgrade.
        return sorted(p.name for p in self.root.iterdir()
                      if (p / "events.jsonl").exists() and not (p / "demo.json").is_file())

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

    def put_derived_artifact(self, run_id: str, stage: str, name: str, content: bytes) -> str:
        """Refresh controller-derived evidence, retaining every version by content hash.

        Raw agent outputs and approved contracts still use immutable put_artifact.
        A resumed verification may legitimately produce new evidence for the same round.
        """
        allowed = ("-hosted_verification.json", "-deterministic_checks.json", "-test_results.json",
                   "-findings.merged.json")
        if not name.endswith(allowed):
            raise ValueError(f"not controller-derived evidence: {name}")
        path = self.artifact_path(run_id, stage, name)
        versions = [content]
        if path.exists():
            versions.insert(0, path.read_bytes())
        for version in versions:
            digest = hashlib.sha256(version).hexdigest()
            self.put_artifact(run_id, "history", f"{stage}/{name}/{digest}", version)
        return self.put_artifact(run_id, stage, name, content, replace=True)

    def get_json(self, run_id: str, stage: str, name: str) -> Any:
        return json.loads(self.get_artifact(run_id, stage, name))

    def sha256(self, run_id: str, stage: str, name: str) -> str:
        return self.artifact_path(run_id, stage, name).with_suffix(
            Path(name).suffix + ".sha256").read_text().strip()


def _last_seq(path: Path, size: int) -> int:
    """The seq of the last event line in the file (-1 when empty), read from the tail without loading the whole log.
    Called under the lock, so the last line is complete; the chunk is widened until it holds that whole line."""
    chunk = 1 << 16
    with path.open("rb") as f:
        while True:
            start = max(0, size - chunk)
            f.seek(start)
            tail = f.read(size - start).rstrip(b"\n")
            cut = tail.rfind(b"\n")
            if not tail.strip():
                if start == 0:
                    return -1
            elif cut != -1 or start == 0:
                return int(json.loads(tail[cut + 1:])["seq"])
            chunk *= 4
