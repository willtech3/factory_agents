"""A simulated run for the Factory Console: the Ledgerline example replayed through the real store, state
machine, checks, and file queue, so the whole human loop (question, Gate 1, Gate 2, change requests) can be
exercised without the Managed Agents platform or a forge.

Every event the demo appends is one the orchestrator would append, in the same order and shape, so the
Console shows a demo run exactly as it would show a real one. The difference is who produces the artifacts:
here they are adapted from `examples/ledgerline/` instead of coming from agent sessions. A demo run is marked
by `runs/<run_id>/demo.json`, which also holds the phase the simulation is in; `tick()` advances it when the
human has answered or decided.
"""
from __future__ import annotations

import copy
import json
import threading
import uuid
from pathlib import Path
from typing import Any

from .. import checks
from ..gates import ArtifactRef, Decision, Gate, GateKind, Question
from ..state import RunStage, WorkItemStage
from ..store import RunStore
from ..surfaces.filequeue import FileQueue

QUESTION = {
    "question": "Should retries follow Stripe Smart Retries or our own schedule?",
    "why": "Our own schedule adds a scheduler work item and roughly a week; Smart Retries is a dashboard toggle.",
    "default": "Stripe Smart Retries",
    "options": ("Stripe Smart Retries", "Own schedule"),
}


