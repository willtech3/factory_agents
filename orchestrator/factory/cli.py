"""`factory` command line.

    factory doctor                      # reconcile config: agents, environment, forge, gate surface
    factory run outcome.json            # start a run (blocks at gates; use a second terminal to decide)
    factory gates                       # list open gates and questions for all runs
    factory decide <gate_id> approve|request_changes|reject [--comment ...] [--spec spec_x ...]
    factory answer <question_id> "text"
    factory status <run_id>             # derived state + timeline
    factory replay <run_id>             # re-derive state from the event log (sanity check)
    factory resume <run_id>             # continue a run that died during implementation or paused at its budget
    factory resume <run_id> --budget 80 # ... raising the run budget (or --outcome file.json to read it from there)
    factory watch <run_id> [--once]     # Gate 3 and Stage 5: follow the run's pull requests to the merge and the dev deploy
    factory runs [--active]             # the run history: every run, its status, and what came out of it
    factory web [--port 8765]           # the Factory Console: the run history, the board, outcomes, gate review
    factory run outcome.json --preview managed --app-id app_x --test-profile-sha256 <sha> --test-vault vlt_a   # hosted verification
    factory browser list|claim|submit|block|doctor|smoke   # attach a Claude in Chrome session to browser checks

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


from .surfaces.filequeue import FileQueue  # noqa: E402  (the `web` gate surface)
from .browser_worker import cli as browser_cli  # noqa: E402

app.add_typer(browser_cli, name="browser")

environment_cli = typer.Typer(no_args_is_help=True, help="Validate application recipes and test them in Anthropic-hosted sandboxes.")
app.add_typer(environment_cli, name="environment")


@environment_cli.command("check")
def environment_check(profile: Path):
    """Check a recipe's structure. This does not provision or mark an app ready."""
    from .test_environments import validate_profile
    errors = validate_profile(json.loads(profile.read_text()), cfgmod.REPO_ROOT / "schemas")
    typer.echo(json.dumps({"status": "invalid" if errors else "valid-plan", "errors": errors}, indent=2))
    if errors:
        raise typer.Exit(1)


@environment_cli.command("verify")
def environment_verify(repo: Path, output: Path, budget: float = typer.Option(..., "--budget"),
                       profile: str = "test_environment.json", revision: str = "HEAD",
                       vault: list[str] = typer.Option([], "--vault")):
    """Run committed app + database + browser in the cloud. Never on this machine."""
    from .test_environments import run_hosted
    cfg = cfgmod.load()
    result = run_hosted(cfg, repo, output, profile_relative=profile, revision=revision,
                        budget_usd=budget, vault_ids=tuple(vault))
    typer.echo(json.dumps(result, indent=2))
    if result["status"] != "passed":
        raise typer.Exit(1)


@environment_cli.command("prune")
def environment_prune(yes: bool = typer.Option(False, "--yes", help="Archive the leftovers listed; without it, only list them.")):
    """List each onboarded app's execution environment in the workspace and archive what earlier attempts left behind."""
    from .app_environments import AppEnvironments
    from .onboarding import AppOnboarding
    cfg = cfgmod.load()
    environments = AppEnvironments(cfg, AppOnboarding(cfg))
    view = environments.snapshot(refresh=True)
    if view["error"]:
        typer.echo(view["error"], err=True)
        raise typer.Exit(1)
    for a in view["apps"]:
        typer.echo(f"app      {a['name']:<32} {a['environment']['name']}  {a['repository_url'] or '(repository not recorded)'}")
    if view["discovery"]:
        typer.echo(f"discovery {view['discovery']['name']}")
    for e in view["retire"]:
        typer.echo(f"leftover {e['name']}  ({e['reason']}; keeps {e['replaced_by_name']})")
    if not view["retire"]:
        typer.echo("Nothing to archive: every app has one execution environment.")
    elif not yes:
        typer.echo(f"{len(view['retire'])} leftover environment(s). Re-run with --yes to archive them.")
    else:
        out = environments.cleanup([e["id"] for e in view["retire"]])
        typer.echo(f"Archived {len(out['archived'])}; {len(out['failed'])} failed.")
        if out["failed"]:
            raise typer.Exit(1)


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
        # Only repositories explicitly marked for creation are seeded. Existing repositories may require
        # every change through a PR; every session already receives the platform skill as an upload.
        created = ensure_repository(forge_obj, repo, cfg.templates_dir / "github-actions-preview.yml",
                                    skill_file=(cfg.skills_dir / "platform-contract" / "SKILL.md") if repo.get("create") else None)
        if created:
            typer.echo(f"created repository {created['url']} (default branch {created.get('default_branch')})")
    return cfg, forge_obj, created


