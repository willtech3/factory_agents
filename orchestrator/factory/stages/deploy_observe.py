"""Stage 5: observed deploy to dev. The factory watches; it does not deploy.

After Gate 3 (`mr.merged`), the platform's own GitLab CI deploys the default branch to `dev`
through its existing templates and WIF identity. The orchestrator:

1. finds the pipeline for the merge commit (webhook first, polling as the source of truth),
2. records `deploy.started` / `deploy.succeeded` / `deploy.failed`,
3. on success reads the dev URL from the pipeline's environment and runs the spec's
   `test_plan.e2e` entries (target `preview`, which means "a deployed instance") plus an
   optional smoke suite against it, recording `e2e.dev_passed` / `e2e.dev_failed`,
4. on any failure opens a `DEPLOY_FAILURE` gate carrying the pipeline log excerpt. The human
   can send the work item back to `IMPLEMENTING` (a follow-up branch), re-observe (platform
   flake), or close it.

Why observe rather than drive: boundary-crossing actions belong to humans or to the platform's
own automation, not to a factory agent (12-Factor Agents Factor 7; OpenAI auto-review, both in
docs/research/practitioners.md). Why observe at all: agents and pipelines alike declare victory
at merge; SWE-Cycle's full-cycle drop and Anthropic's "declare victory too early" are the
evidence that a merged MR is not a working change (docs/05-platform-and-deployment.md).
Promotion beyond dev is human and outside this module.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..gates import Decision, GateKind
from ..gitlab import PIPELINE_TERMINAL, parse_pipeline_webhook
from ..state import WorkItemStage
from . import RunContext
from .common import open_gate_and_wait


class PipelineSource(Protocol):
    """The read-only slice of `GitLabClient` this stage needs; tests supply a fake."""
    def latest_pipeline(self, ref: str, sha: str | None = None) -> dict[str, Any] | None: ...
    def pipeline(self, pipeline_id: int) -> dict[str, Any]: ...
    def failed_job_log_excerpt(self, pipeline_id: int, max_chars: int = 6000) -> str: ...
    def environment(self, name: str) -> dict[str, Any] | None: ...


E2ERunner = Callable[[str, list[dict[str, Any]]], list[dict[str, Any]]]
"""(base_url, e2e_entries) -> results with exit_code per entry. Production: LocalVerifier.run_e2e bound to a checkout."""


@dataclass
class DeployObservation:
    status: str                                  # succeeded | failed | timed_out | no_pipeline
    pipeline: dict[str, Any] | None = None
    environment_url: str | None = None
    log_excerpt: str = ""
    e2e_results: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "pipeline": self.pipeline, "environment_url": self.environment_url,
                "log_excerpt": self.log_excerpt, "e2e_results": self.e2e_results}


def wait_for_pipeline(source: PipelineSource, ref: str, sha: str, *, poll_seconds: float = 15.0,
                      timeout_seconds: float = 3600.0, sleep: Callable[[float], None] = time.sleep,
                      clock: Callable[[], float] = time.monotonic, dev_environment: str = "dev") -> DeployObservation:
    """Poll until the pipeline for `sha` on `ref` reaches a terminal status. Webhooks may arrive earlier;
    polling is the source of truth because webhooks are not a durable log."""
    deadline = clock() + timeout_seconds
    pipe: dict[str, Any] | None = None
    while clock() < deadline:
        pipe = source.pipeline(pipe["id"]) if pipe else source.latest_pipeline(ref, sha)
        if pipe and pipe["status"] in PIPELINE_TERMINAL:
            break
        sleep(poll_seconds)
    if pipe is None:
        return DeployObservation("no_pipeline", log_excerpt=f"no pipeline found for {ref}@{sha[:10]} within {timeout_seconds}s")
    if pipe["status"] != "success":
        if pipe["status"] in PIPELINE_TERMINAL:
            return DeployObservation("failed", pipe, log_excerpt=source.failed_job_log_excerpt(pipe["id"]))
        return DeployObservation("timed_out", pipe, log_excerpt=f"pipeline {pipe['id']} still {pipe['status']} after {timeout_seconds}s")
    env = source.environment(dev_environment)
    url = (env or {}).get("external_url")
    if not url:
        return DeployObservation("failed", pipe, log_excerpt=f"pipeline succeeded but environment '{dev_environment}' has no external_url")
    return DeployObservation("succeeded", pipe, environment_url=url)


def handle_pipeline_webhook(ctx: RunContext, payload: dict[str, Any], sha_to_item: dict[str, tuple[str, str]],
                            default_branch: str) -> dict[str, Any] | None:
    """Webhook handler stub. The Factory Console's HTTP endpoint calls this with the raw GitLab Pipeline Hook
    body. It records `deploy.started` the first time a non-terminal pipeline is seen for a merged work item's
    sha; terminal outcomes are recorded by `observe_deploy`, which reconciles by polling. Returns the
    normalised pipeline, or None when the payload is not a pipeline hook or not for a tracked sha."""
    pipe = parse_pipeline_webhook(payload)
    if pipe is None or pipe.get("ref") != default_branch or pipe.get("sha") not in sha_to_item:
        return None
    wid, sid = sha_to_item[pipe["sha"]]
    if pipe["status"] not in PIPELINE_TERMINAL:
        already = any(ev.kind == "deploy.started" and ev.payload.get("pipeline_id") == pipe["id"]
                      for ev in ctx.store.events(ctx.run_id))
        if not already:
            ctx.event("deploy.started", {"pipeline_id": pipe["id"], "pipeline_url": pipe.get("web_url"), "sha": pipe["sha"],
                                         "environment": ctx.cfg.dev_environment_name, "via": "webhook"},
                      work_item_id=wid, spec_id=sid, stage="deploy", actor="platform")
    return pipe


def observe_deploy(ctx: RunContext, spec: dict[str, Any], merge_sha: str, default_branch: str,
                   source: PipelineSource, run_e2e: E2ERunner, *, smoke_commands: tuple[str, ...] = (),
                   assignees: list[str] | None = None, poll_seconds: float = 15.0, timeout_seconds: float = 3600.0,
                   max_reobserve: int = 1, sleep: Callable[[float], None] = time.sleep,
                   clock: Callable[[], float] = time.monotonic) -> WorkItemStage:
    """Drive one merged work item through Stage 5. Returns the work item's resulting stage."""
    sid, wid = spec["spec_id"], spec["work_item_id"]
    ctx.event("work_item.stage", {"stage": WorkItemStage.DEPLOYING_DEV.value}, work_item_id=wid, spec_id=sid, stage="deploy")
    for attempt in range(max_reobserve + 1):
        obs = _observe_once(ctx, spec, merge_sha, default_branch, source, run_e2e, smoke_commands,
                            poll_seconds, timeout_seconds, sleep, clock)
        if obs.status == "succeeded" and all(r.get("exit_code") == 0 for r in obs.e2e_results):
            ctx.event("work_item.stage", {"stage": WorkItemStage.DEPLOYED_DEV.value, "environment_url": obs.environment_url},
                      work_item_id=wid, spec_id=sid, stage="deploy")
            return WorkItemStage.DEPLOYED_DEV
        decision = _deploy_failure_gate(ctx, spec, obs, assignees or [])
        if decision.decision == Decision.APPROVE and attempt < max_reobserve:
            continue                                    # human says platform flake: watch the next pipeline
        ctx.event("work_item.stage", {"stage": WorkItemStage.ESCALATED.value, "reason": f"deploy:{obs.status}"},
                  work_item_id=wid, spec_id=sid, stage="deploy")
        if decision.decision == Decision.REQUEST_CHANGES:
            # The comments become the next implementer round's findings; a new branch and MR follow.
            ctx.store.put_artifact(ctx.run_id, "deploy", f"{sid}-deploy_feedback.md",
                                   _feedback_md(obs, decision.comments).encode())
            ctx.event("work_item.stage", {"stage": WorkItemStage.IMPLEMENTING.value, "from": "deploy_failure"},
                      work_item_id=wid, spec_id=sid, stage="deploy", actor="human")
            return WorkItemStage.IMPLEMENTING
        if decision.decision == Decision.REJECT:
            ctx.event("work_item.stage", {"stage": WorkItemStage.CLOSED.value}, work_item_id=wid, spec_id=sid,
                      stage="deploy", actor="human")
            return WorkItemStage.CLOSED
        return WorkItemStage.ESCALATED                 # APPROVE with re-observations exhausted: a human takes over
    return WorkItemStage.ESCALATED  # pragma: no cover


