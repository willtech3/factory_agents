"""Human gates and in-session questions.

Two kinds of human interaction:

1. **Gates** sit between stages. The orchestrator opens a gate with a frozen set of
   artifacts (identified by sha256), notifies the assignee, and waits. A decision is
   only accepted if it names the same artifact hashes the human was shown; this is the
   defence against "approve A, execute B" (Loopjacking, arXiv:2609.21081).
2. **Questions** come from inside a session via the `ask_human` custom tool. The
   session idles with `requires_action`; the orchestrator surfaces the question, waits,
   and returns the answer as a `user.custom_tool_result`.

Surfaces (Slack, GitLab design-MR comments, the Factory Console) implement `Notifier`
and `ApprovalQueue`. The in-memory implementations here are for tests and local use.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol
import uuid


class GateKind(str, Enum):
    DECOMPOSITION_REVIEW = "gate_1_decomposition"
    SPEC_READINESS = "gate_2_spec_readiness"
    ESCALATION = "escalation"             # judge or loop could not converge
    # Gate 3 (code review) is GitLab's own MR approval; see gitlab.py


class Decision(str, Enum):
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"   # comments go back to the producing agent as feedback.md
    REJECT = "reject"                     # closes the run (or the work item)
    APPROVE_SUBSET = "approve_subset"     # Gate 2 only: approve some specs, send others back


@dataclass(frozen=True)
class ArtifactRef:
    name: str
    sha256: str
    url: str | None = None                # where the human can read it (design MR file, console)


@dataclass
class Gate:
    run_id: str
    kind: GateKind
    artifacts: tuple[ArtifactRef, ...]
    assignees: tuple[str, ...]
    summary: str                           # the judge's or agent's summary_for_human
    options: tuple[Decision, ...]
    gate_id: str = field(default_factory=lambda: f"gate_{uuid.uuid4().hex[:10]}")
    opened_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    sla_hours: int = 24


@dataclass
class GateDecision:
    gate_id: str
    decision: Decision
    decided_by: str
    comments: str = ""
    approved_subset: tuple[str, ...] = ()  # spec_ids, for APPROVE_SUBSET
    artifact_shas: tuple[str, ...] = ()    # must equal the gate's artifact hashes
    decided_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class StaleDecision(Exception):
    """The decision references artifacts that differ from what the gate presented."""


def validate_decision(gate: Gate, decision: GateDecision) -> None:
    if decision.gate_id != gate.gate_id:
        raise ValueError("decision is for a different gate")
    if decision.decision not in gate.options:
        raise ValueError(f"{decision.decision} is not an option for this gate")
    shown = sorted(a.sha256 for a in gate.artifacts)
    if sorted(decision.artifact_shas) != shown:
        raise StaleDecision("artifact hashes in decision do not match what the gate presented")
    if decision.decision == Decision.REQUEST_CHANGES and not decision.comments.strip():
        raise ValueError("request_changes requires comments; they become the agent's feedback")
    if decision.decision == Decision.APPROVE_SUBSET and not decision.approved_subset:
        raise ValueError("approve_subset requires at least one approved spec_id")


@dataclass
class Question:
    run_id: str
    asked_by: str                          # agent role
    question: str
    why_it_matters: str
    default_if_unanswered: str
    blocking: bool
    options: tuple[str, ...] = ()
    session_id: str | None = None
    custom_tool_use_id: str | None = None
    question_id: str = field(default_factory=lambda: f"q_{uuid.uuid4().hex[:10]}")
    asked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class Answer:
    question_id: str
    answer: str
    answered_by: str
    answered_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Notifier(Protocol):
    def gate_opened(self, gate: Gate) -> None: ...
    def question_asked(self, q: Question) -> None: ...
    def mr_opened(self, run_id: str, spec_id: str, url: str) -> None: ...
    def escalated(self, run_id: str, reason: str) -> None: ...


class ApprovalQueue(Protocol):
    def open(self, gate: Gate) -> None: ...
    def decision(self, gate_id: str) -> GateDecision | None: ...
    def ask(self, q: Question) -> None: ...
    def answer(self, question_id: str) -> Answer | None: ...


class InMemoryQueue:
    """Used by tests and the CLI's local mode. Decisions are injected with `decide`/`reply`."""

    def __init__(self) -> None:
        self.gates: dict[str, Gate] = {}
        self.decisions: dict[str, GateDecision] = {}
        self.questions: dict[str, Question] = {}
        self.answers: dict[str, Answer] = {}

    def open(self, gate: Gate) -> None:
        self.gates[gate.gate_id] = gate

    def decision(self, gate_id: str) -> GateDecision | None:
        d = self.decisions.get(gate_id)
        if d is not None:
            validate_decision(self.gates[gate_id], d)
        return d

    def decide(self, d: GateDecision) -> None:
        validate_decision(self.gates[d.gate_id], d)
        self.decisions[d.gate_id] = d

    def ask(self, q: Question) -> None:
        self.questions[q.question_id] = q

    def answer(self, question_id: str) -> Answer | None:
        return self.answers.get(question_id)

    def reply(self, a: Answer) -> None:
        self.answers[a.question_id] = a


class LogNotifier:
    def gate_opened(self, gate: Gate) -> None:
        print(f"[gate] {gate.kind.value} opened for {gate.run_id}: {gate.summary[:120]}")

    def question_asked(self, q: Question) -> None:
        print(f"[question] {q.asked_by} asks: {q.question}")

    def mr_opened(self, run_id: str, spec_id: str, url: str) -> None:
        print(f"[mr] {spec_id}: {url}")

    def escalated(self, run_id: str, reason: str) -> None:
        print(f"[escalated] {run_id}: {reason}")
