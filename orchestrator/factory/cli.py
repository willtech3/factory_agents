"""`factory` command line.

    factory doctor                      # reconcile config: agents, environment, forge, gate surface
    factory run outcome.json            # start a run (blocks at gates; use a second terminal to decide)
    factory gates                       # list open gates and questions for all runs
    factory decide <gate_id> approve|request_changes|reject [--comment ...] [--spec spec_x ...]
    factory answer <question_id> "text"
    factory status <run_id>             # derived state + timeline
    factory replay <run_id>             # re-derive state from the event log (sanity check)
    factory resume <run_id>             # continue a run that died during implementation
    factory web [--port 8765] [--demo]  # the Factory Console: outcomes, runs board, inbox, gate review

Local mode uses a file-backed queue under the runs directory so two processes can talk. With
`FACTORY_GATE_SURFACE=tracker` the same gate is also opened on the tracker (Linear) and a decision
from either side wins (`CompositeQueue`).
"""
from __future__ import annotations

import json
from pathlib import Path

import typer

from . import config as cfgmod
from .gates import CompositeNotifier, CompositeQueue, LogNotifier
from .store import RunStore

app = typer.Typer(no_args_is_help=True, add_completion=False)


from .surfaces.filequeue import FileQueue  # noqa: E402  (the `file`/`web` gate surface)


def _bind_forge(cfg, outcome: dict, want_forge: bool):
    """The forge for this run, bound to the outcome's repository. FORGE_REPO may be left unset: it is derived from
    the outcome's repository url (and must match it when set). A repository marked `create: true` that does not
    exist yet is created before the run and seeded with the preview workflow template."""
    import dataclasses
    from .forge import ensure_repository, make_forge, repo_from_url

    repo = (outcome.get("target_repositories") or [{}])[0]
    url = repo.get("url", "")
    derived = repo_from_url(url) if url else ""
    if not want_forge:
        return cfg, None, None
    if cfg.forge_repo and derived and cfg.forge_repo.strip("/").lower() != derived.lower():
        typer.echo(f"cannot run: the outcome targets {url} but FORGE_REPO is {cfg.forge_repo}; they must be the same repository")
        raise typer.Exit(1)
    if not cfg.forge_repo and derived:
        cfg = dataclasses.replace(cfg, forge_repo=derived)
    forge_obj = make_forge(cfg)
    created = None
    if forge_obj is not None and url:
        # Creates the repository when the outcome asks for it; seeds the platform-contract skill into any repository
        # whose default branch lacks it, created or not.
        created = ensure_repository(forge_obj, repo, cfg.templates_dir / "github-actions-preview.yml",
                                    skill_file=cfg.skills_dir / "platform-contract" / "SKILL.md")
        if created:
            typer.echo(f"created repository {created['url']} (default branch {created.get('default_branch')})")
    return cfg, forge_obj, created


def _policy(outcome: dict, design_only: bool):
    """Loop limits and budgets; FACTORY_BUDGET_SCALE (e.g. 0.4) scales every per-stage cap for cheap first runs."""
    import os
    from .state import Policy
    scale = float(os.environ.get("FACTORY_BUDGET_SCALE", "1") or 1)
    base = Policy()
    knob = lambda name, default: int(os.environ.get(name) or default)  # noqa: E731
    return Policy(run_budget_usd=outcome.get("constraints", {}).get("budget_usd") or 150.0, design_only=design_only,
                  stage_budget_usd={k: round(v * scale, 2) for k, v in base.stage_budget_usd.items()},
                  reviewer_passes=knob("FACTORY_REVIEWER_PASSES", base.reviewer_passes),
                  security_review=os.environ.get("FACTORY_SECURITY_REVIEW", "1").lower() not in ("0", "false", "no", "off"),
                  review_max_rounds=knob("FACTORY_REVIEW_MAX_ROUNDS", base.review_max_rounds),
                  parallel_implementations=knob("FACTORY_PARALLEL_IMPLEMENTATIONS", base.parallel_implementations),
                  outcome_max_iterations=knob("FACTORY_OUTCOME_MAX_ITERATIONS", base.outcome_max_iterations))


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
    typer.echo(f"forge        {cfg.forge} {cfg.forge_url} {cfg.forge_repo or '(repository taken from the outcome)'} "
               f"token={'set' if cfg.forge_token else 'MISSING'}")
    typer.echo(f"gate surface {cfg.gate_surface}" + (f" (Linear team {cfg.linear_team_id})" if cfg.gate_surface == "tracker" else ""))
    typer.echo(f"pages        {cfg.page_publisher} -> {cfg.runs_dir}/<run_id>/pages/")
    if cfg.forge == "gitlab":
        typer.echo(f"vault        {cfg.vault_id or '(FACTORY_VAULT_ID missing)'}")
    design_gaps = cfgmod.missing(cfg, design_only=True)
    pc = cfgmod.platform_check(cfg)
    if pc["error"]:
        typer.echo(f"platform     {pc['error']}")
    else:
        typer.echo(f"platform     {sum(a['found'] for a in pc['agents'])}/{len(pc['agents'])} agents found on the platform"
                   f"{' (workspace ' + cfg.workspace_id + ')' if cfg.workspace_id else ''}")
    if gaps:
        typer.echo("missing for a full run: " + ", ".join(gaps))
        typer.echo("design-only run (`factory run --design-only`): " + (", ".join(design_gaps) if design_gaps else "ready"))
        raise typer.Exit(1)
    typer.echo("ok")