def _observe_once(ctx: RunContext, spec: dict[str, Any], merge_sha: str, default_branch: str, source: PipelineSource,
                  run_e2e: E2ERunner, smoke_commands: tuple[str, ...], poll_seconds: float, timeout_seconds: float,
                  sleep: Callable[[float], None], clock: Callable[[], float]) -> DeployObservation:
    sid, wid = spec["spec_id"], spec["work_item_id"]
    dev = ctx.cfg.dev_environment_name
    first = source.latest_pipeline(default_branch, merge_sha)
    ctx.event("deploy.started", {"pipeline_id": (first or {}).get("id"), "pipeline_url": (first or {}).get("web_url"),
                                 "sha": merge_sha, "environment": dev, "via": "poll"},
              work_item_id=wid, spec_id=sid, stage="deploy", actor="platform")
    obs = wait_for_pipeline(source, default_branch, merge_sha, poll_seconds=poll_seconds, timeout_seconds=timeout_seconds,
                            sleep=sleep, clock=clock, dev_environment=dev)
    pid = (obs.pipeline or {}).get("id")
    if obs.status != "succeeded":
        ctx.event("deploy.failed", {"pipeline_id": pid, "pipeline_url": (obs.pipeline or {}).get("web_url"), "sha": merge_sha,
                                    "environment": dev, "status": obs.status, "log_excerpt": obs.log_excerpt[-4000:]},
                  work_item_id=wid, spec_id=sid, stage="deploy", actor="platform")
        return obs
    ctx.event("deploy.succeeded", {"pipeline_id": pid, "pipeline_url": obs.pipeline.get("web_url"), "sha": merge_sha,
                                   "environment": dev, "environment_url": obs.environment_url},
              work_item_id=wid, spec_id=sid, stage="deploy", actor="platform")
    entries = [e for e in spec.get("test_plan", {}).get("e2e", []) if e.get("target", "preview") == "preview"]
    entries += [{"covers_ac": [], "command": c, "target": "preview", "description": "smoke suite"} for c in smoke_commands]
    obs.e2e_results = run_e2e(obs.environment_url, entries) if entries else []
    failed = [r for r in obs.e2e_results if r.get("exit_code") != 0]
    kind = "e2e.dev_failed" if failed else "e2e.dev_passed"
    ctx.event(kind, {"environment_url": obs.environment_url, "sha": merge_sha,
                     "results": [{"command": r["command"], "exit_code": r.get("exit_code"), "covers_ac": r.get("covers_ac", [])}
                                 for r in obs.e2e_results]},
              work_item_id=wid, spec_id=sid, stage="deploy")
    if failed:
        obs.log_excerpt = "\n".join(f"$ {r['command']}\nexit {r.get('exit_code')}\n{(r.get('stderr_tail') or r.get('stdout_tail') or '')[-1500:]}"
                                    for r in failed)
    return obs