def _policy(outcome: dict, design_only: bool, budget_usd: float | None = None):
    """Loop limits and budgets. The run budget is the outcome's `constraints.budget_usd` (default $150) and is
    enforced before every session (`budget.py`); `budget_usd` replaces it on a resume whose log carries a raised cap.
    FACTORY_BUDGET_SCALE (e.g. 0.4) scales every per-stage cap for cheap first runs, never below the stage's observed
    median session (`Policy.stage_budget_floor_usd`). FACTORY_SIZE_BUDGET (e.g. "XS=50,S=150,M=400") sets the
    hand-written line budget per size (`size.py`)."""
    import os
    from .size import parse_budget
    from .stages.common import parse_auto_approve
    from .state import Policy
    scale = float(os.environ.get("FACTORY_BUDGET_SCALE", "1") or 1)
    base = Policy()
    knob = lambda name, default: int(os.environ.get(name) or default)  # noqa: E731
    run_budget = budget_usd if budget_usd is not None else (os.environ.get("FACTORY_RUN_BUDGET_USD") or outcome.get("constraints", {}).get("budget_usd") or 150.0)
    return Policy(run_budget_usd=float(run_budget), design_only=design_only,
                  stage_budget_usd={k: max(round(v * scale, 2), base.stage_budget_floor_usd.get(k, 0.0))
                                    for k, v in base.stage_budget_usd.items()},
                  reviewer_passes=knob("FACTORY_REVIEWER_PASSES", base.reviewer_passes),
                  auto_approve=parse_auto_approve(os.environ.get("FACTORY_AUTO_APPROVE")),
                  review_single_pass_lines=knob("FACTORY_REVIEW_SINGLE_PASS_LINES", base.review_single_pass_lines),
                  reviewer_passes_followup=knob("FACTORY_REVIEWER_PASSES_FOLLOWUP", base.reviewer_passes_followup),
                  security_review=os.environ.get("FACTORY_SECURITY_REVIEW", "1").lower() not in ("0", "false", "no", "off"),
                  review_max_rounds=knob("FACTORY_REVIEW_MAX_ROUNDS", base.review_max_rounds),
                  parallel_implementations=knob("FACTORY_PARALLEL_IMPLEMENTATIONS", base.parallel_implementations),
                  outcome_max_iterations=knob("FACTORY_OUTCOME_MAX_ITERATIONS", base.outcome_max_iterations),
                  size_budget_lines=parse_budget(os.environ.get("FACTORY_SIZE_BUDGET")))


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