def _context(cfg, outcome: dict, run_id: str, *, forge: bool, design_only: bool):
    """The RunContext `run` and `resume` share: forge bound to the outcome's repository, the gate surfaces composed,
    and session starts recorded as events so the Console can link to them."""
    from .sessions import SessionDriver
    from .stages import RunContext
    from .surfaces.artifact_pages import make_publisher
    from .surfaces.tracker import make_tracker_queue

    cfg, forge_obj, created = _bind_forge(cfg, outcome, forge and not (design_only and cfg.gate_surface != "forge"))
    queue, notifier = _queue(cfg), LogNotifier()
    tracker = make_tracker_queue(cfg)
    if tracker is not None:
        queue, notifier = CompositeQueue(queue, tracker), CompositeNotifier(notifier, tracker)
    ctx = RunContext(
        cfg=cfg, store=RunStore(cfg.runs_dir), driver=SessionDriver(cfg), queue=queue, notifier=notifier,
        forge=forge_obj,
        policy=_policy(outcome, design_only),
        run_id=run_id, tracker=tracker, pages=make_publisher(cfg),
    )
    if created:
        ctx.event("repository.created", {"url": created["url"], "default_branch": created.get("default_branch"),
                                         "private": created.get("private"), "forge": cfg.forge,
                                         "workflow_seeded": created.get("workflow_seeded")}, stage="intake", actor="platform")
    ctx.driver.on_session_started = lambda info: ctx.event(
        "session.started", {k: v for k, v in info.items() if k not in ("run_id", "work_item_id")},
        stage=info["stage"], actor="platform", work_item_id=info.get("work_item_id"))
    return ctx


def _drive(ctx, pipeline):
    """Run `pipeline(ctx)` with the stop and crash handling every run gets: a signal or an exception interrupts the
    sessions still in flight and records run.failed, so a dead orchestrator never looks like a run in progress."""
    import signal

    def _stop(signum, frame):
        stopped = ctx.driver.interrupt_active()
        try:
            ctx.event("run.failed", {"reason": f"stopped by signal {signum}", "interrupted_sessions": stopped})
        except Exception:  # noqa: BLE001
            pass
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _stop)
    try:
        return pipeline(ctx)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        stopped = ctx.driver.interrupt_active()
        try:
            ctx.event("run.failed", {"reason": f"{type(e).__name__}: {str(e)[:500]}", "interrupted_sessions": stopped})
        except Exception:  # noqa: BLE001
            pass
        raise


@app.command()
def run(outcome_path: Path, forge: bool = typer.Option(True, help="Talk to the forge (change requests, pipelines)"),
        design_only: bool = typer.Option(False, "--design-only", help="Real agents through Gate 2 (decompose, specs, judge); "
                                                                      "no implementation, no forge needed")):
    """Start a run from an outcome.json. Blocks at human gates; decide from another terminal, the Console, or the tracker."""
    from .runner import new_run_id, run as run_pipeline

    cfg = cfgmod.load()
    gaps = cfgmod.missing(cfg, design_only=design_only)
    if gaps:
        typer.echo("cannot run; missing: " + ", ".join(gaps))
        raise typer.Exit(1)
    outcome = json.loads(outcome_path.read_text())
    run_id = new_run_id()
    ctx = _context(cfg, outcome, run_id, forge=forge, design_only=design_only)
    typer.echo(f"run {run_id} started (forge={cfg.forge if ctx.forge else 'none'}, gate surface={cfg.gate_surface}"
               f"{', design only' if design_only else ''})")
    final = _drive(ctx, lambda c: run_pipeline(c, outcome))
    typer.echo(f"run {run_id} finished in state {final.value}")


@app.command()
def resume(run_id: str, forge: bool = typer.Option(True, help="Talk to the forge (change requests, pipelines)")):
    """Continue a run that failed or was stopped during implementation. Nothing before Gate 2 is re-run: the approved
    specs come from the store, and each one picks up at the round whose work was persisted (a finished implementer
    session is not repeated; the orchestrator's own verification of its head is)."""
    from .runner import resume as resume_pipeline

    cfg = cfgmod.load()
    gaps = cfgmod.missing(cfg)
    if gaps:
        typer.echo("cannot resume; missing: " + ", ".join(gaps))
        raise typer.Exit(1)
    store = RunStore(cfg.runs_dir)
    try:
        outcome = json.loads(store.get_artifact(run_id, "intake", "outcome.json"))
    except FileNotFoundError:
        typer.echo(f"no run {run_id} under {cfg.runs_dir}")
        raise typer.Exit(1)
    ctx = _context(cfg, outcome, run_id, forge=forge, design_only=False)
    typer.echo(f"run {run_id} resuming from {store.state(run_id).stage.value} (forge={cfg.forge if ctx.forge else 'none'})")
    final = _drive(ctx, resume_pipeline)
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
    q.record_decision(gate_id, decision, by, comment, spec)
    typer.echo(f"recorded {decision} on {gate_id}")


@app.command()
def answer(question_id: str, text: str, by: str = "cli-user"):
    cfg = cfgmod.load()
    q = _queue(cfg)
    try:
        q.record_answer(question_id, text, by)
    except KeyError:
        typer.echo("unknown question")
        raise typer.Exit(1)
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


@app.command()
def web(host: str = "127.0.0.1", port: int = 8765,
        demo: bool = typer.Option(False, help="Seed a simulated run from examples/ledgerline so the console can be tried without the platform")):
    """Serve the Factory Console (needs `pip install -e '.[web]'`)."""
    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        typer.echo("uvicorn is not installed: pip install -e '.[web]'")
        raise typer.Exit(1)
    from .web.app import create_app

    cfg = cfgmod.load()
    application = create_app(cfg)
    if demo:
        run_id = application.state.demo.seed_example()
        typer.echo(f"demo run {run_id} seeded")
    typer.echo(f"Factory Console at http://{host}:{port}/  (runs under {cfg.runs_dir})")
    uvicorn.run(application, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    app()
