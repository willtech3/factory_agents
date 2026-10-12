"""Human gates and in-session questions.

Two kinds of human interaction:

1. **Gates** sit between stages. The orchestrator opens a gate with a frozen set of
   artifacts (identified by sha256), notifies the assignee, and waits. A decision is
   only accepted if it names the same artifact hashes the human was shown; this is the
   defence against "approve A, execute B" (Loopjacking, arXiv:2609.21081).
2. **Questions** come from inside a session via the `ask_human` custom tool. The
   session idles with `requires_action`; the orchestrator surfaces the question, waits,
   and returns the answer as a `user.custom_tool_result`.

Surfaces implement `Notifier` and `ApprovalQueue`: the file queue behind the CLI, the tracker
(`surfaces/tracker.py`: Linear today), the Factory Console over the file queue, and
later Slack. `CompositeQueue` lets several surfaces answer the same gate, so a
decision can come from the CLI or from Linear. The in-memory implementations here are for tests
and local use. See docs/adr/0007-pluggable-gate-surfaces.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol
import uuid


class GateKind(str, Enum):
    DECOMPOSITION_REVIEW = "gate_1_decomposition"
    SPEC_READINESS = "gate_2_spec_readiness"
    ESCALATION = "escalation"             # judge or loop could not converge
    BUDGET = "budget"                     # the run budget (budget.py: the next session would pass constraints.budget_usd), or one
                                          # session's dollar or iteration cap (stages/common.run_session); approve = raise / accept, reject = close
    DEPLOY_FAILURE = "deploy_failure"     # Stage 5: dev pipeline failed or dev e2e failed; carries the log excerpt
    # Gate 3 (code review) is the forge's own change-request approval; see forge/


class Decision(str, Enum):
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"   # comments go back to the producing agent as feedback.md
    REJECT = "reject"                     # closes the run (or the work item)
    APPROVE_SUBSET = "approve_subset"     # Gate 2 only: approve some specs, send others back
    # DEPLOY_FAILURE gates use: REQUEST_CHANGES (back to IMPLEMENTING with the comments as findings),
    # APPROVE (the failure was the platform's, not the change's: re-observe the next pipeline), REJECT (close the item).
    # BUDGET gates use: APPROVE with the new cap in the comments (`budget_usd: 80`), REJECT (close the run).


def parse_budget(text: str | None) -> float | None:
    """The new run budget a human wrote in a decision or answer: `budget_usd: 80`, `budget: $80`, `$80`, or a bare
    number on its own line. None when there is none."""
    import re

    for line in (text or "").splitlines():
        s = line.strip()
        m = (re.fullmatch(r"(?:budget(?:_usd)?\s*[:=]\s*)?\$?\s*(\d+(?:\.\d+)?)\s*(?:usd)?", s, re.IGNORECASE)
             or re.search(r"budget(?:_usd)?\s*[:=]?\s*(?:to\s*)?\$?\s*(\d+(?:\.\d+)?)", s, re.IGNORECASE))
        if m:
            return float(m.group(1))
    return None


@dataclass(frozen=True)
class ArtifactRef:
    name: str
    sha256: str
    url: str | None = None                # where the human can read it (gate page anchor, tracker)


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
    work_item_id: str | None = None        # set for per-item gates (DEPLOY_FAILURE); trackers post on that issue


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


def run_cap_gate(kind: Any, artifacts: Any) -> bool:
    """A budget gate opened by the run cap (budget.py) presents the pause record `paused-N.json`; one opened for a
    single session's cap or iteration cap (stages/common.run_session) presents nothing. Only the former is approved
    with a new run budget. Accepts a Gate's fields or a queue record's (`kind` string, artifact dicts)."""
    kind = kind.value if isinstance(kind, GateKind) else kind
    if kind != GateKind.BUDGET.value:
        return False
    names = [a.name if isinstance(a, ArtifactRef) else (a.get("name") if isinstance(a, dict) else None) for a in (artifacts or ())]
    return any(str(n or "").startswith("paused-") for n in names)


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
    if run_cap_gate(gate.kind, gate.artifacts) and decision.decision == Decision.APPROVE and parse_budget(decision.comments) is None:
        raise ValueError("approving a budget gate needs the new run budget in the comments, e.g. `budget_usd: 80`")


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
    work_item_id: str | None = None


@dataclass
class Answer:
    question_id: str
    answer: str
    answered_by: str
    answered_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Notifier(Protocol):
    def gate_opened(self, gate: Gate) -> None: ...
    def question_asked(self, q: Question) -> None: ...
    def change_request_opened(self, run_id: str, spec_id: str, url: str) -> None: ...
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

    def change_request_opened(self, run_id: str, spec_id: str, url: str) -> None:
        print(f"[cr] {spec_id}: {url}")

    def escalated(self, run_id: str, reason: str) -> None:
        print(f"[escalated] {run_id}: {reason}")


class CompositeQueue:
    """Fan a gate out to several surfaces; the first surface with a decision wins.

    Used when a human may decide from the CLI (`factory decide`) or from the tracker (a label plus a
    comment on the Linear issue). Every surface sees `open`/`ask`; `decision`/`answer` poll in order.
    """

    def __init__(self, *queues: ApprovalQueue):
        self.queues = list(queues)

    def open(self, gate: Gate) -> None:
        for q in self.queues:
            q.open(gate)

    def decision(self, gate_id: str) -> GateDecision | None:
        for q in self.queues:
            d = q.decision(gate_id)
            if d is not None:
                return d
        return None

    def ask(self, q: Question) -> None:
        for queue in self.queues:
            queue.ask(q)

    def answer(self, question_id: str) -> Answer | None:
        for q in self.queues:
            a = q.answer(question_id)
            if a is not None:
                return a
        return None


class CompositeNotifier:
    def __init__(self, *notifiers: Notifier):
        self.notifiers = list(notifiers)

    def gate_opened(self, gate: Gate) -> None:
        for n in self.notifiers:
            n.gate_opened(gate)

    def question_asked(self, q: Question) -> None:
        for n in self.notifiers:
            n.question_asked(q)

    def change_request_opened(self, run_id: str, spec_id: str, url: str) -> None:
        for n in self.notifiers:
            n.change_request_opened(run_id, spec_id, url)

    def escalated(self, run_id: str, reason: str) -> None:
        for n in self.notifiers:
            n.escalated(run_id, reason)
