"""`factory` command line.

    factory doctor                      # reconcile config: agents, environment, forge, gate surface
    factory run outcome.json            # start a run (blocks at gates; use a second terminal to decide)
    factory gates                       # list open gates and questions for all runs
    factory decide <gate_id> approve|request_changes|reject [--comment ...] [--spec spec_x ...]
    factory answer <question_id> "text"
    factory status <run_id>             # derived state + timeline
    factory replay <run_id>             # re-derive state from the event log (sanity check)

Local mode uses a file-backed queue under the runs directory so two processes can talk. With
`FACTORY_GATE_SURFACE=tracker` the same gate is also opened on the tracker (Linear) and a decision
from either side wins (`CompositeQueue`).
"""
from __future__ import annotations

import json
from pathlib import Path

import typer

from . import config as cfgmod
from .gates import Answer, CompositeNotifier, CompositeQueue, Decision, GateDecision, InMemoryQueue, LogNotifier
from .store import RunStore

app = typer.Typer(no_args_is_help=True, add_completion=False)


class FileQueue(InMemoryQueue):
    """InMemoryQueue persisted as JSON files so `factory decide` in another process is seen by `factory run`."""

    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _load(self, name: str) -> dict:
        p = self.root / name
        return json.loads(p.read_text()) if p.exists() else {}

    def _save(self, name: str, data: dict) -> None:
        (self.root / name).write_text(json.dumps(data, indent=2, default=lambda o: getattr(o, "value", o.__dict__)))

    def open(self, gate):
        super().open(gate)
        d = self._load("gates.json")
        d[gate.gate_id] = {"kind": gate.kind.value, "run_id": gate.run_id, "summary": gate.summary,
                           "artifacts": [a.__dict__ for a in gate.artifacts], "options": [o.value for o in gate.options],
                           "assignees": list(gate.assignees), "opened_at": gate.opened_at}
        self._save("gates.json", d)

    def decision(self, gate_id):
        d = self._load("decisions.json").get(gate_id)
        if d is None:
            return None
        dec = GateDecision(gate_id=gate_id, decision=Decision(d["decision"]), decided_by=d["decided_by"],
                           comments=d.get("comments", ""), approved_subset=tuple(d.get("approved_subset", [])),
                           artifact_shas=tuple(d.get("artifact_shas", [])))
        self.decisions[gate_id] = dec
        return super().decision(gate_id)

    def ask(self, q):
        super().ask(q)
        d = self._load("questions.json")
        d[q.question_id] = {"run_id": q.run_id, "asked_by": q.asked_by, "question": q.question, "why": q.why_it_matters,
                            "default": q.default_if_unanswered, "blocking": q.blocking, "options": list(q.options)}
        self._save("questions.json", d)

    def answer(self, question_id):
        a = self._load("answers.json").get(question_id)
        return Answer(question_id=question_id, answer=a["answer"], answered_by=a["answered_by"]) if a else None


def _queue(cfg) -> FileQueue:
    return FileQueue(cfg.runs_dir / "_queue")


@app.command()
def doctor():
    """Check that everything a run needs exists."""
    cfg = cfgmod.load()
    gaps = cfgmod.missing(cfg)
    for k, ref in cfg.agents.items():
        typer.echo(f"agent {k:20s} {ref.id} v{ref.version}")
    typer.echo(f"environment {cfg.environment_id or '(missing)'}")
    typer.echo(f"forge        {cfg.forge} {cfg.forge_url} {cfg.forge_repo or '(FORGE_REPO missing)'} "
               f"token={'set' if cfg.forge_token else 'MISSING'}")
    typer.echo(f"gate surface {cfg.gate_surface}" + (f" (Linear team {cfg.linear_team_id})" if cfg.gate_surface == "tracker" else ""))
    typer.echo(f"pages        {cfg.page_publisher} -> {cfg.runs_dir}/<run_id>/pages/")
    if cfg.forge == "gitlab":
        typer.echo(f"vault        {cfg.vault_id or '(FACTORY_VAULT_ID missing)'}")
    if gaps:
        typer.echo("missing: " + ", ".join(gaps))
        raise typer.Exit(1)
    typer.echo("ok")