def _context(cfg, outcome: dict, run_id: str, *, forge: bool, design_only: bool, budget_usd: float | None = None,
             driver: bool = True):
    """The RunContext `run`, `resume`, and `watch` share: forge bound to the outcome's repository, the gate surfaces
    composed, and session starts recorded as events so the Console can link to them. `driver=False` (the watcher)
    skips the platform client: no agent runs, so no API key is needed."""
    from .sessions import SessionDriver
    from .stages import RunContext
    from .surfaces.artifact_pages import make_publisher
    from .surfaces.tracker import make_tracker_queue

    cfg, forge_obj, created = _bind_forge(cfg, outcome, forge and not design_only)
    queue, notifier = _queue(cfg), LogNotifier()
    tracker = make_tracker_queue(cfg)
    if tracker is not None:
        queue, notifier = CompositeQueue(queue, tracker), CompositeNotifier(notifier, tracker)
    ctx = RunContext(
        cfg=cfg, store=RunStore(cfg.runs_dir), driver=SessionDriver(cfg) if driver else None, queue=queue, notifier=notifier,
        forge=forge_obj,
        policy=_policy(outcome, design_only, budget_usd),
        run_id=run_id, tracker=tracker, pages=make_publisher(cfg),
    )
    if created:
        ctx.event("repository.created", {"url": created["url"], "default_branch": created.get("default_branch"),
                                         "private": created.get("private"), "forge": cfg.forge,
                                         "workflow_seeded": created.get("workflow_seeded")}, stage="intake", actor="platform")
    if ctx.driver is not None:
        ctx.driver.on_session_started = lambda info: ctx.event(
            "session.started", {k: v for k, v in info.items() if k not in ("run_id", "work_item_id")},
            stage=info["stage"], actor="platform", work_item_id=info.get("work_item_id"))
        # A session interrupted by a stop or a crash still finished, and its spend still counts.
        ctx.driver.on_session_finished = lambda info: ctx.event(
            "session.finished", {k: v for k, v in info.items() if k not in ("run_id", "work_item_id", "stage")},
            stage=info.get("stage"), actor="platform", work_item_id=info.get("work_item_id"))
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


def apply_preview_choice(cfg, preview: str | None = None, app_id: str | None = None, test_profile_sha256: str | None = None,
                         test_vaults: list[str] | None = None, verify_budget: float | None = None):
    """Command-line preview choice over the environment's (FACTORY_PREVIEW, FACTORY_APP_ID, FACTORY_TEST_PROFILE_SHA256,
    FACTORY_TEST_VAULT_IDS, FACTORY_MANAGED_VERIFY_BUDGET_USD). Validation is `config.missing`'s, as for the environment."""
    import dataclasses
    changes = {k: v for k, v in {
        "preview_mode": preview.lower() if preview else None, "app_id": app_id,
        "test_profile_sha256": test_profile_sha256.strip().lower() if test_profile_sha256 else None,
        "test_vault_ids": tuple(v.strip() for v in test_vaults if v.strip()) if test_vaults else None,
        "managed_verify_budget_usd": verify_budget}.items() if v is not None}
    return dataclasses.replace(cfg, **changes) if changes else cfg


@app.command()
def run(outcome_path: Path, forge: bool = typer.Option(True, help="Talk to the forge (change requests, pipelines)"),
        design_only: bool = typer.Option(False, "--design-only", help="Real agents through Gate 2 (decompose, specs, judge); "
                                                                      "no implementation, no forge needed"),
        preview: str | None = typer.Option(None, "--preview", help="platform | local | managed (verify every candidate in a hosted "
                                                                  "sandbox from the app's approved test recipe); default FACTORY_PREVIEW"),
        app_id: str | None = typer.Option(None, "--app-id", help="managed: the onboarded app this run changes"),
        test_profile_sha256: str | None = typer.Option(None, "--test-profile-sha256", help="managed: fingerprint of the approved test recipe"),
        test_vault: list[str] = typer.Option([], "--test-vault", help="managed: an explicit test-only vault id (repeatable); never the forge's"),
        verify_budget: float | None = typer.Option(None, "--verify-budget", help="managed: USD cap per hosted verification session (default 4)")):
    """Start a run from an outcome.json. Blocks at human gates; decide from another terminal, the Console, or the tracker."""
    from .runner import RunLocked, controller_lock, new_run_id, run as run_pipeline

    cfg = apply_preview_choice(cfgmod.load(), preview, app_id, test_profile_sha256, test_vault, verify_budget)
    outcome = json.loads(outcome_path.read_text())
    if not design_only and cfg.preview_mode != "managed":
        cfg = _bind_onboarded_app(cfg, outcome, explicit_preview=preview)
    gaps = cfgmod.missing(cfg, design_only=design_only)
    if gaps:
        typer.echo("cannot run; missing: " + ", ".join(gaps))
        raise typer.Exit(1)
    run_id = new_run_id()
    try:
        # Held for the whole drive, like `resume`: the run history (`factory runs`, the Console) probes this lock to tell a
        # controller that is still driving from one that died without recording why.
        with controller_lock(cfg.runs_dir, run_id):
            ctx = _context(cfg, outcome, run_id, forge=forge, design_only=design_only)
            typer.echo(f"run {run_id} started (forge={cfg.forge if ctx.forge else 'none'}, gate surface={cfg.gate_surface}"
                       f"{', design only' if design_only else ''}, budget ${ctx.policy.run_budget_usd:.2f})")
            final = _drive(ctx, lambda c: run_pipeline(c, outcome))
    except RunLocked as e:
        typer.echo(str(e))
        raise typer.Exit(1)
    _report_final(ctx, run_id, final)


