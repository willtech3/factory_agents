"""Gate 3 and Stage 5 watcher: follow a run's change requests to the merge and the dev deploy.

After the integrator opens a change request the run is complete from the orchestrator's point of view and the
work item sits at `CR_OPEN`. Review and merge happen on the forge (Gate 3), by people. Nothing in the run loop
sees that happen, so this module does: `factory watch <run_id>` (and the Console's **Watch** button, which
starts the same command) polls each open change request through the configured forge and

1. records `cr.merged` (or `cr.closed`) and moves the item to `MERGED` (or `CLOSED`) when the forge reports it;
2. hands a merged item to Stage 5 (`stages/deploy_observe.py`): `DEPLOYING_DEV`, the default-branch pipeline for
   the merge commit, the dev deployment's URL, the spec's `test_plan.e2e` entries against it, ending at
   `DEPLOYED_DEV` or a `DEPLOY_FAILURE` gate;
3. keeps polling the gate for the human's decision and applies it (back to implementing, re-observe once, close).

Polling, not webhooks: the forge is the source of truth and a poll is idempotent. Every tick re-derives the run
state from the event log and only appends what the log does not yet hold, so the watcher can be stopped and
started at will: a merge seen twice is recorded once, an item already deploying is not moved again, a gate left
open by a dead watcher is picked up rather than re-opened, and a decision recorded but not yet applied is applied.
What a restart does repeat is an observation that was interrupted mid-pipeline: a new `deploy.started` with the
next `attempt` number, because the pipeline's outcome was never recorded.

Bounded: `watch()` stops when no item is left at `CR_OPEN`, `MERGED`, or `DEPLOYING_DEV`, or at its deadline. One
tick observes one pipeline at a time, for at most `Policy.dev_pipeline_timeout_seconds`.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .gates import Decision, GateKind
from .stages import RunContext
from .stages.common import gate_decision
from .stages.deploy_observe import (
    DeployObservation,
    E2ERunner,
    PipelineSource,
    apply_deploy_decision,
    deploy_attempts,
    deploy_report_name,
    ensure_deploying,
    observe_once,
    open_deploy_failure_gate,
    record_deployed,
)
from .state import RunEvent, RunState, WorkItemStage, WorkItemState

WATCHED_STAGES = (WorkItemStage.CR_OPEN, WorkItemStage.MERGED, WorkItemStage.DEPLOYING_DEV)
HEARTBEAT = "watcher.json"       # runs/<run_id>/watcher.json: pid, last poll, what is pending; the Console reads it


def pending_work_items(state: RunState) -> list[WorkItemState]:
    """Items the watcher has something to do for: a change request to poll, a merge to observe, or a deploy in flight."""
    return [wi for wi in state.work_items.values()
            if wi.stage in WATCHED_STAGES and not (wi.stage == WorkItemStage.CR_OPEN and wi.cr_id is None)]


@dataclass
class TickReport:
    """What one poll did. Lists hold work item ids."""
    polled: list[dict[str, Any]] = field(default_factory=list)    # {work_item_id, cr_id, state}
    merged: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    deployed: list[str] = field(default_factory=list)
    gates_opened: list[str] = field(default_factory=list)
    decisions_applied: list[str] = field(default_factory=list)   # a DEPLOY_FAILURE decision acted on this tick
    waiting_on_gate: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)   # {work_item_id, error}; the item is retried next tick
    pending: list[str] = field(default_factory=list)             # still watched after this tick
    events_appended: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def summary(self) -> str:
        parts = []
        if self.merged:
            parts.append("merged " + ", ".join(self.merged))
        if self.closed:
            parts.append("closed " + ", ".join(self.closed))
        if self.deployed:
            parts.append("verified in dev " + ", ".join(self.deployed))
        if self.gates_opened:
            parts.append("deploy-failure gate opened for " + ", ".join(self.gates_opened))
        if self.decisions_applied:
            parts.append("gate decision applied for " + ", ".join(self.decisions_applied))
        if self.waiting_on_gate:
            parts.append("waiting on a gate for " + ", ".join(self.waiting_on_gate))
        if self.errors:
            parts.append("errors: " + "; ".join(f"{e['work_item_id']}: {e['error']}" for e in self.errors))
        still = [p["work_item_id"] for p in self.polled if p["state"] not in ("merged", "closed")]
        if still:
            parts.append("still open " + ", ".join(still))
        return "; ".join(parts) or "nothing to do"


@dataclass
class Watcher:
    ctx: RunContext
    outcome: dict[str, Any]
    source: PipelineSource | None = None          # pipeline and deployment reads; defaults to ctx.forge
    run_e2e: E2ERunner | None = None              # (dev_url, entries) -> results; defaults to LocalVerifier on a fresh checkout
    specs: dict[str, dict[str, Any]] = field(default_factory=dict)   # spec_id -> approved spec; loaded from the store on demand
    max_reobserve: int = 1
    pipeline_poll_seconds: float = 15.0
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    heartbeat: bool = True
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    # ---- what is there to watch
    def pending(self) -> list[WorkItemState]:
        return pending_work_items(self.ctx.store.state(self.ctx.run_id))

    @property
    def forge(self):
        if self.ctx.forge is None:
            raise RuntimeError("the watcher needs a forge: set FORGE_TOKEN (and FORGE_REPO or an outcome repository url)")
        return self.ctx.forge

    @property
    def default_branch(self) -> str:
        return (self.outcome.get("target_repositories") or [{}])[0].get("default_branch", "main")

    @property
    def assignees(self) -> list[str]:
        handle = (self.outcome.get("owner") or {}).get("handle")
        return [handle] if handle else []

    def spec(self, spec_id: str) -> dict[str, Any]:
        if spec_id not in self.specs:
            self.specs[spec_id] = self.ctx.store.get_json(self.ctx.run_id, "specify", f"final-{spec_id}.json")
        return self.specs[spec_id]

    def _e2e(self) -> E2ERunner:
        if self.run_e2e is None:
            self.run_e2e = local_e2e_runner(self.ctx, self.outcome, self.default_branch)
        return self.run_e2e

    # ---- one poll
    def tick(self) -> TickReport:
        report = TickReport()
        before = self.ctx.store.state(self.ctx.run_id).last_seq
        for wid in [wi.work_item_id for wi in self.pending()]:
            try:
                self._tick_item(wid, report)
            except Exception as e:  # noqa: BLE001  (a forge hiccup on one item must not stop the others, or the loop)
                report.errors.append({"work_item_id": wid, "error": f"{type(e).__name__}: {str(e)[:300]}"})
        state = self.ctx.store.state(self.ctx.run_id)
        report.pending = [wi.work_item_id for wi in pending_work_items(state)]
        report.events_appended = state.last_seq - before
        if self.heartbeat:
            self._write_heartbeat(report)
        return report

    def _tick_item(self, wid: str, report: TickReport) -> None:
        wi = self.ctx.store.state(self.ctx.run_id).work_items[wid]
        if wi.stage == WorkItemStage.CR_OPEN:
            cr = self.forge.change_request_state(wi.cr_id)
            report.polled.append({"work_item_id": wid, "cr_id": wi.cr_id, "state": cr.get("state")})
            if cr.get("state") == "merged":
                self._record_merged(wi, cr)
                report.merged.append(wid)
                wi = self.ctx.store.state(self.ctx.run_id).work_items[wid]
            elif cr.get("state") == "closed":
                self._record_closed(wi, cr)
                report.closed.append(wid)
                return
            else:
                return
        if wi.stage == WorkItemStage.MERGED:
            self._observe(wi, attempt=0, report=report)
        elif wi.stage == WorkItemStage.DEPLOYING_DEV:
            self._continue_deploying(wi, report)

    # ---- Gate 3 outcomes
    def _cr_url(self, wi: WorkItemState) -> str | None:
        for ev in self.ctx.store.events(self.ctx.run_id):
            if ev.kind == "cr.opened" and ev.work_item_id == wi.work_item_id and ev.payload.get("id") == wi.cr_id:
                return ev.payload.get("url")
        return None

    def _record_merged(self, wi: WorkItemState, cr: dict[str, Any]) -> None:
        sha = cr.get("merge_commit_sha") or cr.get("sha")
        try:
            approved: bool | None = bool(self.forge.change_request_approved(wi.cr_id))
        except Exception:  # noqa: BLE001  (the review record is context, not a condition)
            approved = None
        self.ctx.event("cr.merged", {"id": wi.cr_id, "url": self._cr_url(wi), "merge_commit_sha": sha, "head_sha": cr.get("sha"),
                                     "merged_at": cr.get("merged_at"), "approved": approved, "forge": self.forge.kind},
                       work_item_id=wi.work_item_id, spec_id=wi.spec_id, stage="gate_3", actor="human")
        self.ctx.event("work_item.stage", {"stage": WorkItemStage.MERGED.value, "merge_sha": sha, "cr_id": wi.cr_id},
                       work_item_id=wi.work_item_id, spec_id=wi.spec_id, stage="gate_3")

    def _record_closed(self, wi: WorkItemState, cr: dict[str, Any]) -> None:
        self.ctx.event("cr.closed", {"id": wi.cr_id, "url": self._cr_url(wi), "forge": self.forge.kind},
                       work_item_id=wi.work_item_id, spec_id=wi.spec_id, stage="gate_3", actor="human")
        self.ctx.event("work_item.stage", {"stage": WorkItemStage.CLOSED.value, "reason": "change request closed without merging"},
                       work_item_id=wi.work_item_id, spec_id=wi.spec_id, stage="gate_3", actor="human")

    # ---- Stage 5
    def _merge_sha(self, wi: WorkItemState) -> str:
        if wi.merge_sha:
            return wi.merge_sha
        raise RuntimeError(f"{wi.work_item_id} is {wi.stage.value} but no merge commit is recorded for it")

    def _observe(self, wi: WorkItemState, attempt: int, report: TickReport) -> None:
        spec = self.spec(wi.spec_id)
        ensure_deploying(self.ctx, spec)
        obs = observe_once(self.ctx, spec, self._merge_sha(wi), self.default_branch, self.source or self.forge, self._e2e(),
                           smoke_commands=self.ctx.cfg.smoke_commands, poll_seconds=self.pipeline_poll_seconds,
                           timeout_seconds=self.ctx.policy.dev_pipeline_timeout_seconds, sleep=self.sleep, clock=self.clock,
                           attempt=attempt)
        if obs.passed:
            record_deployed(self.ctx, spec, obs)
            report.deployed.append(wi.work_item_id)
        else:
            open_deploy_failure_gate(self.ctx, spec, obs, self.assignees)
            report.gates_opened.append(wi.work_item_id)

    def _continue_deploying(self, wi: WorkItemState, report: TickReport) -> None:
        """An item left at DEPLOYING_DEV: a gate may be open (poll it), decided but not applied (apply it), or the
        observation was interrupted (observe again)."""
        events = self.ctx.store.events(self.ctx.run_id)
        gate = _latest_deploy_gate(events, wi.work_item_id)
        attempts = deploy_attempts(events, wi.work_item_id)
        if gate is None:
            self._observe(wi, attempt=attempts, report=report)
            return
        gid = gate.payload["gate_id"]
        decided = next((ev for ev in events if ev.kind == "gate.decided" and ev.payload.get("gate_id") == gid), None)
        if decided is not None:
            applied = any(ev.seq > decided.seq and ev.work_item_id == wi.work_item_id
                          and ev.kind in ("work_item.stage", "deploy.started") for ev in events)
            if applied:                                   # an approved re-observation that was interrupted
                self._observe(wi, attempt=attempts, report=report)
                return
            decision, comments = Decision(decided.payload["decision"]), decided.payload.get("comments", "")
        else:
            d = gate_decision(self.ctx, gid, GateKind.DEPLOY_FAILURE, wi.work_item_id)
            if d is None:
                report.waiting_on_gate.append(wi.work_item_id)
                return
            decision, comments = d.decision, d.comments
        spec = self.spec(wi.spec_id)
        obs = DeployObservation.from_dict(self.ctx.store.get_json(self.ctx.run_id, "deploy", deploy_report_name(spec["spec_id"])))
        result = apply_deploy_decision(self.ctx, spec, obs, decision, comments, reobserve_allowed=attempts <= self.max_reobserve)
        report.decisions_applied.append(wi.work_item_id)
        if result is None:
            self._observe(wi, attempt=attempts, report=report)

    # ---- the loop
    def watch(self, *, poll_seconds: float = 60.0, timeout_seconds: float = 72 * 3600,
              on_tick: Callable[[TickReport], None] | None = None) -> TickReport:
        """Poll until nothing is pending or the deadline passes. Returns the last tick's report."""
        deadline = self.clock() + timeout_seconds
        report = self.tick()
        if on_tick:
            on_tick(report)
        while report.pending and self.clock() < deadline:
            self.sleep(poll_seconds)
            report = self.tick()
            if on_tick:
                on_tick(report)
        if self.heartbeat:
            self._write_heartbeat(report, stopped=True)
        return report

    # ---- heartbeat
    def _heartbeat_path(self) -> Path:
        return self.ctx.cfg.runs_dir / self.ctx.run_id / HEARTBEAT

    def _write_heartbeat(self, report: TickReport, stopped: bool = False) -> None:
        now = datetime.now(UTC).isoformat()
        data = {"pid": os.getpid(), "started_at": self.started_at, "last_poll_at": now, "pending": report.pending,
                "last_tick": report.to_dict(), "stopped_at": now if stopped else None}
        p = self._heartbeat_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, p)