def _deploy_failure_gate(ctx: RunContext, spec: dict[str, Any], obs: DeployObservation, assignees: list[str]):
    sid = spec["spec_id"]
    report = json.dumps(obs.to_dict(), indent=2).encode()
    sha = ctx.store.put_artifact(ctx.run_id, "deploy", f"{sid}-deploy_report.json", report)
    what = {"failed": "the dev pipeline failed", "timed_out": "the dev pipeline did not finish in time",
            "no_pipeline": "no dev pipeline was found for the merge commit",
            "succeeded": "the dev deploy succeeded but end-to-end checks failed against the dev URL"}[obs.status]
    summary = (f"{sid}: {what}. Options: request_changes sends the item back to an implementer on a new branch with "
               f"your comments; approve means the failure was the platform's and the next pipeline should be watched; "
               f"reject closes the item. Log excerpt:\n{obs.log_excerpt[-1500:]}")
    return open_gate_and_wait(ctx, GateKind.DEPLOY_FAILURE, {f"{sid}-deploy_report.json": sha}, summary, assignees,
                              options=(Decision.REQUEST_CHANGES, Decision.APPROVE, Decision.REJECT))


def _feedback_md(obs: DeployObservation, comments: str) -> str:
    return "\n".join(["# Deploy-to-dev failure", "", f"Status: `{obs.status}`",
                      f"Environment URL: {obs.environment_url or 'n/a'}", "", "## Human comments", "", comments.strip(),
                      "", "## Pipeline / e2e log excerpt", "", "```", obs.log_excerpt[-4000:], "```", "",
                      "Fix on a new branch from the default branch; the orchestrator opens a follow-up MR."])


def on_mr_merged(ctx: RunContext, spec: dict[str, Any], merge_sha: str, default_branch: str, verifier: Any,
                 assignees: list[str]) -> WorkItemStage:
    """Entry point the MR watcher calls when GitLab reports `mr.merged` for a factory MR.

    Binds the real GitLab client as the pipeline source and `LocalVerifier.run_e2e` (checked out at the
    merge commit) as the e2e runner, then observes. Requires a GitLab client; without one Stage 5 is skipped
    and the item stays `MERGED`, which the Console shows as "not verified in dev".
    """
    if ctx.gitlab is None:
        return WorkItemStage.MERGED
    repo = verifier.checkout(default_branch)

    def run_e2e(url: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return verifier.run_e2e(url, entries, repo=repo)

    return observe_deploy(ctx, spec, merge_sha, default_branch, ctx.gitlab, run_e2e,
                          smoke_commands=ctx.cfg.smoke_commands, assignees=assignees,
                          timeout_seconds=ctx.policy.dev_pipeline_timeout_seconds)