class DemoRunner:
    def __init__(self, runs_dir: Path, schemas_dir: Path, examples_dir: Path):
        self.runs_dir = runs_dir
        self.schemas_dir = schemas_dir
        self.examples_dir = examples_dir
        self.store = RunStore(runs_dir)
        self.queue = FileQueue(runs_dir / "_queue")
        self._lock = threading.Lock()      # the background loop and /api/demo/tick must not advance a run at once

    # ------------------------------------------------------------------ public
    def example_outcome(self) -> dict[str, Any]:
        return json.loads((self.examples_dir / "outcome.json").read_text())

    def seed_example(self) -> str:
        return self.seed(self.example_outcome())

    def seed(self, outcome: dict[str, Any]) -> str:
        """Create a demo run: intake, decomposing, and a blocking question from the decomposer."""
        run_id = f"run_{uuid.uuid4().hex[:10]}"
        self._event(run_id, "run.created", {"outcome_id": outcome["outcome_id"], "title": outcome["title"],
                                            "forge": "demo", "gate_surface": "web",
                                            "budget_usd": (outcome.get("constraints") or {}).get("budget_usd")}, stage="intake")
        self.store.put_artifact(run_id, "intake", "outcome.json", json.dumps(outcome, indent=2).encode())
        self._event(run_id, "stage.entered", {"stage": RunStage.DECOMPOSING.value}, stage="decompose")
        q = Question(run_id=run_id, asked_by="decomposer", question=QUESTION["question"], why_it_matters=QUESTION["why"],
                     default_if_unanswered=QUESTION["default"], blocking=True, options=QUESTION["options"])
        self.queue.ask(q)
        self._event(run_id, "question.asked", {"question_id": q.question_id, "question": q.question, "blocking": True,
                                               "default": q.default_if_unanswered, "asked_by": "decomposer"},
                    stage="decompose", actor="agent")
        self._save(run_id, {"phase": "awaiting_answer", "question_id": q.question_id, "revision": 0, "spec_revision": 0})
        return run_id

    def runs(self) -> list[str]:
        return sorted(p.parent.name for p in self.runs_dir.glob("run_*/demo.json"))

    def is_demo(self, run_id: str) -> bool:
        return (self.runs_dir / run_id / "demo.json").exists()

    def tick_all(self) -> list[tuple[str, str]]:
        return [(r, self.tick(r)) for r in self.runs()]

    def tick(self, run_id: str) -> str:
        """Advance one phase if the human has done their part. Returns the phase after the tick."""
        with self._lock:
            return self._tick(run_id)

    def _tick(self, run_id: str) -> str:
        m = self._load(run_id)
        phase = m.get("phase")
        if phase == "awaiting_answer":
            a = self.queue.answer(m["question_id"])
            if a is None:
                return phase
            self._event(run_id, "question.answered", {"question_id": m["question_id"], "answer": a.answer,
                                                      "answered_by": a.answered_by}, stage="decompose", actor="human")
            m["answer"] = a.answer
            self._decompose(run_id, m, feedback=None)
        elif phase == "gate_1":
            d = self.queue.decision(m["gate_id"])
            if d is None:
                return phase
            self._decided(run_id, m, d, GateKind.DECOMPOSITION_REVIEW)
            if d.decision == Decision.APPROVE:
                self._specify(run_id, m, feedback=None)
            elif d.decision == Decision.REJECT:
                self._close(run_id, m, "gate_1")
            else:
                self._event(run_id, "stage.entered", {"stage": RunStage.DECOMPOSING.value}, stage="decompose")
                self._decompose(run_id, m, feedback=d.comments)
        elif phase == "gate_2":
            d = self.queue.decision(m["gate_id"])
            if d is None:
                return phase
            self._decided(run_id, m, d, GateKind.SPEC_READINESS)
            if d.decision in (Decision.APPROVE, Decision.APPROVE_SUBSET):
                self._implement(run_id, m, d)
            elif d.decision == Decision.REJECT:
                self._close(run_id, m, "gate_2")
            else:
                self._specify(run_id, m, feedback=d.comments)
        return self._load(run_id).get("phase", phase)

    # ------------------------------------------------------------------ phases
    def _decompose(self, run_id: str, m: dict[str, Any], feedback: str | None) -> None:
        outcome = self.store.get_json(run_id, "intake", "outcome.json")
        rev = int(m.get("revision", 0)) + 1
        dec = self._adapted_decomposition(outcome, rev, m.get("answer"), feedback)
        md = decomposition_markdown(dec, outcome)
        shas = {"decomposition.json": self.store.put_artifact(run_id, "decompose", f"r{rev}-decomposition.json",
                                                              json.dumps(dec, indent=2).encode()),
                "decomposition.md": self.store.put_artifact(run_id, "decompose", f"r{rev}-decomposition.md", md.encode())}
        results = [checks.schema_valid(dec, self.schemas_dir / "decomposition.schema.json", self.schemas_dir),
                   checks.dag_acyclic(dec["work_items"]), checks.metric_coverage(outcome, dec)]
        for name, sha in shas.items():
            self._event(run_id, "artifact.produced", {"artifact_type": Path(name).stem, "path": f"decompose/r{rev}-{name}",
                                                      "sha256": sha}, stage="decompose")
        self._event(run_id, "session.finished", {"session_id": f"sess_demo_{run_id[-6:]}_dec{rev}", "agent_id": "decomposer (demo)",
                                                 "agent_version": 1, "list_cost_usd": 6.4, "outcome_result": "satisfied",
                                                 "stop_reason": "end_turn"}, stage="decompose", actor="platform")
        self._event(run_id, "artifact.produced", {"artifact_type": "decomposition", "revision": rev, "work_items": dec["work_items"],
                                                  "checks": [r.to_dict() for r in results]}, stage="decompose")
        self._event(run_id, "stage.entered", {"stage": RunStage.GATE_1_DECOMPOSITION.value}, stage="gate_1")
        summary = dec["summary"]
        gate = self._open_gate(run_id, GateKind.DECOMPOSITION_REVIEW, shas, summary, outcome,
                               (Decision.APPROVE, Decision.REQUEST_CHANGES, Decision.REJECT))
        m.update({"phase": "gate_1", "gate_id": gate.gate_id, "revision": rev})
        self._save(run_id, m)

    def _specify(self, run_id: str, m: dict[str, Any], feedback: str | None) -> None:
        outcome = self.store.get_json(run_id, "intake", "outcome.json")
        dec = self.store.get_json(run_id, "decompose", f"r{m['revision']}-decomposition.json")
        rev = int(m.get("spec_revision", 0)) + 1
        if rev == 1:
            self._event(run_id, "stage.entered", {"stage": RunStage.SPECIFYING.value}, stage="specify")
        else:
            self._event(run_id, "stage.entered", {"stage": RunStage.SPECIFYING.value}, stage="specify")
        specs = self._adapted_specs(outcome, dec, rev, feedback)
        shas: dict[str, str] = {}
        for sid, spec in specs.items():
            wid = spec["work_item_id"]
            self._event(run_id, "work_item.stage", {"stage": WorkItemStage.SPEC_DRAFTING.value, "spec_round": rev},
                        work_item_id=wid, stage="specify")
            self.store.put_artifact(run_id, "specify", f"{wid}-r{rev}-spec.json", json.dumps(spec, indent=2).encode())
            self.store.put_artifact(run_id, "specify", f"{wid}-r{rev}-spec.md", spec_markdown(spec).encode())
            self._event(run_id, "session.finished", {"session_id": f"sess_demo_{wid[-6:]}_r{rev}", "agent_id": "spec_writer (demo)",
                                                     "agent_version": 1, "list_cost_usd": 3.1, "outcome_result": "satisfied",
                                                     "stop_reason": "end_turn"}, stage="specify", actor="platform")
            self._event(run_id, "work_item.stage", {"stage": WorkItemStage.SPEC_JUDGING.value, "spec_id": sid},
                        work_item_id=wid, stage="specify")
            shas[f"{sid}.json"] = self.store.put_artifact(run_id, "specify", f"final-{sid}.json", json.dumps(spec, indent=2).encode())
        verdict = self._adapted_verdict(outcome, dec, specs, rev, feedback)
        shas["verdict.json"] = self.store.put_artifact(run_id, "judge", "final-verdict.json", json.dumps(verdict, indent=2).encode())
        self._event(run_id, "session.finished", {"session_id": f"sess_demo_judge_r{rev}", "agent_id": "spec_judge (demo)",
                                                 "agent_version": 1, "list_cost_usd": 5.0, "outcome_result": None,
                                                 "stop_reason": "end_turn"}, stage="judge", actor="platform")
        self._event(run_id, "judge.verdict", {"round": rev, "result": verdict["overall"]["result"],
                                              "blocking": verdict["overall"]["blocking_issue_count"]}, stage="judge", actor="agent")
        self._event(run_id, "stage.entered", {"stage": RunStage.GATE_2_SPEC_READINESS.value}, stage="gate_2")
        gate = self._open_gate(run_id, GateKind.SPEC_READINESS, shas, verdict["overall"]["summary_for_human"], outcome,
                               (Decision.APPROVE, Decision.APPROVE_SUBSET, Decision.REQUEST_CHANGES, Decision.REJECT))
        m.update({"phase": "gate_2", "gate_id": gate.gate_id, "spec_revision": rev, "spec_ids": sorted(specs)})
        self._save(run_id, m)

    def _implement(self, run_id: str, m: dict[str, Any], d: Any) -> None:
        dec = self.store.get_json(run_id, "decompose", f"r{m['revision']}-decomposition.json")
        approved = set(d.approved_subset) if d.decision == Decision.APPROVE_SUBSET else set(m["spec_ids"])
        by_spec = {f"spec_{w['work_item_id'].removeprefix('wi-')}": w["work_item_id"] for w in dec["work_items"]}
        for sid in sorted(approved):
            wid = by_spec.get(sid)
            if wid:
                self._event(run_id, "work_item.stage", {"stage": WorkItemStage.SPEC_APPROVED.value, "spec_id": sid},
                            work_item_id=wid, spec_id=sid, actor="human")
        self._event(run_id, "stage.entered", {"stage": RunStage.IMPLEMENTING.value}, stage="implement")
        for n, sid in enumerate(sorted(approved), 1):
            wid = by_spec.get(sid)
            if not wid:
                continue
            branch = f"factory/{run_id}/{sid}"
            for stage in (WorkItemStage.IMPLEMENTING, WorkItemStage.REVIEWING, WorkItemStage.MERGE_JUDGING, WorkItemStage.INTEGRATING):
                payload: dict[str, Any] = {"stage": stage.value}
                if stage == WorkItemStage.IMPLEMENTING:
                    payload["branch"] = branch
                self._event(run_id, "work_item.stage", payload, work_item_id=wid, spec_id=sid)
            self._event(run_id, "session.finished", {"session_id": f"sess_demo_impl_{wid[-6:]}", "agent_id": "implementer (demo)",
                                                     "agent_version": 1, "list_cost_usd": 11.2}, stage="implement", actor="platform")
            url = f"https://github.com/example/app/pull/{400 + n}"
            self._event(run_id, "cr.opened", {"id": 400 + n, "url": url, "branch": branch, "forge": "demo"}, work_item_id=wid, spec_id=sid)
            self._event(run_id, "work_item.stage", {"stage": WorkItemStage.CR_OPEN.value, "cr_id": 400 + n}, work_item_id=wid, spec_id=sid)
        self._event(run_id, "run.completed", {"specs_with_change_requests": sorted(approved)})
        m.update({"phase": "done"})
        self._save(run_id, m)

    def _close(self, run_id: str, m: dict[str, Any], stage: str) -> None:
        self._event(run_id, "stage.entered", {"stage": RunStage.CLOSED.value}, stage=stage, actor="human")
        m.update({"phase": "closed"})
        self._save(run_id, m)

    # ------------------------------------------------------------------ helpers
    def _open_gate(self, run_id: str, kind: GateKind, shas: dict[str, str], summary: str, outcome: dict[str, Any],
                   options: tuple[Decision, ...]) -> Gate:
        refs = tuple(ArtifactRef(name=n, sha256=s) for n, s in sorted(shas.items()))
        gate = Gate(run_id=run_id, kind=kind, artifacts=refs, assignees=(outcome["owner"]["handle"],), summary=summary, options=options)
        self.queue.open(gate)
        self._event(run_id, "gate.opened", {"gate_id": gate.gate_id, "kind": kind.value, "artifacts": [r.__dict__ for r in refs],
                                            "assignees": list(gate.assignees), "summary": summary}, stage=kind.value)
        return gate

    def _decided(self, run_id: str, m: dict[str, Any], d: Any, kind: GateKind) -> None:
        self._event(run_id, "gate.decided", {"gate_id": d.gate_id, "kind": kind.value, "decision": d.decision.value,
                                             "comments": d.comments, "approved_subset": list(d.approved_subset),
                                             "artifact_shas": list(d.artifact_shas)}, stage=kind.value, actor="human")

    def _event(self, run_id: str, kind: str, payload: dict[str, Any], actor: str = "orchestrator", **extra: Any) -> None:
        self.store.append(run_id, kind, {"type": actor if actor in ("human", "agent", "platform") else "orchestrator", "id": actor},
                          payload, **extra)

    def _load(self, run_id: str) -> dict[str, Any]:
        p = self.runs_dir / run_id / "demo.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def _save(self, run_id: str, m: dict[str, Any]) -> None:
        (self.runs_dir / run_id / "demo.json").write_text(json.dumps(m, indent=2))

    # ------------------------------------------------------------------ adapted artifacts
    def _example(self, name: str) -> dict[str, Any]:
        return json.loads((self.examples_dir / f"{name}.json").read_text())

    def _adapted_decomposition(self, outcome: dict[str, Any], rev: int, answer: str | None, feedback: str | None) -> dict[str, Any]:
        dec = copy.deepcopy(self._example("decomposition"))
        metrics = [m["metric"] for m in outcome["success_metrics"]]
        example_metrics = [c["metric"] for c in dec["coverage"]]
        rename = {e: metrics[i] for i, e in enumerate(example_metrics) if i < len(metrics)}
        dec["outcome_id"] = outcome["outcome_id"]
        dec["revision"] = rev
        repo = (outcome.get("target_repositories") or [{}])[0].get("url", "")
        for w in dec["work_items"]:
            w["contributes_to_metrics"] = [rename.get(x, x) for x in w.get("contributes_to_metrics", []) if x in rename]
            if repo and "repositories" in w:
                w["repositories"] = [repo]
        coverage = [{**c, "metric": rename[c["metric"]]} for c in dec["coverage"] if c["metric"] in rename]
        for extra in metrics[len(example_metrics):]:
            coverage.append({"metric": extra, "work_item_ids": [w["work_item_id"] for w in dec["work_items"]],
                             "rationale": "Demo: every work item is assumed to move this metric; a real decomposer would be specific."})
        dec["coverage"] = coverage
        if answer:
            dec["assumptions"] = [f"Owner answered the retry-schedule question: {answer}."] + list(dec.get("assumptions", []))
        if feedback:
            dec["summary"] = (f"Revision {rev}, after your feedback. What changed: {feedback.strip()[:300]} "
                              f"(demo: the summary records the request; a real re-run would rework the items.)\n\n" + dec["summary"])
        return dec

    def _adapted_specs(self, outcome: dict[str, Any], dec: dict[str, Any], rev: int, feedback: str | None) -> dict[str, dict[str, Any]]:
        base = self._example("spec_smart_retries")
        specs: dict[str, dict[str, Any]] = {}
        for w in dec["work_items"]:
            sid = f"spec_{w['work_item_id'].removeprefix('wi-')}"
            if w["work_item_id"] == base["work_item_id"]:
                spec = copy.deepcopy(base)
            else:
                spec = copy.deepcopy(base)
                spec["title"] = w["title"]
                spec["goal"] = w["intent"]
                spec["acceptance_criteria"] = [
                    {"ac_id": "AC-1", "user_visible": True, "given": f"the work item '{w['title']}' is deployed to a preview",
                     "when": "an operator exercises the behaviour described in the intent",
                     "then": w["intent"], "verification": "automated_test"},
                    {"ac_id": "AC-2", "user_visible": False, "given": "the existing test suite",
                     "when": "the implementer runs it", "then": "every existing test still passes",
                     "verification": "automated_test"},
                ]
                spec["interfaces"] = spec["interfaces"][:1]
                spec["test_plan"] = {**spec["test_plan"], "new_tests": [], "e2e": [
                    {"covers_ac": ["AC-1"], "command": "curl -fsS $BASE_URL/up", "target": "preview",
                     "description": "Demo smoke check against the preview"}]}
                spec["estimated_size"] = w.get("size", "M")
                spec["open_questions"] = []
            spec["spec_id"] = sid
            spec["work_item_id"] = w["work_item_id"]
            spec["decomposition_id"] = dec["decomposition_id"]
            spec["outcome_id"] = outcome["outcome_id"]
            spec["revision"] = rev
            spec["dependencies"] = {"specs": [f"spec_{d.removeprefix('wi-')}" for d in w.get("depends_on", [])],
                                    "external": spec.get("dependencies", {}).get("external", [])}
            if feedback and rev > 1:
                spec["context"] = f"Revision {rev}, after human feedback: {feedback.strip()[:300]}\n\n" + str(spec.get("context", ""))
            specs[sid] = spec
        return specs

    def _adapted_verdict(self, outcome: dict[str, Any], dec: dict[str, Any], specs: dict[str, dict[str, Any]], rev: int,
                         feedback: str | None) -> dict[str, Any]:
        v = copy.deepcopy(self._example("verdict_round2"))
        v["decomposition_id"] = dec["decomposition_id"]
        v["round"] = rev
        v["per_spec"] = [ps for ps in v["per_spec"] if ps["spec_id"] in specs]
        for sid in specs:
            if not any(ps["spec_id"] == sid for ps in v["per_spec"]):
                v["per_spec"].append({"spec_id": sid, "criteria": [], "issues": []})
        metrics = [m["metric"] for m in outcome["success_metrics"]]
        example_metrics = [g["metric"] for g in v.get("goal_coverage", [])]
        rename = {e: metrics[i] for i, e in enumerate(example_metrics) if i < len(metrics)}
        v["goal_coverage"] = [{**g, "metric": rename[g["metric"]]} for g in v.get("goal_coverage", []) if g["metric"] in rename]
        if feedback and rev > 1:
            v["overall"]["summary_for_human"] = (f"Round {rev}. The writers addressed your feedback ({feedback.strip()[:200]}). "
                                                 + v["overall"]["summary_for_human"])
        return v