def _bind_onboarded_app(cfg, outcome: dict, explicit_preview: str | None):
    """A live run on an onboarded app's repository verifies in that app's hosted sandbox, whichever path started it (the
    Console binds server-side the same way). Several apps for the repository, a setup that is not verified, or an explicit
    local/platform preview for it is a refusal; a repository no app was onboarded for runs as configured."""
    if not any((cfg.runs_dir / "_apps").glob("app_*/app.json")):
        return cfg
    from .errors import Refusal
    from .onboarding import AppOnboarding

    try:
        binding = AppOnboarding(cfg).binding_for((outcome.get("target_repositories") or [{}])[0])
    except Refusal as e:
        typer.echo(f"cannot run: {e.detail}")
        raise typer.Exit(1)
    if binding is None:
        return cfg
    if explicit_preview:
        typer.echo(f"cannot run: this repository is the onboarded app {binding['FACTORY_APP_ID']}; its change runs verify in the "
                   f"hosted sandbox, so --preview {explicit_preview} is not available for it")
        raise typer.Exit(1)
    typer.echo(f"bound to onboarded app {binding['FACTORY_APP_ID']}: hosted verification with its approved recipe")
    return apply_preview_choice(cfg, "managed", binding["FACTORY_APP_ID"], binding["FACTORY_TEST_PROFILE_SHA256"],
                                [v for v in binding["FACTORY_TEST_VAULT_IDS"].split(",") if v],
                                float(binding["FACTORY_MANAGED_VERIFY_BUDGET_USD"]))


def _report_final(ctx, run_id: str, final) -> None:
    from .state import RunStage
    typer.echo(f"run {run_id} finished in state {final.value}")
    if final == RunStage.PAUSED_BUDGET:
        typer.echo(f"the next session would take the run past its budget; nothing is running. Decide the budget gate "
                   f"(Console, `factory decide <gate_id> approve --comment 'budget_usd: N'`, or reject), then `factory resume {run_id}`.")
    _watch_hint(ctx, run_id)


