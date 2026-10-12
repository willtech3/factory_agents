"""Persistence for runs: one SQLite database per run for its events and steps, plus an artifacts directory.

Layout (relative to FACTORY_RUNS_DIR, default ./runs):

    runs/<run_id>/run.db                            # events and steps (SQLite, WAL)
    runs/<run_id>/artifacts/<stage>/<name>          # raw files fetched from sessions
    runs/<run_id>/artifacts/<stage>/<name>.sha256
    runs/<run_id>/blobs/<sha256>                    # content-addressed session outputs a step recorded

Events are validated when they are written, not when they are read: `append` folds the new event into the run's
state inside one write transaction and refuses it (nothing is written) when the reducer does. Before this, an illegal
event was appended and every later read raised, so one bad write made the run unreadable everywhere (run_8d9b17b4f5).
SQLite serialises writers across threads and processes (the spec writers and reviewer passes append from worker
threads; the Console and `factory resume` from other processes), which replaces the old flock and cached-seq logic
(docs/postmortems/2026-10-10-first-live-runs.md issue 2).

A run written by an older controller has `events.jsonl` and no database: the first open imports it as it is, once, and
leaves the file renamed `events.imported.jsonl` beside the database. `factory export-events` writes the JSONL form back
out (the eval reference runs keep that format).

Steps (`steps` table) are the durable units of a run: today one per agent session (`stages/common.session_step`). A
step records the session it started before waiting on it, so a controller that dies is not the end of the session: the
next controller reattaches by id when it asks for the same work again. A finished step keeps how its session ended and
its outputs as blobs; it is not reused yet. docs/adr/0013-durable-runs.md has the design and the phases that follow.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .state import RunEvent, RunState, reduce, replay

DB_NAME = "run.db"
LEGACY_LOG = "events.jsonl"
IMPORTED_LOG = "events.imported.jsonl"
BUSY_TIMEOUT_S = 30.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, at TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL,
    stage TEXT, work_item_id TEXT, spec_id TEXT, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    key TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, attempt INTEGER NOT NULL, inputs_sha TEXT NOT NULL,
    session_id TEXT, result TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""

STEP_STATUSES = ("running", "done", "failed")


def run_exists(runs_dir: Path, run_id: str) -> bool:
    """`RunStore.exists` without a store: the run has an event log. Never creates anything."""
    d = runs_dir / run_id
    return (d / DB_NAME).is_file() or (d / LEGACY_LOG).is_file()


class ArtifactConflict(FileExistsError):
    """An artefact name already holds different content. Artefacts are named per round and a human may have decided
    on them, so the store never overwrites silently (live run 2's second Gate 2 cycle overwrote the spec and verdict the
    owner had just reviewed; docs/postmortems/2026-10-10-first-live-runs.md issue 1)."""


@dataclass
class Step:
    """One durable unit of a run. `attempt` counts the sessions (or executions) the step has started; a step that is
    re-entered with different inputs, or after it failed, starts a new attempt."""
    key: str
    kind: str
    status: str
    attempt: int
    inputs_sha: str
    session_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_event(row: sqlite3.Row, run_id: str) -> RunEvent:
    return RunEvent(run_id=run_id, seq=row["seq"], kind=row["kind"], actor=json.loads(row["actor"]),
                    payload=json.loads(row["payload"]), stage=row["stage"], work_item_id=row["work_item_id"],
                    spec_id=row["spec_id"], at=row["at"], event_id=row["event_id"])


def _row_step(row: sqlite3.Row) -> Step:
    return Step(key=row["key"], kind=row["kind"], status=row["status"], attempt=row["attempt"], inputs_sha=row["inputs_sha"],
                session_id=row["session_id"], result=json.loads(row["result"]) if row["result"] else None,
                error=row["error"], created_at=row["created_at"], updated_at=row["updated_at"])


class RunStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._states: dict[str, RunState] = {}          # run_id -> state as of its last_seq (a cache; the database decides)
        self._guard = threading.Lock()

    # ----- the database -----
    def _run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def db_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / DB_NAME

    def exists(self, run_id: str) -> bool:
        """The run has an event log (a database, or a legacy JSONL log not imported yet). Never creates anything."""
        return run_exists(self.root, run_id)

    @contextmanager
    def _db(self, run_id: str, *, create: bool = True) -> Iterator[sqlite3.Connection | None]:
        """A connection to the run's database (None when it has none and `create` is false). One connection per call:
        sqlite3 connections are per thread, and opening one is cheap next to a session that takes minutes."""
        path = self.db_path(run_id)
        if not create and not path.is_file() and not (self._run_dir(run_id) / LEGACY_LOG).is_file():
            yield None
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
            self._import_legacy(conn, run_id)
            yield conn
        finally:
            conn.close()

    def _import_legacy(self, conn: sqlite3.Connection, run_id: str) -> None:
        """Import an older controller's events.jsonl once, as it is (history is not re-validated; replay still reads it)."""
        legacy = self._run_dir(run_id) / LEGACY_LOG
        if not legacy.is_file():
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("SELECT 1 FROM events LIMIT 1").fetchone() is None:
                for line in legacy.read_text().splitlines():
                    if line.strip():
                        conn.execute(*_insert(RunEvent(**json.loads(line))))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        try:
            legacy.rename(legacy.with_name(IMPORTED_LOG))
        except OSError:
            pass                                          # another process renamed it first; the events are in the database

    # ----- events -----
    def events(self, run_id: str) -> list[RunEvent]:
        with self._db(run_id, create=False) as conn:
            if conn is None:
                return []
            rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        return [_row_event(r, run_id) for r in rows]

    def last_seq(self, run_id: str) -> int:
        """The newest event's seq, -1 for a run with no events: a cheap change key for readers that cache (the Console)."""
        with self._db(run_id, create=False) as conn:
            if conn is None:
                return -1
            row = conn.execute("SELECT MAX(seq) FROM events").fetchone()
        return -1 if row[0] is None else int(row[0])

    def append(self, run_id: str, kind: str, actor: dict[str, Any], payload: dict[str, Any] | None = None,
               **extra: Any) -> RunEvent:
        """Append one event with the next contiguous seq, or raise what the reducer raises and write nothing. Safe
        across threads and processes: the write transaction serialises appenders."""
        with self._db(run_id) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                state = self._state_locked(conn, run_id)
                ev = RunEvent(run_id=run_id, seq=state.last_seq + 1, kind=kind, actor=actor, payload=payload or {}, **extra)
                after = reduce(copy.deepcopy(state), ev)        # refuses an illegal transition before anything is written
                conn.execute(*_insert(ev))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        with self._guard:
            self._states[run_id] = after
        return ev

    def state(self, run_id: str) -> RunState:
        with self._db(run_id, create=False) as conn:
            if conn is None:
                return RunState(run_id=run_id)
            return copy.deepcopy(self._state_locked(conn, run_id))

    def _state_locked(self, conn: sqlite3.Connection, run_id: str) -> RunState:
        """The run's state as of its newest event: the cached state brought forward by the events after it."""
        with self._guard:
            cached = self._states.get(run_id)
        last = conn.execute("SELECT MAX(seq) FROM events").fetchone()[0]
        last = -1 if last is None else int(last)
        if cached is not None and cached.last_seq == last:
            return cached
        if cached is None or cached.last_seq > last:
            rows = conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
            state = replay(run_id, [_row_event(r, run_id) for r in rows])
        else:
            state = copy.deepcopy(cached)
            for r in conn.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq", (cached.last_seq,)).fetchall():
                reduce(state, _row_event(r, run_id))
        with self._guard:
            self._states[run_id] = state
        return state

    def list_runs(self) -> Iterable[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and self.exists(p.name))

    def export_events(self, run_id: str) -> str:
        """The event log as JSONL, one event per line in seq order (the format older runs and the eval datasets use)."""
        return "".join(json.dumps(ev.to_dict()) + "\n" for ev in self.events(run_id))

    # ----- steps -----
    def step(self, run_id: str, key: str) -> Step | None:
        with self._db(run_id, create=False) as conn:
            if conn is None:
                return None
            row = conn.execute("SELECT * FROM steps WHERE key = ?", (key,)).fetchone()
        return _row_step(row) if row else None

    def steps(self, run_id: str) -> list[Step]:
        with self._db(run_id, create=False) as conn:
            if conn is None:
                return []
            return [_row_step(r) for r in conn.execute("SELECT * FROM steps ORDER BY created_at, key").fetchall()]

    def begin_step(self, run_id: str, key: str, kind: str, inputs_sha: str) -> Step:
        """Start a new attempt of `key` (the first, or the next after the previous one failed or its inputs changed)."""
        now = _now()
        with self._db(run_id) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT attempt, created_at FROM steps WHERE key = ?", (key,)).fetchone()
                attempt = (row["attempt"] + 1) if row else 1
                conn.execute("INSERT OR REPLACE INTO steps (key, kind, status, attempt, inputs_sha, session_id, result, error, "
                             "created_at, updated_at) VALUES (?, ?, 'running', ?, ?, NULL, NULL, NULL, ?, ?)",
                             (key, kind, attempt, inputs_sha, row["created_at"] if row else now, now))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return Step(key=key, kind=kind, status="running", attempt=attempt, inputs_sha=inputs_sha, created_at=now, updated_at=now)

    def set_step_session(self, run_id: str, key: str, session_id: str) -> None:
        self._update_step(run_id, key, session_id=session_id)

    def finish_step(self, run_id: str, key: str, result: dict[str, Any]) -> None:
        self._update_step(run_id, key, status="done", result=json.dumps(result), error=None)

    def fail_step(self, run_id: str, key: str, error: str, result: dict[str, Any] | None = None) -> None:
        self._update_step(run_id, key, status="failed", error=error[:2000], result=json.dumps(result) if result is not None else None)

    def _update_step(self, run_id: str, key: str, **fields: Any) -> None:
        if "status" in fields and fields["status"] not in STEP_STATUSES:
            raise ValueError(f"step status {fields['status']!r}")
        sets = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
        with self._db(run_id) as conn:
            cur = conn.execute(f"UPDATE steps SET {sets} WHERE key = ?", (*fields.values(), _now(), key))
            if cur.rowcount != 1:
                raise KeyError(f"{run_id}: no step {key}")

    # ----- blobs (content-addressed; what a step's result refers to) -----
    def put_blob(self, run_id: str, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        p = self._run_dir(run_id) / "blobs" / digest
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_bytes(content)
            tmp.replace(p)
        return digest

    def get_blob(self, run_id: str, digest: str) -> bytes:
        content = (self._run_dir(run_id) / "blobs" / digest).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError(f"{run_id}: blob {digest[:12]} does not match its name")
        return content

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


def _insert(ev: RunEvent) -> tuple[str, tuple[Any, ...]]:
    return ("INSERT INTO events (seq, event_id, at, kind, actor, stage, work_item_id, spec_id, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ev.seq, ev.event_id, ev.at, ev.kind, json.dumps(ev.actor), ev.stage, ev.work_item_id, ev.spec_id,
             json.dumps(ev.payload)))

