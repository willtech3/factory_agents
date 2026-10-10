"""Stage 2: decomposition -> specs -> spec judge loop -> Gate 2.

Loop shape (see docs/01-pipeline.md):
  for round in 1..spec_judge_max_rounds:
      write/revise every spec that is not yet passing   (parallel sessions, one per work item)
      judge the whole set in one independent session
      if verdict.pass -> Gate 2
      if verdict.escalate -> escalation gate
  after max rounds -> escalation gate with the last verdict
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .. import checks
from ..gates import Decision, GateKind
from ..state import RunStage, WorkItemStage
from . import RunContext
from .common import make_ask_human, open_gate_and_wait, persist_outputs, provenance, require_json

WRITER_DESCRIPTION = """Write the specification for work item {work_item_id} ({title}).
Inputs are under /mnt/session/uploads/ (outcome.json, decomposition.json, work_item.json, sibling_specs/,
and on a revision judge_issues.json and human_comments.md). The repository is cloned at /workspace/repo.
Write spec.json and spec.md to /mnt/session/outputs/ and validate spec.json with the validate.py tool."""

JUDGE_DESCRIPTION = """Judge the full set of specifications under /mnt/session/uploads/specs/ against rubric.md,
then judge the set's coherence and goal coverage against outcome.json and decomposition.json.
This is round {round}. Write verdict.json to /mnt/session/outputs/ and validate it."""


def _write_one(ctx: RunContext, outcome, decomposition, work_item, sibling_specs: dict[str, dict],
               judge_issues: list[dict] | None, human_comments: str | None, round_no: int) -> dict[str, Any]:
    wid = work_item["work_item_id"]
    ctx.event("work_item.stage", {"stage": WorkItemStage.SPEC_DRAFTING.value, "spec_round": round_no},
              work_item_id=wid, stage="specify")
    uploads = {
        "outcome.json": json.dumps(outcome, indent=2).encode(),
        "decomposition.json": json.dumps(decomposition, indent=2).encode(),
        "work_item.json": json.dumps(work_item, indent=2).encode(),
    }
    for sid, s in sibling_specs.items():
        uploads[f"sibling_specs/{sid}.json"] = json.dumps(s, indent=2).encode()
    if judge_issues is not None:
        uploads["judge_issues.json"] = json.dumps(judge_issues, indent=2).encode()
    if human_comments:
        uploads["human_comments.md"] = human_comments.encode()
    res = ctx.driver.run(
        agent_key="spec_writer", title=f"{ctx.run_id} spec {wid} r{round_no}", run_id=ctx.run_id, stage="specify",
        uploads=uploads, description=WRITER_DESCRIPTION.format(work_item_id=wid, title=work_item["title"]),
        rubric_md=ctx.rubric("spec"), max_iterations=ctx.policy.outcome_max_iterations,
        budget_usd=ctx.policy.stage_budget_usd["specify"], work_item_id=wid,
        custom_tools={"ask_human": make_ask_human(ctx, "spec_writer")},
    )
    spec = require_json(res, "spec.json")
    spec["revision"] = round_no
    spec.setdefault("produced_by", provenance(ctx, res, "Spec writer", "claude-sonnet-5-5", {}))
    res.outputs["spec.json"] = json.dumps(spec, indent=2).encode()
    persist_outputs(ctx, "specify", res, prefix=f"{wid}-r{round_no}-")
    return spec


def _judge(ctx: RunContext, outcome, decomposition, specs: dict[str, dict], round_no: int,
           previous_verdict: dict | None) -> dict[str, Any]:
    uploads = {
        "outcome.json": json.dumps(outcome, indent=2).encode(),
        "decomposition.json": json.dumps(decomposition, indent=2).encode(),
        "rubric.md": ctx.rubric("spec").encode(),
    }
    for sid, s in specs.items():
        # Normalise: the judge sees only the JSON contract, not the writer's prose rendering.
        uploads[f"specs/{sid}.json"] = json.dumps(s, indent=2, sort_keys=True).encode()
    if previous_verdict:
        uploads["previous_verdict.json"] = json.dumps(previous_verdict, indent=2).encode()
    res = ctx.driver.run(
        agent_key="spec_judge", title=f"{ctx.run_id} spec-judge r{round_no}", run_id=ctx.run_id, stage="judge",
        uploads=uploads, description=JUDGE_DESCRIPTION.format(round=round_no), rubric_md=None, max_iterations=1,
        budget_usd=ctx.policy.stage_budget_usd["judge"],
    )
    verdict = require_json(res, "verdict.json")
    verdict["round"] = round_no
    verdict.setdefault("produced_by", provenance(ctx, res, "Spec judge", "claude-opus-5-5", {}))
    res.outputs["verdict.json"] = json.dumps(verdict, indent=2).encode()
    persist_outputs(ctx, "judge", res, prefix=f"r{round_no}-")
    ctx.event("judge.verdict", {"round": round_no, "result": verdict["overall"]["result"],
                                "blocking": verdict["overall"]["blocking_issue_count"]}, stage="judge", actor="agent")
    return verdict


def run_specify(ctx: RunContext, outcome, decomposition, human_comments_by_item: dict[str, str] | None = None
                ) -> tuple[dict[str, dict], dict[str, Any]]:
    ctx.event("stage.entered", {"stage": RunStage.SPECIFYING.value}, stage="specify")
    items = {w["work_item_id"]: w for w in decomposition["work_items"]}
    specs: dict[str, dict] = {}
    issues_by_item: dict[str, list[dict]] = {}
    verdict: dict[str, Any] | None = None
    to_write = set(items)

    for round_no in range(1, ctx.policy.spec_judge_max_rounds + 1):
        # Write in dependency order waves so siblings can reference produced interfaces; parallel within a wave.
        waves = _topological_waves(items)
        for wave in waves:
            batch = [w for w in wave if w in to_write]
            if not batch:
                continue
            with ThreadPoolExecutor(max_workers=min(4, len(batch))) as pool:
                futures = {pool.submit(_write_one, ctx, outcome, decomposition, items[w],
                                       {s: specs[s] for s in specs if s != w},
                                       issues_by_item.get(w), (human_comments_by_item or {}).get(w), round_no): w
                           for w in batch}
                for fut, w in futures.items():
                    spec = fut.result()
                    specs[spec["spec_id"]] = spec
                    ctx.event("work_item.stage", {"stage": WorkItemStage.SPEC_JUDGING.value, "spec_id": spec["spec_id"]},
                              work_item_id=w, stage="specify")
        # Deterministic pre-checks: schema and traceability, cheap and before the judge spends tokens.
        det = {sid: [checks.schema_valid(s, ctx.cfg.schemas_dir / "spec.schema.json", ctx.cfg.schemas_dir),
                     checks.spec_traceability(s, decomposition)] for sid, s in specs.items()}
        verdict = _judge(ctx, outcome, decomposition, specs, round_no, verdict)
        # Merge deterministic failures into the verdict as blocking issues so the writer sees them.
        for sid, results in det.items():
            for r in results:
                if not r.passed:
                    verdict["overall"]["result"] = "revise" if verdict["overall"]["result"] == "pass" else verdict["overall"]["result"]
                    verdict["overall"]["blocking_issue_count"] += 1
                    for ps in verdict["per_spec"]:
                        if ps["spec_id"] == sid:
                            ps["issues"].append({"issue_id": f"DET-{r.check}", "severity": "blocking", "location": sid,
                                                 "description": r.detail, "suggested_resolution": "fix and re-validate"})
        result = verdict["overall"]["result"]
        if result == "pass":
            break
        if result == "escalate":
            break
        # Route issues to the specs that own them; cross-spec issues go to every related spec.
        issues_by_item = {}
        spec_to_item = {s["spec_id"]: s["work_item_id"] for s in specs.values()}
        for ps in verdict["per_spec"]:
            if ps["issues"]:
                issues_by_item.setdefault(spec_to_item[ps["spec_id"]], []).extend(ps["issues"])
        for bucket in verdict["coherence"].values():
            for issue in bucket:
                for sid in issue.get("related_spec_ids", []):
                    issues_by_item.setdefault(spec_to_item.get(sid, sid), []).append(issue)
        to_write = set(issues_by_item)
        if not to_write:  # judge said revise but attached no issues to any spec: treat as pass-with-notes
            break

    assert verdict is not None
    return specs, verdict


def _topological_waves(items: dict[str, dict]) -> list[list[str]]:
    remaining = dict(items)
    done: set[str] = set()
    waves = []
    while remaining:
        wave = [w for w, it in remaining.items() if all(d in done or d not in items for d in it.get("depends_on", []))]
        if not wave:  # cycle; dag_acyclic should have caught it, but never loop forever
            wave = list(remaining)
        waves.append(wave)
        for w in wave:
            done.add(w)
            remaining.pop(w)
    return waves


def gate_2(ctx: RunContext, outcome, specs: dict[str, dict], verdict: dict[str, Any], shas: dict[str, str],
           design_mr_urls: dict[str, str] | None = None):
    ctx.event("stage.entered", {"stage": RunStage.GATE_2_SPEC_READINESS.value}, stage="gate_2")
    kind = GateKind.SPEC_READINESS if verdict["overall"]["result"] == "pass" else GateKind.ESCALATION
    summary = verdict["overall"]["summary_for_human"]
    if kind == GateKind.ESCALATION:
        summary = (f"Spec judge result after {verdict['round']} round(s): {verdict['overall']['result']} "
                   f"({verdict['overall']['blocking_issue_count']} blocking).\n\n") + summary
    return open_gate_and_wait(
        ctx, kind, shas, summary, assignees=[outcome["owner"]["handle"]],
        options=(Decision.APPROVE, Decision.APPROVE_SUBSET, Decision.REQUEST_CHANGES, Decision.REJECT),
        urls=design_mr_urls,
    )