@app.command()
def resume(run_id: str, forge: bool = typer.Option(True, help="Talk to the forge (change requests, pipelines)"),
           budget: float | None = typer.Option(None, "--budget", help="New run budget in USD. For a run paused at its budget "
                                                                   "this is the decision (no gate decision needed); for a "
                                                                   "failed run it replaces the cap for the rest of the run."),
           outcome_file: Path | None = typer.Option(None, "--outcome", help="Read the new budget from this outcome file's "
                                                                         "constraints.budget_usd instead of --budget"),
           preview: str | None = typer.Option(None, "--preview", help="platform | local | managed; default FACTORY_PREVIEW"),
           app_id: str | None = typer.Option(None, "--app-id", help="managed: the onboarded app this run changes"),
           test_profile_sha256: str | None = typer.Option(None, "--test-profile-sha256", help="managed: fingerprint of the approved test recipe"),
           test_vault: list[str] = typer.Option([], "--test-vault", help="managed: an explicit test-only vault id (repeatable); never the forge's"),
           verify_budget: float | None = typer.Option(None, "--verify-budget", help="managed: USD cap per hosted verification session")):
    """Continue a run that failed or was stopped during implementation, that paused at its budget, or whose controller
    was stopped while it waited at Gate 2. Nothing before Gate 2 is re-run for a failed run: the approved specs come
    from the store, and each one picks up at the round whose work was persisted (a finished implementer session is not
    repeated; the orchestrator's own verification of its head is). A budget pause applies the owner's decision on the
    budget gate (approve with `budget_usd: N`, or reject to close) or --budget / --outcome, records the raised cap, and
    re-enters the stage it interrupted. A run stopped at Gate 2 waits on that same gate, its artefacts verified against
    the hashes it presented, and continues with the owner's decision; --preview managed then needs the approved recipe
    fingerprint. One controller per run: a second `factory resume` is refused while the first holds the run's lock."""
    from .runner import RunLocked, controller_lock

    cfg = apply_preview_choice(cfgmod.load(), preview, app_id, test_profile_sha256, test_vault, verify_budget)
    store = RunStore(cfg.runs_dir)
    try:
        outcome = json.loads(store.get_artifact(run_id, "intake", "outcome.json"))
    except FileNotFoundError:
        typer.echo(f"no run {run_id} under {cfg.runs_dir}")
        raise typer.Exit(1)
    events = store.events(run_id)
    st = store.state(run_id)
    design_only = any(ev.kind == "run.created" and ev.payload.get("design_only") for ev in events)
    gaps = cfgmod.missing(cfg, design_only=design_only)
    if gaps:
        typer.echo("cannot resume; missing: " + ", ".join(gaps))
        raise typer.Exit(1)
    new_budget = budget
    if new_budget is None and outcome_file is not None:
        new_budget = (json.loads(outcome_file.read_text()).get("constraints") or {}).get("budget_usd")
        if new_budget is None:
            typer.echo(f"{outcome_file} has no constraints.budget_usd")
            raise typer.Exit(1)
    current = st.budget_usd
    ctx = _context(cfg, outcome, run_id, forge=forge, design_only=design_only, budget_usd=current)
    try:
        with controller_lock(cfg.runs_dir, run_id):
            _resume_locked(ctx, run_id, st, events, current, new_budget)
    except RunLocked as e:
        typer.echo(str(e))
        raise typer.Exit(1)


def _resume_locked(ctx, run_id: str, st, events, current, new_budget) -> None:
    """`factory resume` once the run's controller lock is held: every refusal before the drive, so none writes run.failed."""
    from .runner import budget_decision, preflight, resume as resume_pipeline
    from .state import RunStage

    cfg = ctx.cfg
    try:
        preflight(ctx, events, budget_usd=new_budget)
    except RuntimeError as e:
        typer.echo(f"cannot resume run {run_id}: {e}")
        raise typer.Exit(1)
    if st.stage == RunStage.PAUSED_BUDGET:
        try:                      # refuse here, before any event is written, when nothing has decided the pause yet
            action, raised_to, _ = budget_decision(ctx, st, events, new_budget)
        except RuntimeError as e:
            typer.echo(str(e))
            raise typer.Exit(1)
        typer.echo(f"run {run_id} paused at its budget (${st.list_cost_usd:.2f} spent of ${current or 0:.2f}): "
                   + ("closing it as the owner decided" if action == "close" else f"raising the budget to ${raised_to:.2f} and continuing"))
    else:
        typer.echo(f"run {run_id} resuming from {st.stage.value} (forge={cfg.forge if ctx.forge else 'none'}, preview={cfg.preview_mode}"
                   + (f", budget raised to ${new_budget:.2f}" if new_budget is not None else "") + ")")
    final = _drive(ctx, lambda c: resume_pipeline(c, budget_usd=new_budget))
    _report_final(ctx, run_id, final)