def _latest_deploy_gate(events: list[RunEvent], work_item_id: str) -> RunEvent | None:
    """The newest DEPLOY_FAILURE gate opened for the item since its latest merge, or None."""
    gate = None
    for ev in events:
        if ev.work_item_id != work_item_id:
            continue
        if ev.kind == "cr.merged":
            gate = None
        elif ev.kind == "gate.opened" and ev.payload.get("kind") == GateKind.DEPLOY_FAILURE.value:
            gate = ev
    return gate


def local_e2e_runner(ctx: RunContext, outcome: dict[str, Any], default_branch: str) -> E2ERunner:
    """`LocalVerifier.run_e2e` bound to a checkout of the default branch, made on first use (a tick that finds no
    merge never clones anything)."""
    from .stages.implement import LocalVerifier

    repo_url = (outcome.get("target_repositories") or [{}])[0].get("url") or ctx.cfg.repo_url
    verifier = LocalVerifier(ctx.forge.authenticated_url(repo_url) if ctx.forge is not None else repo_url,
                             ctx.cfg.runs_dir / ctx.run_id / "verify")
    checkout: dict[str, Path] = {}

    def run_e2e(url: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if "repo" not in checkout:
            checkout["repo"] = verifier.checkout(default_branch)
        return verifier.run_e2e(url, entries, repo=checkout["repo"])

    return run_e2e


def heartbeat(runs_dir: Path, run_id: str) -> dict[str, Any] | None:
    """The watcher's last heartbeat for a run with `alive` (the pid still exists and the loop has not stopped), or None."""
    p = runs_dir / run_id / HEARTBEAT
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text())
    except json.JSONDecodeError:
        return None
    d["alive"] = not d.get("stopped_at") and _pid_alive(int(d.get("pid") or 0))
    return d


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


__all__ = ["HEARTBEAT", "WATCHED_STAGES", "TickReport", "Watcher", "heartbeat", "local_e2e_runner", "pending_work_items"]
