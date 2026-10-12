"""One verification pipeline (ADR-0014): `verify(target, plan) -> EvidenceBundle` and `judge(bundle, spec) -> checks`.

Every candidate head is verified through these two functions, whatever runs it:

- **The plan** is the spec's test plan as the controller runs it (`plan_of`). A structured command
  (`{"runner": "pytest" | "bun", "select": [...]}`) and a journey entry (`{"journey": "<name>", ...}`) are rendered here,
  so the controller, not the spec writer, owns how a runner is invoked and which reporters it gets. A plain string is
  the shell form older specs and non-test commands (a build, a linter) use.
- **The target** says where the candidate runs: `hosted` (preview mode managed: a fresh Anthropic-hosted sandbox built
  from the app's approved recipe, `hosted_verifier`), `local` (the controller's own clone and per-run environment, with
  the service started from its manifest, `local_verifier`; for development), or `platform` (the same clone for the
  commands, and the platform's per-branch preview deployment for the preview entries).
- **The bundle** is the evidence in one shape on every target: the setup and environment checks, a row per command and
  per e2e entry in the plan's order, the test ids collected and reported, and the preview record. Verification decides
  nothing.
- **`judge`** turns a bundle into the deterministic checks the merge judge receives and may not override. A row passed
  only when it ran, exited 0 and any browser evidence it owes passed (`checks.row_passed`), which is also what Stage 5
  reads from its rows against dev.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import checks
from .checks import CheckResult
from .plans import Plan, normalized, plan_of, render_command, render_e2e
from .hosted_verifier import HostedVerifier, journey_name, preview_record
from .local_verifier import SETUP_CHECK, LocalPreview, LocalVerifier

if TYPE_CHECKING:
    from .stages import RunContext

__all__ = ["EvidenceBundle", "Plan", "Target", "evidence_from_reports", "journey_name", "judge", "normalized", "plan_of",
           "preview_info", "render_command", "render_e2e", "target_for", "verify", "wait_for_preview"]


@dataclass(frozen=True)
class Target:
    """Where a candidate head is verified (module docstring)."""
    kind: str                       # hosted | local | platform
    repo: Path
    base_sha: str
    head_sha: str
    branch: str


TARGETS = {"managed": "hosted", "local": "local"}       # any other preview mode verifies against the platform's preview


def target_for(ctx: Any, repo: Path, base_sha: str, head_sha: str, branch: str) -> Target:
    return Target(TARGETS.get(ctx.cfg.preview_mode, "platform"), repo, base_sha, head_sha, branch)


@dataclass
class EvidenceBundle:
    """What one verification of a head observed, in the shape `judge` and the reviewers read on every target."""
    target: str
    head_sha: str
    setup: CheckResult                          # the environment the commands ran in could be built
    commands: list[dict[str, Any]]              # one row per plan command, in order
    e2e: list[dict[str, Any]]                   # one row per plan e2e entry, in order
    preview: dict[str, Any]                     # the preview record agents and the change request see
    environment_record: dict[str, Any]          # where it ran and how (test_results.json `environment`)
    environment: CheckResult | None = None      # hosted: the recipe's own lifecycle and baseline check
    collected: list[str] | None = None          # pytest node ids collected at the head; None: unknown
    passing_tests: list[str] | None = None      # test ids the run's own reports saw pass (`evidence_from_reports`)
    tests_known: list[str] | None = None
    require_test_evidence: bool = False         # hosted: no report is a failure, never "unverified"

    def test_results(self) -> dict[str, Any]:
        """test_results.json: the rows, the preview and the environment, as reviewers and the merge judge read them."""
        return {"commands": self.commands, "e2e": self.e2e, "preview": self.preview, "environment": self.environment_record,
                **({"collected_tests": self.collected} if self.target == "hosted" else {})}


def _reported(row: dict[str, Any]) -> dict[str, Any] | None:
    """A command row's test outcomes: its framework-neutral `tests` evidence when it is a report (pytest or Bun, local or
    hosted)."""
    tests = row.get("tests")
    return tests if isinstance(tests, dict) and tests.get("status") == "reported" else None


def passed_test_ids(command_results: list[dict[str, Any]]) -> tuple[list[str], list[str]] | None:
    """(passed, failed) test ids across every command that produced a test report, or None when none did."""
    runs = [t for t in map(_reported, command_results) if t]
    if not runs:
        return None
    passed = sorted({t for r in runs for t in r.get("passed", [])})
    failed = sorted({t for r in runs for t in r.get("failed", [])})
    return [t for t in passed if t not in set(failed)], failed


def evidence_from_reports(command_results: list[dict[str, Any]], collected: list[str] | None) -> tuple[list[str] | None, list[str] | None]:
    """(passing ids, known ids) from a run's own test reports, on any host. Known is pytest's collection plus every id
    any report listed (Bun has no verified collect-only mode, so its inventory is the report of the run itself, skipped
    and filtered tests included). (None, None) when the run produced neither: the mapping is then unverified, which
    fails the check wherever test evidence is required (`EvidenceBundle.require_test_evidence`)."""
    runs = passed_test_ids(command_results)
    if runs is None and collected is None:
        return None, None
    listed = {t for r in map(_reported, command_results) if r for k in ("passed", "failed", "skipped") for t in r.get(k, [])}
    return (runs[0] if runs else []), sorted(set(collected or []) | listed)


def wait_for_preview(ctx: RunContext, branch: str, head_sha: str, *, poll_seconds: float = 20.0,
                     sleep=None, clock=None) -> dict[str, Any]:
    """Give the platform's pipeline time to deploy `head_sha` before running e2e against the preview.

    Bounded by `policy.preview_wait_seconds`; a preview still `pending` or `unavailable` at the deadline
    stays that way and `e2e_preview` fails closed (not run is not passed). The forge is polled; nothing
    pushes deployment state to the controller.
    """
    import time as _t
    sleep = sleep or _t.sleep
    clock = clock or _t.monotonic
    deadline = clock() + ctx.policy.preview_wait_seconds
    info = preview_info(ctx, branch, head_sha)
    while info["status"] in ("pending", "unavailable") and ctx.forge is not None and clock() < deadline:
        sleep(poll_seconds)
        info = preview_info(ctx, branch, head_sha)
    return info


def preview_info(ctx: RunContext, branch: str, head_sha: str | None = None) -> dict[str, Any]:
    """What the platform knows about this branch's preview deploy, read from the forge, never from an agent.

    Shape handed to agents as `preview.json`: {branch, status, url, pipeline}. `status` is one of
    `ready` (pipeline succeeded and an environment URL exists), `pending` (pipeline not finished),
    `failed`, `unavailable` (no forge, or no pipeline yet), or `unreachable_from_sandbox` (local mode: the
    orchestrator starts the service on its own host, which no sandbox can reach, so `url` is null and the
    agent's own manifest-entrypoint instance is the stand-in). UNVERIFIED against a live forge.
    """
    info: dict[str, Any] = {"branch": branch, "status": "unavailable", "url": None, "pipeline": None,
                            "head_sha": head_sha}
    if ctx.cfg.preview_mode == "managed":
        return preview_record(branch, head_sha)
    if ctx.cfg.preview_mode == "local":
        info.update(status="unreachable_from_sandbox", mode="local",
                    note=("no platform preview exists in local mode: the orchestrator starts the service from the manifest "
                          "entrypoint on its own host and runs the preview entries there; that instance cannot be reached from "
                          "a sandbox. Start the service from runtime.entrypoint yourself and run the entries against it."))
        return info
    if ctx.forge is None:
        return info
    try:
        pipe = ctx.forge.latest_pipeline(branch, head_sha)
        info["pipeline"] = pipe
        url = ctx.forge.environment_url_for_branch(branch, ctx.cfg.preview_environment_prefix)
        info["url"] = url
        if pipe is None:
            info["status"] = "unavailable"
        elif pipe["status"] == "success" and url:
            info["status"] = "ready"
        elif pipe["status"] in ("failed", "canceled"):
            info["status"] = "failed"
        else:
            info["status"] = "pending"
    except Exception as e:  # network or shape surprises must not take the stage down; the check still fails closed
        info["error"] = str(e)
    return info


def manifest_or_none(path: Path) -> dict[str, Any] | None:
    """The service manifest as a dict, or None when it is missing or not JSON (`manifest_valid` reports that)."""
    try:
        m = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return m if isinstance(m, dict) else None


def _local_instance(ctx: Any, target: Target, verifier: LocalVerifier, entries: list[dict[str, Any]],
                    manifest: dict[str, Any] | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Local mode: start the checked-out service from its manifest, in the per-run environment, and run every e2e entry
    against it (both targets, as in the hosted sandbox).

    The returned preview record is the one agents and the change request see: status `unreachable_from_sandbox`,
    url null (nobody but the orchestrator can reach this instance), and `orchestrator_instance` saying whether the
    service started and where the orchestrator's own e2e rows ran."""
    preview = preview_info(ctx, target.branch, target.head_sha)
    instance: dict[str, Any] = {"status": "failed", "url": None}
    preview["orchestrator_instance"] = instance

    def run(url: str | None) -> list[dict[str, Any]]:
        return verifier.run_entries({"preview": url, "local": url}, entries, repo=target.repo)
    if manifest is None:
        instance["error"] = f"no readable service manifest at {ctx.cfg.service_manifest_path}; cannot start the service"
        return preview, run(None)
    if verifier.setup is not None and not verifier.setup.passed:
        instance["error"] = f"not started: {SETUP_CHECK} failed ({verifier.setup.detail[:300]})"
        return preview, run(None)
    try:
        with LocalPreview(target.repo, manifest, env_bin=verifier.env_bin) as url:
            instance.update(status="ready", url=url)
            return preview, run(url)
    except Exception as exc:  # noqa: BLE001
        instance["error"] = str(exc)[:500]
        return preview, run(None)


def verify(ctx: Any, target: Target, plan: Plan, *, spec: dict[str, Any], verifier: LocalVerifier,
           journeys: dict[str, bytes] | None = None, round_no: int = 0, work_item_id: str | None = None) -> EvidenceBundle:
    """Run `plan` against the candidate at `target` and return what was observed. `verifier` is the controller's clone
    (the hosted target only reads git through it); `journeys` are the implementer's journey files for `factory:journey`
    entries."""
    spec = {**spec, "test_plan": {**(spec.get("test_plan") or {}), "commands": plan.commands, "e2e": plan.e2e}}
    if target.kind == "hosted":
        run = HostedVerifier(ctx, verifier.workdir).verify(target.repo, target.base_sha, target.head_sha, spec, branch=target.branch,
                                                           round_no=round_no, work_item_id=work_item_id, journeys=journeys)
        passing, known = evidence_from_reports(run.commands, run.collected)
        return EvidenceBundle("hosted", target.head_sha, run.setup, run.commands, run.e2e, run.preview,
                              {**run.record, "detail": run.setup.detail}, environment=run.environment, collected=run.collected,
                              passing_tests=passing, tests_known=known, require_test_evidence=True)
    repo = target.repo
    manifest = manifest_or_none(repo / ctx.cfg.service_manifest_path)
    setup = verifier.prepare_environment(repo, manifest)       # the per-run environment, rebuilt from this head's pyproject
    commands = verifier.run_commands(repo, plan.commands)
    if target.kind == "local":
        preview, e2e = _local_instance(ctx, target, verifier, plan.e2e, manifest)
    else:
        # The preview entries run against the preview the platform deployed for this head. The URL comes from the forge's
        # deployment record, not from the implementer's report; "not run" counts as failed.
        preview = wait_for_preview(ctx, target.branch, target.head_sha)
        e2e = verifier.run_entries({"preview": preview["url"] if preview["status"] == "ready" else None}, plan.e2e, repo=repo)
    # The passing set is the orchestrator's own: ids its run's reports saw pass, checked against what pytest collects in
    # the checkout. A repository nothing reports on leaves the mapping unverified (said so in the check's detail).
    collected = verifier.collect_test_ids(repo)
    passing, known = evidence_from_reports(commands, collected)
    environment = {"mode": ctx.cfg.preview_mode, "host": "orchestrator" if target.kind == "local" else "platform",
                   **setup.data, "ready": setup.passed, "detail": setup.detail}
    return EvidenceBundle(target.kind, target.head_sha, setup, commands, e2e, preview, environment, collected=collected,
                          passing_tests=passing, tests_known=known)


def judge(bundle: EvidenceBundle, spec: dict[str, Any], *, diff: dict[str, Any], report: dict[str, Any],
          manifest_path: Path | None = None, schema_dir: Path | None = None, size: dict[str, Any] | None = None,
          size_budget_lines: dict[str, int] | None = None, reported_head: str | None = None) -> list[CheckResult]:
    """The deterministic checks for one verified head: the bundle's setup and environment, then every merge check
    (`checks.run_all_for_merge`). `reported_head` is the head an implementation report recorded before this verification
    (a replayed round, or an older controller's): the branch may have moved since, and the check says so."""
    spec = normalized(spec)
    det = [bundle.setup] + ([bundle.environment] if bundle.environment is not None else []) + checks.run_all_for_merge(
        spec=spec, changed_files=diff["changed"], deleted_files=diff["deleted"], existing_test_files=diff["existing_tests"],
        added_lines_by_file=diff["added_lines"], implementation_report=report, passing_tests=bundle.passing_tests,
        tests_that_exist=bundle.tests_known, command_results=bundle.commands, manifest_path=manifest_path,
        schema_dir=schema_dir, e2e_results=bundle.e2e, require_test_evidence=bundle.require_test_evidence,
        size=size, size_budget_lines=size_budget_lines,
        # only exact test paths in the approved spec may be updated; every other existing test stays protected
        named_test_files=checks.named_test_paths(spec["allowed_paths"]), removed_lines_by_file=diff.get("removed_lines"),
    )
    if reported_head is not None:
        det.append(CheckResult("implementation_head", reported_head == bundle.head_sha,
                               f"implementation report names {reported_head}; verified candidate is {bundle.head_sha}. "
                               "A changed candidate requires an updated implementation report and review response."))
    return det