@app.command()
def run(outcome_path: Path, forge: bool = typer.Option(True, help="Talk to the forge (change requests, pipelines)")):
    """Start a run from an outcome.json. Blocks at human gates; decide from another terminal or from the tracker."""
    from .forge import make_forge
    from .runner import new_run_id, run as run_pipeline
    from .sessions import SessionDriver
    from .stages import RunContext
    from .state import Policy
    from .surfaces.artifact_pages import make_publisher
    from .surfaces.tracker import make_tracker_queue

    cfg = cfgmod.load()
    gaps = cfgmod.missing(cfg)
    if gaps:
        typer.echo("cannot run; missing: " + ", ".join(gaps))
        raise typer.Exit(1)
    outcome = json.loads(outcome_path.read_text())
    run_id = new_run_id()
    queue, notifier = _queue(cfg), LogNotifier()
    tracker = make_tracker_queue(cfg)
    if tracker is not None:
        queue, notifier = CompositeQueue(queue, tracker), CompositeNotifier(notifier, tracker)
    ctx = RunContext(
        cfg=cfg, store=RunStore(cfg.runs_dir), driver=SessionDriver(cfg), queue=queue, notifier=notifier,
        forge=make_forge(cfg) if forge else None,
        policy=Policy(run_budget_usd=outcome.get("constraints", {}).get("budget_usd") or 150.0), run_id=run_id,
        tracker=tracker, pages=make_publisher(cfg),
    )
    typer.echo(f"run {run_id} started (forge={cfg.forge}, gate surface={cfg.gate_surface})")
    final = run_pipeline(ctx, outcome)
    typer.echo(f"run {run_id} finished in state {final.value}")


@app.command()
def gates():
    """List open gates and unanswered questions."""
    cfg = cfgmod.load()
    q = _queue(cfg)
    decided = q._load("decisions.json")
    for gid, g in q._load("gates.json").items():
        if gid in decided:
            continue
        typer.echo(f"\n{gid}  {g['kind']}  run={g['run_id']}  options={g['options']}")
        typer.echo("  " + g["summary"][:400].replace("\n", "\n  "))
        for a in g["artifacts"]:
            typer.echo(f"  - {a['name']}  sha256={a['sha256'][:12]}  {a.get('url') or ''}")
    answered = q._load("answers.json")
    for qid, qq in q._load("questions.json").items():
        if qid in answered:
            continue
        typer.echo(f"\n{qid}  question from {qq['asked_by']} (run {qq['run_id']}, blocking={qq['blocking']})")
        typer.echo(f"  {qq['question']}\n  why: {qq['why']}\n  default: {qq['default']}")


@app.command()
def decide(gate_id: str, decision: str, comment: str = "", spec: list[str] = typer.Option([], help="approve_subset spec ids"),
           by: str = "cli-user"):
    """Record a gate decision. The artifact hashes shown at the gate are bound to the decision automatically."""
    cfg = cfgmod.load()
    q = _queue(cfg)
    g = q._load("gates.json").get(gate_id)
    if g is None:
        typer.echo("unknown gate")
        raise typer.Exit(1)
    d = q._load("decisions.json")
    d[gate_id] = {"decision": decision, "decided_by": by, "comments": comment, "approved_subset": spec,
                  "artifact_shas": [a["sha256"] for a in g["artifacts"]]}
    q._save("decisions.json", d)
    typer.echo(f"recorded {decision} on {gate_id}")


@app.command()
def answer(question_id: str, text: str, by: str = "cli-user"):
    cfg = cfgmod.load()
    q = _queue(cfg)
    d = q._load("answers.json")
    d[question_id] = {"answer": text, "answered_by": by}
    q._save("answers.json", d)
    typer.echo("recorded")


@app.command()
def status(run_id: str):
    cfg = cfgmod.load()
    store = RunStore(cfg.runs_dir)
    st = store.state(run_id)
    typer.echo(f"{run_id}: {st.stage.value}  cost=${st.list_cost_usd:.2f}  decomposition r{st.decomposition_revision}")
    for wi in st.work_items.values():
        typer.echo(f"  {wi.work_item_id:16s} {wi.stage.value:16s} spec={wi.spec_id} review_round={wi.review_round} cr={wi.cr_id}")
    for ev in store.events(run_id)[-15:]:
        typer.echo(f"  {ev.at[11:19]} {ev.kind:20s} {ev.stage or '':12s} {json.dumps(ev.payload)[:100]}")


@app.command()
def replay(run_id: str):
    cfg = cfgmod.load()
    st = RunStore(cfg.runs_dir).state(run_id)
    typer.echo(json.dumps({"stage": st.stage.value, "work_items": {k: v.stage.value for k, v in st.work_items.items()},
                           "cost": st.list_cost_usd, "events": st.last_seq + 1}, indent=2))


if __name__ == "__main__":
    app()
