"""The file-backed gate surface: JSON files under `runs/_queue/` that two processes share.

`factory run` opens gates and questions here; `factory decide` / `factory answer` and the Factory Console
(`factory web`) write decisions and answers here. It is the `file` (alias `web`) gate surface and is
always composed in alongside the tracker, so a decision from any side wins (ADR-0007).
"""
from __future__ import annotations

import json
import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..gates import Answer, ArtifactRef, Decision, Gate, GateDecision, GateKind, InMemoryQueue, Question, validate_decision


class FileQueue(InMemoryQueue):
    """InMemoryQueue persisted as JSON files so a decision made in another process is seen by `factory run`."""

    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _load(self, name: str) -> dict:
        p = self.root / name
        return json.loads(p.read_text()) if p.exists() else {}

    def _save(self, name: str, data: dict) -> None:
        """Write to a temp file and rename, so a poll from the other process never sees a truncated file."""
        import os, tempfile
        fd, tmp = tempfile.mkstemp(prefix=f".{name}.", dir=self.root)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data, indent=2, default=lambda o: getattr(o, "value", o.__dict__)))
        os.replace(tmp, self.root / name)

    @contextmanager
    def _writing(self, name: str):
        # Lock a stable inode, not the JSON file that _save atomically replaces.
        # A separately opened descriptor also serializes threads and queue instances.
        with (self.root / f".{name}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _put(self, name: str, key: str, entry: dict) -> None:
        with self._writing(name):
            data = self._load(name)
            data[key] = entry
            self._save(name, data)

    # ---- gates
    def open(self, gate: Gate) -> None:
        super().open(gate)
        self._put("gates.json", gate.gate_id, gate_record(gate))

    def gate(self, gate_id: str) -> Gate | None:
        """The gate as opened, rebuilt from gates.json when this process did not open it."""
        if gate_id in self.gates:
            return self.gates[gate_id]
        rec = self._load("gates.json").get(gate_id)
        if rec is None:
            return None
        g = gate_from_record(gate_id, rec)
        self.gates[gate_id] = g
        return g

    def decision(self, gate_id: str) -> GateDecision | None:
        d = self._load("decisions.json").get(gate_id)
        if d is None:
            return None
        dec = GateDecision(gate_id=gate_id, decision=Decision(d["decision"]), decided_by=d["decided_by"],
                           comments=d.get("comments", ""), approved_subset=tuple(d.get("approved_subset", [])),
                           artifact_shas=tuple(d.get("artifact_shas", [])), decided_at=d.get("decided_at") or "")
        if self.gate(gate_id) is None:
            return None
        self.decisions[gate_id] = dec
        return super().decision(gate_id)

    def record_decision(self, gate_id: str, decision: str, by: str, comments: str = "",
                        approved_subset: list[str] | None = None) -> dict[str, Any]:
        """What `factory decide` and the Console do: bind the hashes the gate showed and persist the decision."""
        from datetime import datetime, timezone

        rec = self._load("gates.json").get(gate_id)
        if rec is None:
            raise KeyError(gate_id)
        entry = {"decision": decision, "decided_by": by, "comments": comments, "approved_subset": approved_subset or [],
                 "artifact_shas": [a["sha256"] for a in rec["artifacts"]],
                 "decided_at": datetime.now(timezone.utc).isoformat()}
        validate_decision(gate_from_record(gate_id, rec), GateDecision(
            gate_id=gate_id, decision=Decision(decision), decided_by=by, comments=comments,
            approved_subset=tuple(approved_subset or ()), artifact_shas=tuple(entry["artifact_shas"])))
        self._put("decisions.json", gate_id, entry)
        return entry

    # ---- questions
    def ask(self, q: Question) -> None:
        super().ask(q)
        entry = {"run_id": q.run_id, "asked_by": q.asked_by, "question": q.question, "why": q.why_it_matters,
                            "default": q.default_if_unanswered, "blocking": q.blocking, "options": list(q.options),
                            "asked_at": q.asked_at, "work_item_id": q.work_item_id}
        self._put("questions.json", q.question_id, entry)

    def answer(self, question_id: str) -> Answer | None:
        a = self._load("answers.json").get(question_id)
        return Answer(question_id=question_id, answer=a["answer"], answered_by=a["answered_by"],
                      answered_at=a.get("answered_at") or "") if a else None

    def record_answer(self, question_id: str, answer: str, by: str) -> dict[str, Any]:
        from datetime import datetime, timezone

        if question_id not in self._load("questions.json"):
            raise KeyError(question_id)
        entry = {"answer": answer, "answered_by": by, "answered_at": datetime.now(timezone.utc).isoformat()}
        self._put("answers.json", question_id, entry)
        return entry

    # ---- views
    def open_gates(self) -> dict[str, dict[str, Any]]:
        decided = self._load("decisions.json")
        out = {}
        for gid, g in self._load("gates.json").items():
            out[gid] = {**g, "gate_id": gid, "recorded": decided.get(gid)}
        return out

    def open_questions(self) -> dict[str, dict[str, Any]]:
        answered = self._load("answers.json")
        return {qid: {**q, "question_id": qid, "answered": answered.get(qid)} for qid, q in self._load("questions.json").items()}


def gate_record(gate: Gate) -> dict[str, Any]:
    return {"kind": gate.kind.value, "run_id": gate.run_id, "summary": gate.summary,
            "artifacts": [a.__dict__ for a in gate.artifacts], "options": [o.value for o in gate.options],
            "assignees": list(gate.assignees), "opened_at": gate.opened_at, "work_item_id": gate.work_item_id}


def gate_from_record(gate_id: str, rec: dict[str, Any]) -> Gate:
    return Gate(run_id=rec["run_id"], kind=GateKind(rec["kind"]),
                artifacts=tuple(ArtifactRef(name=a["name"], sha256=a["sha256"], url=a.get("url")) for a in rec["artifacts"]),
                assignees=tuple(rec.get("assignees", [])), summary=rec.get("summary", ""),
                options=tuple(Decision(o) for o in rec["options"]), gate_id=gate_id,
                opened_at=rec.get("opened_at", ""), work_item_id=rec.get("work_item_id"))
