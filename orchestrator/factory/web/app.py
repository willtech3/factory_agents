"""The Factory Console (v0): a local web app over the run store and the file queue.

What it is for: writing an outcome, watching runs, and deciding Gate 1 and Gate 2 (and answering blocking
questions) from a page that puts the judge's summary first and binds the decision to the artifact hashes
the page showed (ADR-0005). It is the `web` gate surface: it reads `runs/<run_id>/events.jsonl` and the
artifacts, and writes decisions and answers to `runs/_queue/`, exactly where `factory decide` writes them,
so a running `factory run` picks them up unchanged. No agent, forge, or tracker call happens here.

Routes: `/` and `/static/*` serve the UI; `/api/*` is JSON; `/gates/<id>/page` is the standalone artifact
page (the same renderer as `surfaces/artifact_pages.py`). The home page is the run history: every run under the
runs directory with its status and what came out of it, derived from the event log by `summary.py`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr

from .. import visual
from ..approvals import presented_specs, subset_problems
from .. import checks
from .. import config as cfgmod
from ..budget import last_pause
from ..config import REPO_ROOT, Config
from ..gates import Decision, GateKind, parse_budget
from ..state import RunStage, replay
from ..store import RunStore
from ..summary import controller_running, summarize
from ..surfaces.artifact_pages import render_gate1_page, render_gate2_page
from ..surfaces.filequeue import FileQueue
from ..watcher import heartbeat as watcher_heartbeat
from ..watcher import pending_work_items
from .repositories import list_repositories, repository_name

STATIC = Path(__file__).parent / "static"
RUN_ID = re.compile(r"^run_[a-z0-9]{8,}$")
OUTCOME_ID = re.compile(r"^out_[a-z0-9]{8,}$")
# Every examples/<name>/outcome.json is offered as a starting point in the outcome form.
EXAMPLES = {p.parent.name: p for p in sorted((REPO_ROOT / "examples").glob("*/outcome.json"))}
TIMELINE_KINDS = {"run.created", "repository.created", "stage.entered", "session.started", "gate.opened", "gate.decided", "question.asked", "question.answered",
                  "judge.verdict", "review.findings", "review.response", "merge.verdict", "cr.opened", "cr.merged", "cr.closed",
                  "deploy.started", "deploy.succeeded", "deploy.failed", "e2e.dev_passed", "e2e.dev_failed", "budget.reached",
                  "budget.raised", "session.truncated", "run.escalated", "run.failed", "run.resumed", "run.completed", "session.finished",
                  "work_item.stage"}


class AppBody(BaseModel):
    kind: str
    name: str = Field(min_length=1, max_length=160)
    repository_url: str = Field(max_length=500)
    description: str = Field(min_length=1, max_length=12000)
    budget_usd: float = 10
    revision: str = ""


class AppPlanBody(BaseModel):
    plan_sha256: str


class AppAnswersBody(BaseModel):
    answers: dict[str, str]


class AppFeedbackBody(BaseModel):
    text: str


class AppCredentialBody(BaseModel):
    name: str
    value: SecretStr


class EnvironmentCleanupBody(BaseModel):
    environment_ids: list[str] = Field(max_length=200)


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
    app_id: str | None = None
    mode: str = "live"          # live | design
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


def local_activity_url(runs_dir: Path, activity_id: str | None) -> str | None:
    """Workspace sessions outlive local checkouts; only link to records this Console can open."""
    if not isinstance(activity_id, str):
        return None
    if RUN_ID.fullmatch(activity_id) and (runs_dir / activity_id / "events.jsonl").is_file():
        return f"#/runs/{activity_id}"
    if re.fullmatch(r"app_[a-f0-9]{12}", activity_id) and (runs_dir / "_apps" / activity_id / "app.json").is_file():
        return f"#/apps/{activity_id}"
    return None


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
        elif ev.kind == "run.resumed":
            # a resume starts new sessions; whatever its dead predecessor left open is not being worked on
            for row in by_role.values():
                if row["live"]:
                    row["live"].clear()
                    row["finished_at"] = ev.at
                    row["state"] = "done"
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


class ResumeBody(BaseModel):
    budget_usd: float | None = None     # a run paused at its budget: the new cap (records the gate decision too)


class Console:
    """Everything the routes need, bound to one runs directory."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = RunStore(cfg.runs_dir)
        self.queue = FileQueue(cfg.runs_dir / "_queue")
        # run_id -> ((log size, log mtime, controller alive), summary): the history replays a log only when it changed
        self._summaries: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
        self.outcomes_dir = cfg.runs_dir / "_outcomes"
        self.logs_dir = cfg.runs_dir / "_logs"

    # ---- runs
    def _title(self, run_id: str, events=None) -> str:
        for ev in events or self.store.events(run_id):
            if ev.kind == "run.created":
                return ev.payload.get("title") or run_id
        return run_id

    def summary(self, run_id: str) -> dict[str, Any]:
        """`summary.summarize` for one run, cached on the event log's size and mtime and on whether a controller holds
        the run's lock. A finished run is replayed once per Console process; the Runs page's poll then costs a stat
        and one non-blocking flock attempt per run."""
        log = self.cfg.runs_dir / run_id / "events.jsonl"
        st = log.stat()
        alive = controller_running(self.cfg.runs_dir, run_id)
        key = (st.st_size, st.st_mtime_ns, alive)
        hit = self._summaries.get(run_id)
        if hit is None or hit[0] != key:
            events = self.store.events(run_id)
            hit = (key, summarize(events, replay(run_id, events), alive))
            self._summaries[run_id] = hit
        return dict(hit[1])

    def runs(self) -> list[dict[str, Any]]:
        """The run history: one record per run with its status and what came out of it, plus what the queue says is
        still waiting on a person. Runs that are not finished come first, then by last activity."""
        gates = self.queue.open_gates()
        questions = self.queue.open_questions()
        out = []
        for run_id in self.store.list_runs():
            try:
                s = self.summary(run_id)
            except Exception as e:  # a corrupt log should not hide the other runs
                try:
                    title = self._title(run_id)
                except Exception:  # noqa: BLE001
                    title = run_id
                out.append({"run_id": run_id, "title": title, "stage": "unreadable", "status": "unreadable", "status_label": "Unreadable",
                            "headline": f"The event log could not be read: {str(e)[:160]}", "terminal": True, "error": str(e)})
                continue
            open_gates, open_questions = set(s.pop("open_gate_ids")), set(s.pop("open_question_ids"))
            s["waiting"] = {"gates": sum(1 for g in gates.values() if g["run_id"] == run_id and g["gate_id"] in open_gates and not g["recorded"]),
                            "questions": sum(1 for q in questions.values() if q["run_id"] == run_id and not q["answered"]
                                             and q["question_id"] in open_questions)}
            out.append(s)
        out.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
        out.sort(key=lambda r: bool(r.get("terminal")))
        return out

    def run_detail(self, run_id: str) -> dict[str, Any]:
        if not RUN_ID.match(run_id) or not (self.cfg.runs_dir / run_id / "events.jsonl").is_file():
            raise HTTPException(404, f"no run {run_id}")       # checked first: RunStore.events would create the directory
        events = self.store.events(run_id)
        if not any(ev.kind == "run.created" for ev in events):
            # empty, or stopped between creating its repository (`repository.created`) and starting (`run.created`)
            raise HTTPException(404, f"run {run_id} never started")
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
        # A run that died in the implementation stage, or paused at its run budget, continues from its artefacts
        # (`factory resume`); a budget pause first needs the owner's decision on the budget gate. A run paused because
        # one session hit its own cap is still being driven: its orchestrator waits on that gate, so nothing to resume.
        budget = self._budget(st, events, gates)
        paused = budget["paused"]
        resumable = paused or (
            st.stage == RunStage.FAILED
            and any(ev.kind == "stage.entered" and ev.payload.get("stage") == "implementing" for ev in events))
        # Gate 3 and Stage 5 are followed by `factory watch`, a separate process; its heartbeat says whether one is running.
        pending = [wi.work_item_id for wi in pending_work_items(st)]
        watch = {"pending": pending, "watchable": bool(pending), "heartbeat": watcher_heartbeat(self.cfg.runs_dir, run_id)}
        summary = summarize(events, st, controller_running(self.cfg.runs_dir, run_id))
        for key in ("open_gate_ids", "open_question_ids"):
            summary.pop(key, None)
        created = next((ev.payload for ev in events if ev.kind == "run.created"), {})
        policy = dict(created["policy"])
        # A controller upgrade can move an existing run to hosted execution at an idle gate.
        # Show the effective settings, keeping the immutable creation event as history.
        for ev in events:
            if ev.kind == "run.resumed" and ev.payload.get("preview_mode"):
                for key in ("preview_mode", "app_id", "test_profile_sha256"):
                    if key in ev.payload:
                        policy[key] = ev.payload[key]
        return {"run_id": run_id, "title": self._title(run_id, events), "outcome": outcome, "stage": st.stage.value,
                "status": summary["status"], "summary": summary,
                "cost": round(st.list_cost_usd, 2), "design_only": design_only, "work_items": items,
                "resumable": resumable, "budget": budget, "resuming": self.resuming(run_id), "watch": watch, "crew": crew_roster(events), "policy": policy,
                "gates": gates, "questions": questions, "events": timeline, "artifacts": self._artifacts(run_id),
                "browser_checks_pending": visual.pending(self.cfg.runs_dir / run_id),
                "visual_evidence": visual.summaries(self.cfg.runs_dir / run_id / "artifacts", events),
                "created_at": events[0].at, "updated_at": events[-1].at}

    def _budget(self, st, events, open_gates: list[dict[str, Any]]) -> dict[str, Any]:
        """The run's cap and spend, and when it is paused at the cap, what the pause needs and the gate deciding it."""
        pause = last_pause(events) if st.stage == RunStage.PAUSED_BUDGET else None
        gate = next((g for g in open_gates if g["kind"] == GateKind.BUDGET.value), None) if pause else None
        return {"budget_usd": st.budget_usd, "spent_usd": round(st.list_cost_usd, 2), "sessions": st.sessions_finished,
                "paused": pause is not None, "pause": pause, "gate_id": gate["gate_id"] if gate else None,
                "decision": (gate or {}).get("recorded")}

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
                        # the runner stores the gate's own spec.md as final-<spec_id>.md (absent when it is empty)
                        final = self.cfg.runs_dir / run_id / "artifacts" / "specify" / f"final-{s['spec_id']}.md"
                        spec_md[s["spec_id"]] = final.read_text(errors="replace") if final.is_file() else ""
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
        if kind == GateKind.BUDGET.value:
            record = self._budget_record(rec)
            if record is None:          # a session's own cap (run_session): the summary says it all
                return {"kind": "other", "outcome": outcome}
            st = self.store.state(run_id)
            return {"kind": "budget", "outcome": outcome, "budget": record,
                    "run_stage": st.stage.value, "spent_now_usd": round(st.list_cost_usd, 2), "budget_now_usd": st.budget_usd}
        return {"kind": "other", "outcome": outcome}

    def _budget_record(self, rec: dict[str, Any]) -> dict[str, Any] | None:
        """The paused-N.json the budget gate presented (spent, cap, what the next step needs)."""
        for a in rec["artifacts"]:
            p = self.artifact_by_sha(rec["run_id"], a["sha256"])
            if p is not None and p.name.startswith("paused-"):
                return json.loads(p.read_text())
        return None

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
        if decision == Decision.APPROVE_SUBSET and rec["kind"] in (GateKind.SPEC_READINESS.value, GateKind.ESCALATION.value):
            try:                            # the exact specs this gate presented, by the sha256 it showed
                presented = presented_specs(self.cfg.runs_dir, rec["run_id"], rec["artifacts"])
            except ValueError as e:
                raise HTTPException(409, f"cannot check the selection: {e}")
            problems = subset_problems(presented, body.approved_subset)
            if problems:
                raise HTTPException(409, "This selection cannot be approved: " + "; ".join(problems)
                                    + ". Select the prerequisites too, or use Request changes.")
        if rec["kind"] == GateKind.BUDGET.value and decision == Decision.APPROVE and self._budget_record(rec) is not None:
            self._check_new_budget(rec, parse_budget(body.comments))     # the run cap: approve needs the new budget
        entry = self.queue.record_decision(gate_id, decision.value, body.by or "console", body.comments, body.approved_subset)
        return {"ok": True, "recorded": entry}

    def _check_new_budget(self, rec: dict[str, Any], new: float | None) -> float:
        record = self._budget_record(rec) or {}
        needed = float(record.get("needed_usd") or 0)
        if new is None:
            raise HTTPException(400, "approving a budget gate needs the new budget, e.g. `budget_usd: 80`")
        if new < needed:
            raise HTTPException(400, f"the next step needs at least ${needed:.2f}; ${new:.2f} would pause the run again at once")
        return new

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

    def outcomes(self, include_runs: bool = True) -> list[dict[str, Any]]:
        """Saved outcomes, newest first, with the runs started from each (which reads every run's log; a caller that
        needs only the outcomes passes include_runs=False)."""
        self.outcomes_dir.mkdir(parents=True, exist_ok=True)
        runs_by_outcome: dict[str, list[str]] = {}
        for run_id in self.store.list_runs() if include_runs else []:
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
                        "owner": (o.get("owner") or {}).get("handle"), "owner_name": (o.get("owner") or {}).get("name"),
                        "runs": runs_by_outcome.get(o.get("outcome_id"), []),
                        "target_repositories": o.get("target_repositories", []),
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
        field_errors = outcome_field_errors(outcome, self.cfg.schemas_dir)
        if field_errors:
            return {"ok": False, "errors": [e["message"] for e in field_errors], "field_errors": field_errors, "outcome": outcome}
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
        if body.mode not in ("live", "design"):
            raise HTTPException(400, "mode must be live or design")
        design = body.mode == "design"
        repo_env = {}
        if self.cfg.forge_token:
            # Config may receive the authenticated token from the Console launcher instead of os.environ.
            # Forward it only to the orchestrator child; never include it in the API response.
            repo_env["FORGE_TOKEN"] = self.cfg.forge_token
        target = (outcome.get("target_repositories") or [{}])[0]
        if not design and self.cfg.forge == "github" and target.get("url"):
            # The Console selection is per run; do not inherit a different repository from server defaults.
            repo_env["FORGE_REPO"] = repository_name(target["url"], self.cfg)
        binding = None
        if body.app_id or not design:
            # Every live start path binds an onboarded repository to its verified hosted setup, server-side: a change to
            # an onboarded app never runs with local or platform verification because the client did not name the app.
            from ..onboarding import AppOnboarding
            binding = AppOnboarding(self.cfg).binding_for(target, body.app_id)
            if binding:
                repo_env.update(binding)
        gaps = cfgmod.missing(self.cfg, design_only=design)
        if gaps:
            raise HTTPException(409, f"the orchestrator is not configured for a {body.mode} run: {', '.join(gaps)}")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log = self.logs_dir / f"{body.outcome_id}-{int(time.time())}.log"
        path = self.outcomes_dir / f"{body.outcome_id}.json"
        args = [sys.executable, "-u", "-m", "factory.cli", "run", str(path)] + (["--design-only"] if design else [])
        with log.open("w") as fh:
            proc = subprocess.Popen(args, cwd=str(Path(__file__).resolve().parents[2]), stdout=fh, stderr=subprocess.STDOUT,
                                    env={**os.environ, **settings_env(body.settings), **repo_env, "FACTORY_RUNS_DIR": str(self.cfg.runs_dir)}, start_new_session=True)
        settings = {**settings_env(body.settings), **({"FACTORY_PREVIEW": "managed"} if binding else {})}
        return {"ok": True, "mode": body.mode, "pid": proc.pid, "log": log.name, "settings": settings,
                "app_id": binding.get("FACTORY_APP_ID") if binding else None, "preview": "managed" if binding else settings.get("FACTORY_PREVIEW")}

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
                         "updated_at": events[-1].at, "cards": cards,
                         "terminal": stage in ("completed", "closed", "failed", "escalated")})
        runs.sort(key=lambda r: (r["terminal"], r["updated_at"]), reverse=False)
        runs.sort(key=lambda r: r["updated_at"] or "", reverse=True)
        return {"columns": ["plan", "specify", "build", "review", "ship"], "rows": ["po", "eng", "ops", "crew"], "runs": runs}

    def resume_run(self, run_id: str, body: ResumeBody | None = None) -> dict[str, Any]:
        """Continue a run that failed during implementation or paused at its budget: spawns `factory resume <run_id>`
        the way start_run spawns `factory run`, so the Console never holds the orchestrator's state itself. For a
        budget pause the owner's decision must exist first; `budget_usd` in the body records an approve with that
        cap on the budget gate before spawning, so one click raises and continues."""
        detail = self.run_detail(run_id)
        if not detail["resumable"]:
            raise HTTPException(409, f"run {run_id} is {detail['stage']}; only a failed run that reached implementation, "
                                     f"or a run paused at its budget, can be resumed")
        live = self.resuming(run_id)
        if live:
            raise HTTPException(409, f"run {run_id} is already being resumed (pid {live})")
        budget = detail["budget"]
        new_budget = body.budget_usd if body is not None else None
        if budget["paused"]:
            gate_id, recorded = budget["gate_id"], budget["decision"]
            if recorded is None and new_budget is None:
                raise HTTPException(409, "the run is paused at its budget: decide the budget gate first (raise it or close the run)")
            if recorded is None:
                rec = self.queue.open_gates().get(gate_id) or {}
                self._check_new_budget(rec, new_budget)
                self.queue.record_decision(gate_id, Decision.APPROVE.value, "console", f"budget_usd: {new_budget:g}")
            elif new_budget is not None and parse_budget(recorded.get("comments")) != new_budget:
                raise HTTPException(409, "a different decision is already recorded on the budget gate; resume without a new budget to apply it")
        gaps = cfgmod.missing(self.cfg, design_only=detail["design_only"])
        if gaps:
            raise HTTPException(409, f"the orchestrator is not configured for a live run: {', '.join(gaps)}")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log = self.logs_dir / f"resume-{run_id}-{int(time.time())}.log"
        args = [sys.executable, "-u", "-m", "factory.cli", "resume", run_id] + ([f"--budget={new_budget:g}"] if new_budget is not None else [])
        with log.open("w") as fh:
            proc = subprocess.Popen(args, cwd=str(Path(__file__).resolve().parents[2]), stdout=fh, stderr=subprocess.STDOUT,
                                    env={**os.environ, "FACTORY_RUNS_DIR": str(self.cfg.runs_dir)}, start_new_session=True)
        (self.cfg.runs_dir / run_id / "resume.pid").write_text(str(proc.pid))
        return {"ok": True, "mode": "resume", "run_id": run_id, "pid": proc.pid, "log": log.name}

    def resuming(self, run_id: str) -> int | None:
        """The pid of a `factory resume` this Console started for the run and that is still alive, else None.
        A second click on Resume must never start a second orchestrator on the same run."""
        p = self.cfg.runs_dir / run_id / "resume.pid"
        try:
            pid = int(p.read_text().strip())
        except (OSError, ValueError):
            return None
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        try:                                            # a zombie the Console has not reaped yet is not alive
            if os.waitpid(pid, os.WNOHANG) != (0, 0):
                return None
        except ChildProcessError:
            pass
        return pid

    def watch_run(self, run_id: str) -> dict[str, Any]:
        """Start `factory watch <run_id>` in the background: Gate 3 (the merge on the forge) and Stage 5 (the dev deploy
        and the e2e checks against it) for every open change request of the run. One watcher per run at a time."""
        detail = self.run_detail(run_id)
        watch = detail["watch"]
        if not watch["watchable"]:
            raise HTTPException(409, f"run {run_id} has nothing to watch: no work item has an open pull request, a merge to observe, "
                                     "or a dev deploy in flight")
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
                # the security pass runs only when its optional agent is deployed (implement.py skips it otherwise)
                "security_review_available": "security_reviewer" in self.cfg.agents,
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
                    activity_id = meta.get("run_id") if isinstance(meta, dict) else None
                    out["sessions"].append({"id": s.id, "status": getattr(s, "status", None), "title": getattr(s, "title", None),
                                            "created_at": str(getattr(s, "created_at", "") or ""), "run_id": activity_id,
                                            "local_url": local_activity_url(self.cfg.runs_dir, activity_id),
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


# What the outcome form calls each field, by JSON pointer with list indexes as `*`.
OUTCOME_FIELDS = {
    "title": "The short title", "statement": "The outcome statement", "success_metrics": "Success metrics",
    "success_metrics/*/metric": "The metric", "success_metrics/*/target": "The target",
    "success_metrics/*/baseline": "The baseline", "success_metrics/*/measurement": "How it is measured",
    "owner": "The owner", "owner/name": "The owner's name", "owner/handle": "The owner's code host handle",
    "target_repositories/*/url": "The repository URL", "target_repositories/*/default_branch": "The default branch",
    "constraints/budget_usd": "The budget", "constraints/deadline": "The deadline",
}


def outcome_field_errors(outcome: dict[str, Any], schemas_dir: Path) -> list[dict[str, str]]:
    """Why an outcome cannot be saved, one entry per form field: `field` is a JSON pointer into the outcome
    (`success_metrics/0/target`) so the form can mark that control and put `message` beside it, in the form's words
    rather than the schema's."""
    def label(path: list[Any]) -> str:
        key = "/".join("*" if isinstance(x, int) else str(x) for x in path)
        return OUTCOME_FIELDS.get(key, "This field" if path else "The outcome")

    out: list[dict[str, str]] = []
    if not (outcome.get("success_metrics") or []):
        out.append({"field": "success_metrics", "message": "Add at least one success metric with a target."})
    for e in checks.schema_errors(outcome, schemas_dir / "outcome.schema.json", schemas_dir):
        path = list(e.path)
        if e.validator == "required":
            for name in e.validator_value:
                if isinstance(e.instance, dict) and name not in e.instance:
                    out.append({"field": "/".join(map(str, path + [name])), "message": f"{label(path + [name])} is required."})
            continue
        if e.validator == "minItems" and path == ["success_metrics"]:
            continue                                    # said once above
        n = e.validator_value
        message = {"minLength": f"{label(path)} needs at least {n} characters; it has {len(e.instance) if isinstance(e.instance, str) else 0}.",
                   "maxLength": f"{label(path)} can be at most {n} characters.",
                   "type": f"{label(path)} has the wrong kind of value.",
                   "format": f"{label(path)} is not a valid {n}."}.get(e.validator, f"{label(path)}: {e.message}")
        out.append({"field": "/".join(map(str, path)), "message": message})
    # jsonschema reports `required` once per missing property, each carrying the whole list: keep each field once
    unique: list[dict[str, str]] = []
    for e in out:
        if e not in unique:
            unique.append(e)
    return unique


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


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or cfgmod.load()
    console = Console(cfg)
    app = FastAPI(title="Factory Console", docs_url=None, redoc_url=None)
    app.state.console = console
    from ..onboarding import AppOnboarding
    onboarding = AppOnboarding(cfg)
    app.state.onboarding = onboarding
    from ..app_environments import AppEnvironments
    environments = AppEnvironments(cfg, onboarding)
    app.state.environments = environments

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/config")
    def api_config():
        return console.config()

    @app.get("/api/repositories")
    def api_repositories(page: int = Query(1, ge=1, le=10000)):
        return list_repositories(console.cfg, page)

    @app.get("/api/apps")
    def api_apps():
        return {"apps": [onboarding.refresh_setup_pr(a) for a in onboarding.list()]}

    @app.post("/api/apps")
    def api_create_app(body: AppBody):
        return onboarding.create(**body.model_dump())

    # Declared before /api/apps/{app_id}: the apps' execution environments, read from the shared workspace.
    @app.get("/api/apps/environments")
    def api_app_environments(refresh: bool = False):
        return environments.snapshot(refresh=refresh)

    @app.post("/api/apps/environments/cleanup")
    def api_app_environments_cleanup(body: EnvironmentCleanupBody):
        return environments.cleanup(body.environment_ids)

    @app.post("/api/apps/environments/{environment_id}/archive")
    def api_app_environment_archive(environment_id: str):
        return environments.archive(environment_id)

    @app.get("/api/apps/{app_id}")
    def api_app(app_id: str):
        return onboarding.refresh_setup_pr(onboarding.get(app_id))

    @app.post("/api/apps/{app_id}/discover")
    def api_app_discover(app_id: str):
        return onboarding.start(app_id, "discover")

    @app.post("/api/apps/{app_id}/answers")
    def api_app_answers(app_id: str, body: AppAnswersBody):
        return onboarding.answers(app_id, body.answers)

    @app.post("/api/apps/{app_id}/verify")
    def api_app_verify(app_id: str, body: AppPlanBody):
        return onboarding.start(app_id, "verify", body.plan_sha256)

    @app.post("/api/apps/{app_id}/publish")
    def api_app_publish(app_id: str, body: AppPlanBody):
        return onboarding.start(app_id, "publish", body.plan_sha256)

    @app.post("/api/apps/{app_id}/feedback")
    def api_app_feedback(app_id: str, body: AppFeedbackBody):
        return onboarding.feedback(app_id, body.text)

    @app.post("/api/apps/{app_id}/recover")
    def api_app_recover(app_id: str):
        return onboarding.recover(app_id)

    @app.post("/api/apps/{app_id}/credentials")
    def api_app_credential(app_id: str, body: AppCredentialBody):
        return onboarding.credential(app_id, body.name, body.value.get_secret_value())

    @app.get("/api/apps/{app_id}/evidence/{name}")
    def api_app_evidence(app_id: str, name: str):
        p = onboarding.evidence(app_id, name)
        return FileResponse(p, media_type=visual.media_type(p) or "application/json",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

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
    def api_outcomes(runs: bool = True):
        return {"outcomes": console.outcomes(include_runs=runs)}

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
    def api_resume_run(run_id: str, body: ResumeBody | None = None):
        return console.resume_run(run_id, body)

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

    @app.get("/api/artifacts/{run_id}/{stage}/{name:path}")
    def api_artifact(run_id: str, stage: str, name: str):
        root = (cfg.runs_dir / run_id / "artifacts").resolve()
        p = root / stage / name
        if (not root.is_relative_to(cfg.runs_dir.resolve()) or not p.is_file() or p.is_symlink()
                or not p.resolve().is_relative_to(root)
                or any(x in (".", "..") for x in (run_id, stage, *Path(name).parts))):
            raise HTTPException(404, "no such artifact")
        mime = visual.media_type(p)
        if mime:
            # FileResponse supports range requests: video seeking works without loading the whole file.
            return FileResponse(p, media_type=mime, headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
        return PlainTextResponse(p.read_text(errors="replace"), headers={"X-Content-Type-Options": "nosniff"})

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    # Local tool with no auth: refuse requests whose Host is not local, which closes the DNS-rebinding door.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @app.middleware("http")
    async def no_store(request, call_next):
        # App setup can spend money and bind credentials. Refuse cross-origin mutation.
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/apps"):
            from urllib.parse import urlparse
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return PlainTextResponse("Cross-origin app changes are not allowed", status_code=403)
            if "application/json" not in request.headers.get("content-type", ""):
                return PlainTextResponse("App changes require JSON", status_code=415)
        # A local tool that is edited and reloaded; never let a browser hold on to yesterday's script.
        response = await call_next(request)
        if request.url.path.startswith("/static") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-cache"
        return response

    return app
