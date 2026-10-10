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
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
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
from .demo import DemoRunner

STATIC = Path(__file__).parent / "static"
TIMELINE_KINDS = {"run.created", "stage.entered", "gate.opened", "gate.decided", "question.asked", "question.answered",
                  "judge.verdict", "review.findings", "review.response", "merge.verdict", "cr.opened", "cr.merged", "cr.closed",
                  "deploy.started", "deploy.succeeded", "deploy.failed", "e2e.dev_passed", "e2e.dev_failed", "budget.reached",
                  "run.escalated", "run.failed", "run.completed", "session.finished", "work_item.stage"}


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
    mode: str = "demo"          # demo | live


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
            })
        out.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
        return out

    def run_detail(self, run_id: str) -> dict[str, Any]:
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
                  "spec_id": wi.spec_id, "branch": wi.branch, "cr_id": wi.cr_id, "depends_on": wi.depends_on,
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
        return {"run_id": run_id, "title": self._title(run_id, events), "outcome": outcome, "stage": st.stage.value,
                "cost": round(st.list_cost_usd, 2), "demo": self.demo.is_demo(run_id), "work_items": items,
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
    def outcome_template(self) -> dict[str, Any]:
        ex = self.demo.example_outcome()
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
        p = self.outcomes_dir / f"{outcome_id}.json"
        if not p.exists():
            raise HTTPException(404, f"no saved outcome {outcome_id}")
        return json.loads(p.read_text())

    def start_run(self, body: StartRunBody) -> dict[str, Any]:
        outcome = self.load_outcome(body.outcome_id)
        if body.mode == "demo":
            run_id = self.demo.seed(outcome)
            return {"ok": True, "mode": "demo", "run_id": run_id}
        if body.mode != "live":
            raise HTTPException(400, "mode must be demo or live")
        gaps = cfgmod.missing(self.cfg)
        if gaps:
            raise HTTPException(409, f"the orchestrator is not configured for a live run: {', '.join(gaps)}")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log = self.logs_dir / f"{body.outcome_id}-{int(time.time())}.log"
        path = self.outcomes_dir / f"{body.outcome_id}.json"
        with log.open("w") as fh:
            proc = subprocess.Popen([sys.executable, "-m", "factory.cli", "run", str(path)], cwd=str(Path(__file__).resolve().parents[2]),
                                    stdout=fh, stderr=subprocess.STDOUT, env={**os.environ, "FACTORY_RUNS_DIR": str(self.cfg.runs_dir)},
                                    start_new_session=True)
        return {"ok": True, "mode": "live", "pid": proc.pid, "log": str(log)}

    def config(self) -> dict[str, Any]:
        gaps = cfgmod.missing(self.cfg)
        return {"forge": self.cfg.forge, "forge_repo": self.cfg.forge_repo, "gate_surface": self.cfg.gate_surface,
                "runs_dir": str(self.cfg.runs_dir), "gaps": gaps, "can_run_live": not gaps}


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
    def api_outcome_template():
        return {"outcome": console.outcome_template()}

    @app.get("/api/outcomes/{outcome_id}")
    def api_outcome(outcome_id: str):
        return {"outcome": console.load_outcome(outcome_id)}

    @app.post("/api/outcomes")
    def api_save_outcome(body: OutcomeBody):
        return console.save_outcome(body.outcome)

    @app.post("/api/runs")
    def api_start_run(body: StartRunBody):
        return console.start_run(body)

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
