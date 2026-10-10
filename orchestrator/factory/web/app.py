"""The Factory Console (v0): a local web app over the run store and the file queue.

What it is for: writing an outcome, watching runs, and deciding Gate 1 and Gate 2 (and answering blocking
questions) from a page that puts the judge's summary first and binds the decision to the artifact hashes
the page showed (ADR-0005). It is the `web` gate surface: it reads `runs/<run_id>/events.jsonl` and the
artifacts, and writes decisions and answers to `runs/_queue/`, exactly where `factory decide` writes them,
so a running `factory run` picks them up unchanged. No agent, forge, or tracker call happens here.

Routes: `/` and `/static/*` serve the UI; `/api/*` is JSON; `/gates/<id>/page` is the standalone artifact
page (the same renderer as `surfaces/artifact_pages.py`). `--demo` seeds a simulated run (`demo.py`).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import checks
from .. import config as cfgmod
from ..config import REPO_ROOT, Config
from ..gates import Decision, GateKind
from ..store import RunStore
from ..surfaces.artifact_pages import render_gate1_page, render_gate2_page
from ..surfaces.filequeue import FileQueue
from ..watcher import heartbeat as watcher_heartbeat
from ..watcher import pending_work_items
from .demo import DemoRunner

STATIC = Path(__file__).parent / "static"
RUN_ID = re.compile(r"^run_[a-z0-9]{8,}$")
OUTCOME_ID = re.compile(r"^out_[a-z0-9]{8,}$")
# Every examples/<name>/outcome.json is offered as a starting point in the outcome form.
EXAMPLES = {p.parent.name: p for p in sorted((REPO_ROOT / "examples").glob("*/outcome.json"))}
TIMELINE_KINDS = {"run.created", "repository.created", "stage.entered", "session.started", "gate.opened", "gate.decided", "question.asked", "question.answered",
                  "judge.verdict", "review.findings", "review.response", "merge.verdict", "cr.opened", "cr.merged", "cr.closed",
                  "deploy.started", "deploy.succeeded", "deploy.failed", "e2e.dev_passed", "e2e.dev_failed", "budget.reached",
                  "run.escalated", "run.failed", "run.resumed", "run.completed", "session.finished", "work_item.stage"}


class DecisionBody(BaseModel):
    decision: str
    comments: str = ""
    approved_subset: list[str] = []
    by: str = "console"


class AnswerBody(BaseModel):
    answer: str
    by: str = "console"


class OutcomeBody(BaseModel):
    outcome: dict[str, Any]


class StartRunBody(BaseModel):
    outcome_id: str
    mode: str = "demo"          # demo | design | live
    settings: dict[str, Any] = {}   # tunables from the Start screen; see SETTINGS_ENV


# Start-screen settings -> the environment variables the orchestrator reads (cli._policy, config). Keys not listed are ignored.
SETTINGS_ENV = {
    "budget_usd": "FACTORY_RUN_BUDGET_USD", "preview": "FACTORY_PREVIEW", "reviewer_passes": "FACTORY_REVIEWER_PASSES",
    "review_rounds": "FACTORY_REVIEW_MAX_ROUNDS", "security_review": "FACTORY_SECURITY_REVIEW",
    "parallel_items": "FACTORY_PARALLEL_IMPLEMENTATIONS", "grader_iterations": "FACTORY_OUTCOME_MAX_ITERATIONS",
    "budget_scale": "FACTORY_BUDGET_SCALE",
}
CREW_ROLES = ["decomposer", "spec_writer", "spec_judge", "implementer", "reviewer", "security_reviewer", "merge_judge", "integrator"]


def settings_env(settings: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, var in SETTINGS_ENV.items():
        if key in settings and settings[key] not in (None, ""):
            v = settings[key]
            out[var] = ("1" if v else "0") if isinstance(v, bool) else str(v)
    return out


def crew_roster(events) -> list[dict[str, Any]]:
    """One row per crew role from the run's session events: state (asleep, working, done), the live or last session,
    sessions so far, cost so far."""
    by_role: dict[str, dict[str, Any]] = {r: {"role": r, "sessions": 0, "cost_usd": 0.0, "state": "asleep", "title": None,
                                               "started_at": None, "finished_at": None, "session_id": None, "live": []} for r in CREW_ROLES}
    started: dict[str, dict[str, Any]] = {}
    for ev in events:
        p = ev.payload
        if ev.kind == "session.started" and p.get("agent_key") in by_role:
            row = by_role[p["agent_key"]]
            started[p["session_id"]] = {"role": p["agent_key"], "at": ev.at, "title": p.get("title")}
            row["sessions"] += 1; row["title"] = p.get("title"); row["started_at"] = ev.at; row["session_id"] = p["session_id"]
            row["live"].append(p["session_id"]); row["state"] = "working"
        elif ev.kind == "session.finished":
            meta = started.get(p.get("session_id"))
            if meta is None:
                continue
            row = by_role[meta["role"]]
            row["cost_usd"] += float(p.get("list_cost_usd") or 0.0)
            if p["session_id"] in row["live"]:
                row["live"].remove(p["session_id"])
            row["finished_at"] = ev.at
            row["state"] = "working" if row["live"] else ("blocked" if p.get("stop_reason") in ("budget_reached",) else "done")
        elif ev.kind == "run.failed":
            # sessions the orchestrator interrupted on its way out never get a session.finished of their own
            for sid in p.get("interrupted_sessions") or []:
                meta = started.get(sid)
                if meta and sid in by_role[meta["role"]]["live"]:
                    row = by_role[meta["role"]]
                    row["live"].remove(sid)
                    row["finished_at"] = ev.at
                    row["state"] = "working" if row["live"] else "done"
    for row in by_role.values():
        row["cost_usd"] = round(row["cost_usd"], 2)
        row["live_count"] = len(row.pop("live"))
    return list(by_role.values())


class Console:
    """Everything the routes need, bound to one runs directory."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = RunStore(cfg.runs_dir)
        self.queue = FileQueue(cfg.runs_dir / "_queue")
        self.demo = DemoRunner(cfg.runs_dir, cfg.schemas_dir, REPO_ROOT / "examples" / "ledgerline")
        self.outcomes_dir = cfg.runs_dir / "_outcomes"
        self.logs_dir = cfg.runs_dir / "_logs"

    # ---- runs
    def _title(self, run_id: str, events=None) -> str:
        for ev in events or self.store.events(run_id):
            if ev.kind == "run.created":
                return ev.payload.get("title") or run_id
        return run_id

    def runs(self) -> list[dict[str, Any]]:
        gates = self.queue.open_gates()
        questions = self.queue.open_questions()
        out = []
        for run_id in self.store.list_runs():
            events = self.store.events(run_id)
            try:
                st = self.store.state(run_id)
            except Exception as e:  # a corrupt log should not hide the other runs
                out.append({"run_id": run_id, "title": self._title(run_id, events), "stage": "unreadable", "error": str(e)})
                continue
            by_stage: dict[str, int] = {}
            for wi in st.work_items.values():
                by_stage[wi.stage.value] = by_stage.get(wi.stage.value, 0) + 1
            out.append({
                "run_id": run_id, "title": self._title(run_id, events), "stage": st.stage.value,
                "cost": round(st.list_cost_usd, 2), "updated_at": events[-1].at if events else None,
                "created_at": events[0].at if events else None,
                "waiting": {"gates": sum(1 for g in gates.values() if g["run_id"] == run_id and g["gate_id"] in st.open_gates and not g["recorded"]),
                            "questions": sum(1 for q in questions.values() if q["run_id"] == run_id and not q["answered"]
                                             and q["question_id"] in st.open_questions)},
                "work_items": {"total": len(st.work_items), "by_stage": by_stage},
                "demo": self.demo.is_demo(run_id),
                "design_only": any(ev.kind in ("run.created", "run.completed") and ev.payload.get("design_only") for ev in events),
            })
        out.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
        return out

    def run_detail(self, run_id: str) -> dict[str, Any]:
        if not RUN_ID.match(run_id) or not (self.cfg.runs_dir / run_id / "events.jsonl").is_file():
            raise HTTPException(404, f"no run {run_id}")       # checked first: RunStore.events would create the directory
        events = self.store.events(run_id)
        if not events:
            raise HTTPException(404, f"no run {run_id}")
        st = self.store.state(run_id)
        outcome = None
        try:
            outcome = self.store.get_json(run_id, "intake", "outcome.json")
        except FileNotFoundError:
            pass
        titles = {}
        for ev in events:
            if ev.kind == "artifact.produced" and ev.payload.get("artifact_type") == "decomposition":
                titles = {w["work_item_id"]: w.get("title", "") for w in ev.payload.get("work_items", [])}
        items = [{"work_item_id": wi.work_item_id, "title": titles.get(wi.work_item_id, ""), "stage": wi.stage.value,
                  "spec_id": wi.spec_id, "branch": wi.branch, "cr_id": wi.cr_id, "merge_sha": wi.merge_sha, "depends_on": wi.depends_on,
                  "review_round": wi.review_round, "dev_environment_url": wi.dev_environment_url}
                 for wi in st.work_items.values()]
        crs = {ev.spec_id: ev.payload for ev in events if ev.kind == "cr.opened" and ev.spec_id}
        for it in items:
            if it["spec_id"] in crs:
                it["cr_url"] = crs[it["spec_id"]].get("url")
        gates = [g for g in self.queue.open_gates().values() if g["run_id"] == run_id and g["gate_id"] in st.open_gates]
        questions = [q for q in self.queue.open_questions().values() if q["run_id"] == run_id and q["question_id"] in st.open_questions]
        timeline = [{"seq": ev.seq, "at": ev.at, "kind": ev.kind, "actor": ev.actor.get("type"), "stage": ev.stage,
                     "work_item_id": ev.work_item_id, "spec_id": ev.spec_id, "payload": _trim(ev.payload)}
                    for ev in events if ev.kind in TIMELINE_KINDS]
        design_only = any(ev.kind == "run.completed" and ev.payload.get("design_only") for ev in events) or \
            any(ev.kind == "run.created" and ev.payload.get("design_only") for ev in events)
        # A run that died in the implementation stage can continue from its artefacts (`factory resume`).
        resumable = (st.stage.value == "failed" and not self.demo.is_demo(run_id)
                     and any(ev.kind == "stage.entered" and ev.payload.get("stage") == "implementing" for ev in events))
        # Gate 3 and Stage 5 are followed by `factory watch`, a separate process; its heartbeat says whether one is running.
        pending = [wi.work_item_id for wi in pending_work_items(st)]
        watch = {"pending": pending, "watchable": bool(pending) and not self.demo.is_demo(run_id),
                 "heartbeat": watcher_heartbeat(self.cfg.runs_dir, run_id)}
        created = next((ev.payload for ev in events if ev.kind == "run.created"), {})
        policy = created.get("policy") or {"run_budget_usd": ((outcome or {}).get("constraints") or {}).get("budget_usd"),
                                           "preview_mode": self.cfg.preview_mode, "recorded": False}
        return {"run_id": run_id, "title": self._title(run_id, events), "outcome": outcome, "stage": st.stage.value,
                "cost": round(st.list_cost_usd, 2), "demo": self.demo.is_demo(run_id), "design_only": design_only, "work_items": items,
                "resumable": resumable, "watch": watch, "crew": crew_roster(events), "policy": policy,
                "gates": gates, "questions": questions, "events": timeline, "artifacts": self._artifacts(run_id),
                "created_at": events[0].at, "updated_at": events[-1].at}

    def _artifacts(self, run_id: str) -> list[dict[str, Any]]:
        root = self.cfg.runs_dir / run_id / "artifacts"
        out = []
        if root.exists():
            for p in sorted(root.rglob("*")):
                if p.is_file() and not p.name.endswith(".sha256"):
                    sha_file = p.with_suffix(p.suffix + ".sha256")
                    out.append({"stage": p.parent.relative_to(root).as_posix(), "name": p.name, "bytes": p.stat().st_size,
                                "sha256": sha_file.read_text().strip() if sha_file.exists() else None})
        return out

    def artifact_by_sha(self, run_id: str, sha: str) -> Path | None:
        root = self.cfg.runs_dir / run_id / "artifacts"
        for p in root.rglob("*.sha256") if root.exists() else []:
            if p.read_text().strip() == sha:
                return p.with_name(p.name[: -len(".sha256")])
        return None

    # ---- inbox
    def inbox(self) -> dict[str, Any]:
        titles: dict[str, str] = {}
        states: dict[str, Any] = {}
        for run_id in self.store.list_runs():
            try:
                states[run_id] = self.store.state(run_id)
                titles[run_id] = self._title(run_id)
            except Exception:
                continue
        gates = [{**g, "run_title": titles.get(g["run_id"], g["run_id"])} for g in self.queue.open_gates().values()
                 if g["run_id"] in states and g["gate_id"] in states[g["run_id"]].open_gates]
        questions = [{**q, "run_title": titles.get(q["run_id"], q["run_id"])} for q in self.queue.open_questions().values()
                     if q["run_id"] in states and q["question_id"] in states[q["run_id"]].open_questions and not q["answered"]]
        gates.sort(key=lambda g: g.get("opened_at") or "")
        questions.sort(key=lambda q: q.get("asked_at") or "")
        return {"gates": gates, "questions": questions}

    # ---- gates
    def gate(self, gate_id: str) -> dict[str, Any]:
        rec = self.queue.open_gates().get(gate_id)
        if rec is None:
            raise HTTPException(404, f"no gate {gate_id}")
        run_id = rec["run_id"]
        events = self.store.events(run_id)
        st = self.store.state(run_id)
        rec["open"] = gate_id in st.open_gates
        decided = next((ev.payload for ev in events if ev.kind == "gate.decided" and ev.payload.get("gate_id") == gate_id), None)
        return {"gate": rec, "run_id": run_id, "run_title": self._title(run_id, events), "run_stage": st.stage.value,
                "decided": decided, "review": self._review(run_id, rec, events)}

    def _review(self, run_id: str, rec: dict[str, Any], events) -> dict[str, Any]:
        kind = rec["kind"]
        by_name = {a["name"]: a["sha256"] for a in rec["artifacts"]}

        def load(name: str, text: bool = False):
            p = self.artifact_by_sha(run_id, by_name[name]) if name in by_name else None
            if p is None:
                return None
            return p.read_text(errors="replace") if text else json.loads(p.read_text())

        outcome = None
        try:
            outcome = self.store.get_json(run_id, "intake", "outcome.json")
        except FileNotFoundError:
            pass
        if kind == GateKind.DECOMPOSITION_REVIEW.value:
            dec = load("decomposition.json")
            checks_ = next((ev.payload.get("checks") for ev in reversed(events)
                            if ev.kind == "artifact.produced" and ev.payload.get("artifact_type") == "decomposition"
                            and (dec is None or ev.payload.get("revision") == dec.get("revision"))), None)
            previous = [ev.payload for ev in events if ev.kind == "gate.decided" and ev.payload.get("kind") == kind
                        and ev.payload.get("gate_id") != rec["gate_id"]]
            return {"kind": "decomposition", "outcome": outcome, "decomposition": dec,
                    "decomposition_md": load("decomposition.md", text=True) or "", "checks": checks_ or [],
                    "previous_decisions": previous}
        if kind in (GateKind.SPEC_READINESS.value, GateKind.ESCALATION.value):
            specs, spec_md = {}, {}
            for name in by_name:
                if name.endswith(".json") and name != "verdict.json":
                    s = load(name)
                    if s:
                        specs[s["spec_id"]] = s
                        md = self._latest(run_id, "specify", f"{s['work_item_id']}-", "-spec.md")
                        spec_md[s["spec_id"]] = md.read_text(errors="replace") if md else ""
            titles = {}
            for ev in events:
                if ev.kind == "artifact.produced" and ev.payload.get("artifact_type") == "decomposition":
                    titles = {w["work_item_id"]: w.get("title", "") for w in ev.payload.get("work_items", [])}
            previous = [ev.payload for ev in events if ev.kind == "gate.decided" and ev.payload.get("kind") == kind
                        and ev.payload.get("gate_id") != rec["gate_id"]]
            return {"kind": "specs", "outcome": outcome, "specs": specs, "spec_md": spec_md, "verdict": load("verdict.json"),
                    "work_item_titles": titles, "escalation": kind == GateKind.ESCALATION.value, "previous_decisions": previous}
        if kind == GateKind.DEPLOY_FAILURE.value:
            report = next((load(n) for n in by_name if n.endswith("deploy_report.json")), None)
            return {"kind": "deploy", "outcome": outcome, "report": report}
        return {"kind": "other", "outcome": outcome}

    def _latest(self, run_id: str, stage: str, prefix: str, suffix: str) -> Path | None:
        d = self.cfg.runs_dir / run_id / "artifacts" / stage
        cands = sorted(p for p in d.glob(f"{prefix}*{suffix}") if not p.name.endswith(".sha256")) if d.exists() else []
        return cands[-1] if cands else None

    def gate_page(self, gate_id: str) -> str:
        g = self.gate(gate_id)
        r = g["review"]
        shas = {a["name"]: a["sha256"] for a in g["gate"]["artifacts"]}
        if r["kind"] == "decomposition" and r["decomposition"]:
            return render_gate1_page(g["run_id"], r["outcome"] or {}, r["decomposition"], r["decomposition_md"], shas, r["checks"])
        if r["kind"] == "specs" and r["verdict"]:
            return render_gate2_page(g["run_id"], r["outcome"] or {}, r["specs"], r["verdict"], shas, r["spec_md"])
        raise HTTPException(404, "no page for this gate")

    def decide(self, gate_id: str, body: DecisionBody) -> dict[str, Any]:
        rec = self.queue.open_gates().get(gate_id)
        if rec is None:
            raise HTTPException(404, f"no gate {gate_id}")
        if rec["recorded"]:
            raise HTTPException(409, "a decision is already recorded for this gate; the run will pick it up")
        try:
            decision = Decision(body.decision)
        except ValueError:
            raise HTTPException(400, f"unknown decision {body.decision!r}")
        if decision.value not in rec["options"]:
            raise HTTPException(400, f"{decision.value} is not an option for this gate; options: {rec['options']}")
        if decision == Decision.REQUEST_CHANGES and not body.comments.strip():
            raise HTTPException(400, "request_changes needs a comment; it becomes the agent's feedback")
        if decision == Decision.APPROVE_SUBSET and not body.approved_subset:
            raise HTTPException(400, "approve_subset needs at least one spec")
        entry = self.queue.record_decision(gate_id, decision.value, body.by or "console", body.comments, body.approved_subset)
        return {"ok": True, "recorded": entry}

    def answer(self, question_id: str, body: AnswerBody) -> dict[str, Any]:
        if not body.answer.strip():
            raise HTTPException(400, "an answer is required")
        try:
            entry = self.queue.record_answer(question_id, body.answer.strip(), body.by or "console")
        except KeyError:
            raise HTTPException(404, f"no question {question_id}")
        return {"ok": True, "recorded": entry}

    # ---- outcomes
    def example_outcome(self, name: str) -> dict[str, Any]:
        path = EXAMPLES.get(name)
        if path is None:
            raise HTTPException(404, f"no example {name!r}; known: {', '.join(EXAMPLES)}")
        o = json.loads(path.read_text())
        o["outcome_id"], o["created_at"] = "", ""
        return o

    def outcome_template(self, example: str = "sample-app") -> dict[str, Any]:
        ex = self.example_outcome(example)
        return {"outcome_id": "", "title": "", "statement": "", "created_at": "",
                "success_metrics": [{"metric": "", "baseline": "", "target": "", "measurement": ""}],
                "constraints": {"must_not": [], "budget_usd": 150, "deadline": "", "compliance": []},
                "context_links": [], "clarifications": [],
                "target_repositories": [{"url": "", "default_branch": "main", "paths_in_scope": []}],
                "owner": {"name": "", "handle": "", "role": ""},
                "_example": ex}

    def outcomes(self) -> list[dict[str, Any]]:
        self.outcomes_dir.mkdir(parents=True, exist_ok=True)
        runs_by_outcome: dict[str, list[str]] = {}
        for run_id in self.store.list_runs():
            for ev in self.store.events(run_id):
                if ev.kind == "run.created":
                    runs_by_outcome.setdefault(ev.payload.get("outcome_id", ""), []).append(run_id)
                    break
        out = []
        for p in sorted(self.outcomes_dir.glob("*.json")):
            try:
                o = json.loads(p.read_text())
            except json.JSONDecodeError:
                continue
            out.append({"outcome_id": o.get("outcome_id"), "title": o.get("title"), "created_at": o.get("created_at"),
                        "owner": (o.get("owner") or {}).get("handle"), "runs": runs_by_outcome.get(o.get("outcome_id"), []),
                        "path": str(p)})
        out.sort(key=lambda o: o.get("created_at") or "", reverse=True)
        return out

    def save_outcome(self, outcome: dict[str, Any]) -> dict[str, Any]:
        outcome = dict(outcome)
        outcome.pop("_example", None)
        if not outcome.get("outcome_id"):
            outcome["outcome_id"] = f"out_{uuid.uuid4().hex[:10]}"
        if not outcome.get("created_at"):
            outcome["created_at"] = datetime.now(timezone.utc).isoformat()
        outcome = _clean(outcome)
        result = checks.schema_valid(outcome, self.cfg.schemas_dir / "outcome.schema.json", self.cfg.schemas_dir)
        problems = [] if result.passed else [result.detail]
        if not (outcome.get("success_metrics") or []):
            problems.append("at least one success metric with a target is required")
        if problems:
            return {"ok": False, "errors": problems, "outcome": outcome}
        self.outcomes_dir.mkdir(parents=True, exist_ok=True)
        path = self.outcomes_dir / f"{outcome['outcome_id']}.json"
        path.write_text(json.dumps(outcome, indent=2))
        return {"ok": True, "outcome_id": outcome["outcome_id"], "path": str(path), "outcome": outcome}

    def load_outcome(self, outcome_id: str) -> dict[str, Any]:
        if not OUTCOME_ID.match(outcome_id):
            raise HTTPException(404, f"no saved outcome {outcome_id!r}")
        p = self.outcomes_dir / f"{outcome_id}.json"
        if not p.exists():
            raise HTTPException(404, f"no saved outcome {outcome_id}")
        return json.loads(p.read_text())

    def start_run(self, body: StartRunBody) -> dict[str, Any]:
        outcome = self.load_outcome(body.outcome_id)
        if body.mode == "demo":
            run_id = self.demo.seed(outcome)
            return {"ok": True, "mode": "demo", "run_id": run_id}
        if body.mode not in ("live", "design"):
            raise HTTPException(400, "mode must be demo, design, or live")
        design = body.mode == "design"
        gaps = cfgmod.missing(self.cfg, design_only=design)
        if gaps:
            raise HTTPException(409, f"the orchestrator is not configured for a {body.mode} run: {', '.join(gaps)}")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log = self.logs_dir / f"{body.outcome_id}-{int(time.time())}.log"
        path = self.outcomes_dir / f"{body.outcome_id}.json"
        args = [sys.executable, "-u", "-m", "factory.cli", "run", str(path)] + (["--design-only"] if design else [])
        with log.open("w") as fh:
            proc = subprocess.Popen(args, cwd=str(Path(__file__).resolve().parents[2]), stdout=fh, stderr=subprocess.STDOUT,
                                    env={**os.environ, **settings_env(body.settings), "FACTORY_RUNS_DIR": str(self.cfg.runs_dir)}, start_new_session=True)
        return {"ok": True, "mode": body.mode, "pid": proc.pid, "log": log.name, "settings": settings_env(body.settings)}

    def board(self) -> dict[str, Any]:
        """The role-swimlane view across runs: for every run that is not finished (plus finished ones from today), the cards
        in each (column, row): a human's decisions and reviews, and the crew's live sessions."""
        gates = self.queue.open_gates()
        questions = self.queue.open_questions()
        runs = []
        for run_id in self.store.list_runs():
            try:
                events = self.store.events(run_id)
                st = self.store.state(run_id)
            except Exception:
                continue
            if not events:
                continue
            cards: list[dict[str, Any]] = []
            stage = st.stage.value
            column = {"intake": "plan", "decomposing": "plan", "gate_1_decomposition": "plan", "specifying": "specify",
                      "gate_2_spec_readiness": "specify", "implementing": "build", "completed": "ship"}.get(stage, "plan")
            for g in gates.values():
                if g["run_id"] == run_id and g["gate_id"] in st.open_gates and not g["recorded"]:
                    col = "plan" if g["kind"] == "gate_1_decomposition" else "specify" if g["kind"] == "gate_2_spec_readiness" else "ship" if g["kind"] == "deploy_failure" else "specify"
                    row = "ops" if g["kind"] == "deploy_failure" else "po"
                    cards.append({"column": col, "row": row, "kind": "gate", "gate_id": g["gate_id"], "title": g["kind"], "summary": g.get("summary", ""), "since": g.get("opened_at")})
            for q in questions.values():
                if q["run_id"] == run_id and q["question_id"] in st.open_questions and not q["answered"]:
                    row = "eng" if q.get("asked_by") in ("implementer", "reviewer", "security_reviewer", "merge_judge") else "po"
                    cards.append({"column": column, "row": row, "kind": "question", "question_id": q["question_id"], "title": q["question"][:140], "since": q.get("asked_at"), "blocking": q.get("blocking")})
            for wi in st.work_items.values():
                if wi.stage.value == "cr_open":
                    cr = next((ev.payload for ev in events if ev.kind == "cr.opened" and ev.work_item_id == wi.work_item_id), {})
                    cards.append({"column": "ship", "row": "eng", "kind": "pr", "title": f"Review pull request #{wi.cr_id}" if wi.cr_id else "Review the pull request", "url": cr.get("url"), "work_item_id": wi.work_item_id})
                elif wi.stage.value in ("merged", "deploying_dev"):
                    cards.append({"column": "ship", "row": "ops", "kind": "deploy", "title": "Watch the dev deploy", "work_item_id": wi.work_item_id})
            for member in crew_roster(events):
                if member["state"] == "working":
                    col = {"decomposer": "plan", "spec_writer": "specify", "spec_judge": "specify", "implementer": "build",
                           "reviewer": "review", "security_reviewer": "review", "merge_judge": "review", "integrator": "ship"}[member["role"]]
                    cards.append({"column": col, "row": "crew", "kind": "agent", "role": member["role"], "title": member["title"], "since": member["started_at"], "session_id": member["session_id"]})
            runs.append({"run_id": run_id, "title": self._title(run_id, events), "stage": stage, "column": column, "cost": round(st.list_cost_usd, 2),
                         "demo": self.demo.is_demo(run_id), "updated_at": events[-1].at, "cards": cards,
                         "terminal": stage in ("completed", "closed", "failed", "escalated")})
        runs.sort(key=lambda r: (r["terminal"], r["updated_at"]), reverse=False)
        runs.sort(key=lambda r: r["updated_at"] or "", reverse=True)
        return {"columns": ["plan", "specify", "build", "review", "ship"], "rows": ["po", "eng", "ops", "crew"], "runs": runs}

    def resume_run(self, run_id: str) -> dict[str, Any]:
        """Continue a run that failed during implementation: spawns `factory resume <run_id>` the way start_run spawns
        `factory run`, so the Console never holds the orchestrator's state itself."""
        detail = self.run_detail(run_id)
        if not detail["resumable"]:
            raise HTTPException(409, f"run {run_id} is {detail['stage']}; only a failed run that reached implementation can be resumed")
        gaps = cfgmod.missing(self.cfg)
        if gaps:
            raise HTTPException(409, f"the orchestrator is not configured for a live run: {', '.join(gaps)}")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log = self.logs_dir / f"resume-{run_id}-{int(time.time())}.log"
        args = [sys.executable, "-u", "-m", "factory.cli", "resume", run_id]
        with log.open("w") as fh:
            proc = subprocess.Popen(args, cwd=str(Path(__file__).resolve().parents[2]), stdout=fh, stderr=subprocess.STDOUT,
                                    env={**os.environ, "FACTORY_RUNS_DIR": str(self.cfg.runs_dir)}, start_new_session=True)
        return {"ok": True, "mode": "resume", "run_id": run_id, "pid": proc.pid, "log": log.name}

    def watch_run(self, run_id: str) -> dict[str, Any]:
        """Start `factory watch <run_id>` in the background: Gate 3 (the merge on the forge) and Stage 5 (the dev deploy
        and the e2e checks against it) for every open change request of the run. One watcher per run at a time."""
        detail = self.run_detail(run_id)
        watch = detail["watch"]
        if not watch["watchable"]:
            raise HTTPException(409, f"run {run_id} has nothing to watch: no work item has an open pull request, a merge to observe, "
                                     "or a dev deploy in flight" + (" (demo runs are not watched)" if detail["demo"] else ""))
        hb = watch["heartbeat"]
        if hb and hb.get("alive"):
            raise HTTPException(409, f"a watcher is already running for {run_id} (pid {hb.get('pid')}, last poll {hb.get('last_poll_at')})")
        gaps = cfgmod.missing_for_watch(self.cfg)
        if gaps:
            raise HTTPException(409, f"the orchestrator is not configured to watch the forge: {', '.join(gaps)}")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log = self.logs_dir / f"watch-{run_id}-{int(time.time())}.log"
        args = [sys.executable, "-u", "-m", "factory.cli", "watch", run_id]
        with log.open("w") as fh:
            proc = subprocess.Popen(args, cwd=str(Path(__file__).resolve().parents[2]), stdout=fh, stderr=subprocess.STDOUT,
                                    env={**os.environ, "FACTORY_RUNS_DIR": str(self.cfg.runs_dir)}, start_new_session=True)
        return {"ok": True, "mode": "watch", "run_id": run_id, "pid": proc.pid, "log": log.name, "pending": watch["pending"]}

    def logs(self) -> list[dict[str, Any]]:
        if not self.logs_dir.exists():
            return []
        out = []
        for p in sorted(self.logs_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True):
            text = p.read_text(errors="replace")
            m = re.search(r"run (run_[a-z0-9]+) (?:started|resuming|watching)", text)
            out.append({"name": p.name, "bytes": p.stat().st_size, "modified_at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(),
                        "run_id": m.group(1) if m else None, "tail": redact(text[-1500:])})
        return out

    def log(self, name: str) -> str:
        p = self.logs_dir / name
        if not p.is_file() or "/" in name or name.startswith("."):
            raise HTTPException(404, "no such log")
        return redact(p.read_text(errors="replace"))

    def config(self) -> dict[str, Any]:
        gaps = cfgmod.missing(self.cfg)
        design_gaps = cfgmod.missing(self.cfg, design_only=True)
        return {"forge": self.cfg.forge, "forge_repo": self.cfg.forge_repo, "gate_surface": self.cfg.gate_surface,
                "runs_dir": str(self.cfg.runs_dir), "gaps": gaps, "can_run_live": not gaps,
                "design_gaps": design_gaps, "can_run_design": not design_gaps,
                "workspace_id": self.cfg.workspace_id, "console_session_url": self.cfg.console_session_url("SESSION_ID"),
                "agents": {k: {"id": v.id, "version": v.version} for k, v in self.cfg.agents.items()},
                "environment_id": self.cfg.environment_id}

    _platform_cache: tuple[float, dict[str, Any]] | None = None

    def platform(self, max_age: float = 30.0) -> dict[str, Any]:
        """The live platform check (agents found, recent sessions), cached briefly so the Settings page can poll."""
        now = time.time()
        if self._platform_cache and now - self._platform_cache[0] < max_age:
            return self._platform_cache[1]
        out = cfgmod.platform_check(self.cfg)
        out["sessions"] = []
        reachable = os.environ.get("ANTHROPIC_API_KEY") and "ANTHROPIC_WORKSPACE_ID" not in (out["error"] or "")
        if reachable:            # a missing agent is not a reason to hide the sessions that do exist
            try:
                client = cfgmod.anthropic_client(self.cfg)
                page = client.beta.sessions.list(limit=25)
                for s in list(getattr(page, "data", None) or []):     # one page: iterating the cursor would walk the whole history
                    meta = getattr(s, "metadata", None) or {}
                    out["sessions"].append({"id": s.id, "status": getattr(s, "status", None), "title": getattr(s, "title", None),
                                            "created_at": str(getattr(s, "created_at", "") or ""), "run_id": meta.get("run_id") if isinstance(meta, dict) else None,
                                            "stage": meta.get("stage") if isinstance(meta, dict) else None,
                                            "url": self.cfg.console_session_url(s.id)})
            except Exception as e:  # noqa: BLE001
                out["sessions_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        self._platform_cache = (now, out)
        return out


def redact(text: str) -> str:
    """Blank out every secret-looking environment value (tokens, keys) before a log leaves the process: a git
    error can echo an authenticated clone URL, and the Settings page shows these logs."""
    for k, v in os.environ.items():
        if v and len(v) >= 12 and any(t in k.upper() for t in ("TOKEN", "KEY", "SECRET", "PASSWORD")):
            text = text.replace(v, "***")
    return re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1***@", text)


def _trim(payload: dict[str, Any], limit: int = 4000) -> dict[str, Any]:
    s = json.dumps(payload)
    if len(s) <= limit:
        return payload
    return {k: v for k, v in payload.items() if k not in ("work_items", "artifacts", "checks")} | {"_trimmed": True}


def _clean(o: Any) -> Any:
    """Drop empty strings and empty optional fields the form sends, so the schema sees absent rather than ''."""
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            v = _clean(v)
            if v in ("", None) and k not in ("baseline",):
                continue
            out[k] = v
        return out
    if isinstance(o, list):
        return [_clean(x) for x in o if _clean(x) not in ("", None)]
    return o


def create_app(cfg: Config | None = None, demo_thread: bool = True) -> FastAPI:
    cfg = cfg or cfgmod.load()
    console = Console(cfg)
    app = FastAPI(title="Factory Console", docs_url=None, redoc_url=None)
    app.state.console = console
    app.state.demo = console.demo

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/config")
    def api_config():
        return console.config()

    @app.get("/api/runs")
    def api_runs():
        return {"runs": console.runs()}

    @app.get("/api/board")
    def api_board():
        return console.board()

    @app.get("/api/inbox")
    def api_inbox():
        return console.inbox()

    @app.get("/api/runs/{run_id}")
    def api_run(run_id: str):
        return console.run_detail(run_id)

    @app.get("/api/gates/{gate_id}")
    def api_gate(gate_id: str):
        return console.gate(gate_id)

    @app.get("/gates/{gate_id}/page", response_class=HTMLResponse)
    def gate_page(gate_id: str):
        return console.gate_page(gate_id)

    @app.post("/api/gates/{gate_id}/decision")
    def api_decide(gate_id: str, body: DecisionBody):
        return console.decide(gate_id, body)

    @app.post("/api/questions/{question_id}/answer")
    def api_answer(question_id: str, body: AnswerBody):
        return console.answer(question_id, body)

    @app.get("/api/outcomes")
    def api_outcomes():
        return {"outcomes": console.outcomes()}

    @app.get("/api/outcomes/template")
    def api_outcome_template(example: str = "sample-app"):
        return {"outcome": console.outcome_template(example)}

    @app.get("/api/outcomes/examples")
    def api_outcome_examples():
        return {"examples": [{"name": n, "title": json.loads(p.read_text()).get("title"), "path": str(p.relative_to(REPO_ROOT))} for n, p in EXAMPLES.items()]}

    @app.get("/api/outcomes/{outcome_id}")
    def api_outcome(outcome_id: str):
        return {"outcome": console.load_outcome(outcome_id)}

    @app.post("/api/outcomes")
    def api_save_outcome(body: OutcomeBody):
        return console.save_outcome(body.outcome)

    @app.post("/api/runs")
    def api_start_run(body: StartRunBody):
        return console.start_run(body)

    @app.post("/api/runs/{run_id}/resume")
    def api_resume_run(run_id: str):
        return console.resume_run(run_id)

    @app.post("/api/runs/{run_id}/watch")
    def api_watch_run(run_id: str):
        return console.watch_run(run_id)

    @app.get("/api/platform")
    def api_platform():
        return console.platform()

    @app.get("/api/logs")
    def api_logs():
        return {"logs": console.logs()}

    @app.get("/api/logs/{name}", response_class=PlainTextResponse)
    def api_log(name: str):
        return console.log(name)

    @app.post("/api/demo/tick")
    def api_demo_tick():
        return {"ticked": console.demo.tick_all()}

    @app.get("/api/artifacts/{run_id}/{stage}/{name}", response_class=PlainTextResponse)
    def api_artifact(run_id: str, stage: str, name: str):
        p = cfg.runs_dir / run_id / "artifacts" / stage / name
        if not p.is_file() or ".." in (run_id, stage, name):
            raise HTTPException(404, "no such artifact")
        return p.read_text(errors="replace")

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    # Local tool with no auth: refuse requests whose Host is not local, which closes the DNS-rebinding door.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @app.middleware("http")
    async def no_store(request, call_next):
        # A local tool that is edited and reloaded; never let a browser hold on to yesterday's script.
        response = await call_next(request)
        if request.url.path.startswith("/static") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-cache"
        return response

    if demo_thread:
        def loop() -> None:
            while True:
                try:
                    console.demo.tick_all()
                except Exception:
                    pass
                time.sleep(1.5)
        threading.Thread(target=loop, name="factory-demo", daemon=True).start()
    return app