# ---------------------------------------------------------------------- markdown renderings

def decomposition_markdown(dec: dict[str, Any], outcome: dict[str, Any]) -> str:
    lines = [f"# Decomposition of: {outcome.get('title', '')}", "", "## What you are approving", "", dec["summary"], "",
             "## Work items", "", "| id | title | kind | size | depends on | metrics |", "|---|---|---|---|---|---|"]
    for w in dec["work_items"]:
        lines.append(f"| `{w['work_item_id']}` | {w['title']} | {w.get('kind', '')} | {w.get('size', '')} | "
                     f"{', '.join(w.get('depends_on', [])) or '-'} | {', '.join(w.get('contributes_to_metrics', []))} |")
    lines += ["", "## Coverage", ""]
    for c in dec.get("coverage", []):
        lines.append(f"- **{c['metric']}**: {', '.join(c.get('work_item_ids', [])) or 'not covered'}. {c.get('rationale', '')}")
    lines += ["", "## Explicitly out of scope", ""] + [f"- {x}" for x in dec.get("explicitly_out_of_scope", [])]
    lines += ["", "## Open questions", ""]
    for q in dec.get("open_questions", []):
        lines.append(f"- {q['question']} (default: {q.get('default_if_unanswered', '')})")
    lines += ["", "## Assumptions", ""] + [f"- {x}" for x in dec.get("assumptions", [])]
    return "\n".join(lines) + "\n"


def spec_markdown(spec: dict[str, Any]) -> str:
    lines = [f"# {spec['spec_id']}: {spec['title']}", "", spec.get("goal", ""), "", "## Acceptance criteria", ""]
    for ac in spec.get("acceptance_criteria", []):
        lines.append(f"- **{ac['ac_id']}** Given {ac['given']}, when {ac['when']}, then {ac['then']}.")
    lines += ["", "## Allowed paths", ""] + [f"- `{p}`" for p in spec.get("allowed_paths", [])]
    lines += ["", "## Test plan", ""] + [f"- `{c}`" for c in spec.get("test_plan", {}).get("commands", [])]
    return "\n".join(lines) + "\n"