def _watch_hint(ctx, run_id: str) -> None:
    from .watcher import pending_work_items
    try:
        open_items = pending_work_items(ctx.store.state(run_id))
    except Exception:  # noqa: BLE001
        return
    if open_items:
        typer.echo(f"{len(open_items)} change request(s) await review on the forge: `factory watch {run_id}` (or Watch in the "
                   f"Console) records the merge and follows the dev deploy")


@app.command()
def watch(run_id: str, once: bool = typer.Option(False, "--once", help="Poll once, act on what changed, and exit"),
          poll_seconds: int = typer.Option(60, help="Seconds between polls of the forge"),
          hours: float = typer.Option(72.0, help="Give up after this long with change requests still open")):
    """Gate 3 and Stage 5 for a run whose pull requests are open: poll the forge for each change request; on merge
    record it, watch the default-branch pipeline and the dev deployment, run the spec's e2e entries against the dev
    URL, and end the item at verified-in-dev or a deploy-failure gate. Safe to stop and start: nothing is recorded twice."""
    from .watcher import Watcher

    cfg = cfgmod.load()
    gaps = cfgmod.missing_for_watch(cfg)
    if gaps:
        typer.echo("cannot watch; missing: " + ", ".join(gaps))
        raise typer.Exit(1)
    store = RunStore(cfg.runs_dir)
    try:
        outcome = json.loads(store.get_artifact(run_id, "intake", "outcome.json"))
    except FileNotFoundError:
        typer.echo(f"no run {run_id} under {cfg.runs_dir}")
        raise typer.Exit(1)
    ctx = _context(cfg, outcome, run_id, forge=True, design_only=False, driver=False)
    if ctx.forge is None:
        typer.echo("cannot watch: no forge is configured for this run (FORGE_TOKEN and the outcome's repository url)")
        raise typer.Exit(1)
    w = Watcher(ctx, outcome)
    pending = w.pending()
    if not pending:
        typer.echo(f"run {run_id} watching: nothing to watch (no work item at cr_open, merged, or deploying_dev)")
        return
    typer.echo(f"run {run_id} watching {len(pending)} work item(s) on {ctx.forge.kind} {ctx.forge.repo}: "
               + ", ".join(f"{wi.work_item_id} ({wi.stage.value}{', #' + str(wi.cr_id) if wi.cr_id else ''})" for wi in pending)
               + ("" if once else f"; polling every {poll_seconds}s for up to {hours:g}h"))

    def say(report):
        typer.echo(f"{__import__('datetime').datetime.now().strftime('%H:%M:%S')} {report.summary()}")

    if once:
        say(w.tick())
    else:
        w.watch(poll_seconds=poll_seconds, timeout_seconds=hours * 3600, on_tick=say)
    st = store.state(run_id)
    typer.echo("work items: " + ", ".join(f"{wi.work_item_id}={wi.stage.value}" for wi in st.work_items.values()))


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
    from .gates import parse_budget, run_cap_gate

    cfg = cfgmod.load()
    q = _queue(cfg)
    g = q._load("gates.json").get(gate_id)
    if g is None:
        typer.echo("unknown gate")
        raise typer.Exit(1)
    if decision not in g["options"]:
        typer.echo(f"{decision} is not an option for this gate; options: {', '.join(g['options'])}")
        raise typer.Exit(1)
    if run_cap_gate(g["kind"], g.get("artifacts")) and decision == "approve" and parse_budget(comment) is None:
        typer.echo("approving a budget gate needs the new run budget in the comment, e.g. --comment 'budget_usd: 80'")
        raise typer.Exit(1)
    if decision == "approve_subset" and g["kind"] in ("gate_2_spec_readiness", "escalation"):
        from .approvals import presented_specs, subset_problems
        try:
            problems = subset_problems(presented_specs(cfg.runs_dir, g["run_id"], g["artifacts"]), spec)
        except ValueError as e:
            problems = [f"cannot check the selection: {e}"]
        if problems:
            typer.echo("cannot approve this selection: " + "; ".join(problems) + ". Select the prerequisites too, or request changes.")
            raise typer.Exit(1)
    q.record_decision(gate_id, decision, by, comment, spec)
    typer.echo(f"recorded {decision} on {gate_id}")
    if g["kind"] == "budget":
        typer.echo(f"the run is parked: `factory resume {g['run_id']}` applies this decision")


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
def runs(active: bool = typer.Option(False, "--active", help="Only runs that are not finished (waiting on someone, running, paused, stopped)")):
    """The run history: every run under the runs directory, its status, and what came out of it (the same record the
    Console's Runs page shows)."""
    from .summary import summarize_run

    cfg = cfgmod.load()
    for line in run_history_lines(RunStore(cfg.runs_dir), cfg.runs_dir, active=active, summarize=summarize_run):
        typer.echo(line)


def run_history_lines(store, runs_dir: Path, *, active: bool = False, summarize=None) -> list[str]:
    """`factory runs` as lines: unfinished runs first, then by last activity; two lines per run (the facts, then the
    headline). Kept apart from the command so it is testable without the environment's configuration."""
    from .summary import summarize_run

    summarize = summarize or summarize_run
    rows = []
    for run_id in store.list_runs():
        try:
            rows.append(summarize(store, runs_dir, run_id))
        except Exception as e:  # noqa: BLE001  a corrupt log should not hide the other runs
            rows.append({"run_id": run_id, "title": "", "status_label": "Unreadable", "terminal": True, "cost_usd": 0.0,
                         "budget_usd": None, "updated_at": "", "headline": f"The event log could not be read: {type(e).__name__}: {e}"})
    if active:
        rows = [r for r in rows if not r["terminal"]]
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    rows.sort(key=lambda r: bool(r["terminal"]))
    if not rows:
        return ["no runs" + (" in progress; `factory runs` lists finished ones too" if active else f" under {runs_dir}")]
    out = [f"{'run':16s} {'status':16s} {'spend':18s} {'last activity':17s} outcome"]
    for r in rows:
        spend = f"${r['cost_usd']:.2f}" + (f" of ${r['budget_usd']:.2f}" if r.get("budget_usd") is not None else "")
        out.append(f"{r['run_id']:16s} {r['status_label']:16s} {spend:18s} {(r.get('updated_at') or '')[:16]:17s} {r['title']}")
        out.append(f"{'':16s} {r['headline']}")
    return out


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
def timing(run_id: str, as_json: bool = typer.Option(False, "--json", help="The full report as JSON")):
    """Where the run's wall clock went: waiting on a person, agents (by stage), the controller, stopped."""
    from . import timing as timing_mod

    cfg = cfgmod.load()
    rep = timing_mod.report(RunStore(cfg.runs_dir).events(run_id))
    if as_json:
        typer.echo(json.dumps(rep, indent=2))
        return
    for line in timing_mod.render(rep):
        typer.echo(line)


@app.command()
def web(host: str = "127.0.0.1", port: int = 8765):
    """Serve the Factory Console (needs `pip install -e '.[web]'`). It opens on the run history."""
    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        typer.echo("uvicorn is not installed: pip install -e '.[web]'")
        raise typer.Exit(1)
    from .web.app import create_app

    cfg = cfgmod.load()
    application = create_app(cfg)
    typer.echo(f"Factory Console at http://{host}:{port}/  (runs under {cfg.runs_dir})")
    uvicorn.run(application, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    app()
