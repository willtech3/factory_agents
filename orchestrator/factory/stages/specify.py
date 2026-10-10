"""Stage 2: decomposition -> specs -> spec judge loop -> Gate 2.

Loop shape (see docs/01-pipeline.md):
  for round in 1..spec_judge_max_rounds:
      write/revise every spec that is not yet passing   (parallel sessions, one per work item, all at once)
      judge the whole set in one independent session
      if verdict.pass -> Gate 2
      if verdict.escalate -> escalation gate
  after max rounds -> escalation gate with the last verdict

Round numbers are monotonic across Gate 2 cycles: a send-back re-enters `run_specify` with `round_offset` set to the
last round the owner saw and `previous_verdict` set to that cycle's final verdict, so artefact names
(`{wid}-r{round}-spec.json`, `r{round}-verdict.json`) never collide with what the owner decided on, and the first judge
after a send-back sees the previous verdict next to the owner's comments (post-mortem issue 1).

Writers run all at once: each gets the decomposition (so it knows what its siblings will do) and, on later
rounds, the siblings' current specs. Interface mismatches between siblings are the judge's job to catch and
route back, which is cheaper than serialising the writers along the dependency chain. `Policy.spec_waves`
restores dependency-order waves for repositories where that trade-off goes the other way.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .. import checks
from ..gates import Decision, GateKind
from ..state import RunStage, WorkItemStage
from . import RunContext
from .common import make_ask_human, open_gate_and_wait, persist_outputs, provenance, public, require_json, rubric_text, run_session

WRITER_DESCRIPTION = """Write the specification for work item {work_item_id} ({title}).
Inputs are under /mnt/session/uploads/ (outcome.json, decomposition.json, work_item.json, sibling_specs/,
and on a revision judge_issues.json and human_comments.md). {repo_clause}
Write spec.json and spec.md to /mnt/session/outputs/ and validate spec.json with the validate.py tool."""

REPO_MOUNTED = "The repository is cloned at /workspace/repo."
REPO_ABSENT = ("The repository is NOT available in this session: write allowed_paths and interfaces from the decomposition "
               "and the outcome, and mark anything that depends on the actual code as an open question with a default.")

JUDGE_DESCRIPTION = """Judge the full set of specifications under /mnt/session/uploads/specs/ against rubric.md,
then judge the set's coherence and goal coverage against outcome.json and decomposition.json. {repo_clause}{owner_clause}
This is round {round}. Write verdict.json to /mnt/session/outputs/ and validate it."""
OWNER_CLAUSE = """ The outcome owner's decisions from the previous gate are in /mnt/session/uploads/human_comments.md: treat
them as settled facts about what is acceptable (do not raise them again as owner decisions); judge whether the specs honour them."""
JUDGE_REPO_MOUNTED = "The repository is cloned at /workspace/repo for checking paths, commands, and current behaviour."
JUDGE_REPO_ABSENT = ("The repository is NOT available in this session: rubric.md says how to score the criteria that would "
                     "need it. Do not fail a spec for a path or command you cannot check; note it as unverified.")


def _write_one(ctx: RunContext, outcome, decomposition, work_item, sibling_specs: dict[str, dict],
               judge_issues: list[dict] | None, human_comments: str | None, round_no: int,
               prior_spec: dict[str, Any] | None = None) -> dict[str, Any]:
    wid = work_item["work_item_id"]
    ctx.event("work_item.stage", {"stage": WorkItemStage.SPEC_DRAFTING.value, "spec_round": round_no},
              work_item_id=wid, stage="specify")
    uploads = {
        "outcome.json": json.dumps(outcome, indent=2).encode(),
        "decomposition.json": json.dumps(decomposition, indent=2).encode(),
        "work_item.json": json.dumps(work_item, indent=2).encode(),
    }
    for sid, s in sibling_specs.items():
        uploads[f"sibling_specs/{sid}.json"] = json.dumps(public(s), indent=2).encode()
    if judge_issues is not None:
        uploads["judge_issues.json"] = json.dumps(judge_issues, indent=2).encode()
    if human_comments:
        uploads["human_comments.md"] = human_comments.encode()
    if prior_spec is not None:
        uploads["prior/spec.json"] = json.dumps(public(prior_spec), indent=2).encode()   # the draft being revised
    from .decompose import repository_access
    mount, repo_clause, repo_available = repository_access(ctx, outcome, REPO_MOUNTED, REPO_ABSENT)
    res = run_session(
        ctx, prefix=f"{wid}-r{round_no}-",
        agent_key="spec_writer", title=f"{ctx.run_id} spec {wid} r{round_no}", run_id=ctx.run_id, stage="specify",
        uploads=uploads, description=WRITER_DESCRIPTION.format(work_item_id=wid, title=work_item["title"], repo_clause=repo_clause),
        rubric_md=rubric_text(ctx, "spec", repo_available), max_iterations=ctx.policy.outcome_max_iterations,
        budget_usd=ctx.policy.stage_budget_usd["specify"], work_item_id=wid,
        custom_tools={"ask_human": make_ask_human(ctx, "spec_writer")}, repository=mount,
    )
    spec = require_json(res, "spec.json")
    spec["revision"] = round_no
    spec.setdefault("produced_by", provenance(ctx, res, "Spec writer", "claude-sonnet-5-5", {}))
    # The writer's own spec.md travels with the spec object (private key, stripped from everything persisted or
    # uploaded as spec.json) so the judge, the implementer, the reviewers and the merge judge all read this round's
    # prose, never a lexicographic "latest" from the store that an earlier cycle can win.
    markdown = res.outputs.get("spec.md", b"").decode(errors="replace").strip()
    if not markdown:
        raise RuntimeError(f"session {res.session_id} wrote spec.json for {wid} but no spec.md; outputs: {sorted(res.outputs)}")
    spec["_markdown"] = markdown
    res.outputs["spec.json"] = json.dumps(public(spec), indent=2).encode()
    persist_outputs(ctx, "specify", res, prefix=f"{wid}-r{round_no}-")
    return spec


def _judge(ctx: RunContext, outcome, decomposition, specs: dict[str, dict], round_no: int,
           previous_verdict: dict | None, owner_comments: str | None = None) -> dict[str, Any]:
    from .decompose import repository_access
    mount, _clause, repo_available = repository_access(ctx, outcome)
    uploads = {
        "outcome.json": json.dumps(outcome, indent=2).encode(),
        "decomposition.json": json.dumps(decomposition, indent=2).encode(),
        "rubric.md": rubric_text(ctx, "spec", repo_available).encode(),
    }
    for sid, s in specs.items():
        # The JSON is the contract and is normalised so formatting cannot bias the judge; the writer's spec.md is
        # uploaded beside it because the rubric scores its readability (S10) and the judge's prompt names it.
        uploads[f"specs/{sid}.json"] = json.dumps(public(s), indent=2, sort_keys=True).encode()
        md = s.get("_markdown") or _latest_spec_md(ctx, s["work_item_id"])
        if md:
            uploads[f"specs/{sid}.md"] = md.encode() if isinstance(md, str) else md
    if previous_verdict:
        uploads["previous_verdict.json"] = json.dumps(previous_verdict, indent=2).encode()
    if owner_comments:
        uploads["human_comments.md"] = owner_comments.encode()
    res = run_session(
        ctx, prefix=f"r{round_no}-",
        agent_key="spec_judge", title=f"{ctx.run_id} spec-judge r{round_no}", run_id=ctx.run_id, stage="judge",
        uploads=uploads, description=JUDGE_DESCRIPTION.format(round=round_no, repo_clause=JUDGE_REPO_MOUNTED if repo_available else JUDGE_REPO_ABSENT,
                                                              owner_clause=OWNER_CLAUSE if owner_comments else ""),
        rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["judge"], repository=mount,
    )
    verdict = require_json(res, "verdict.json")
    verdict["round"] = round_no
    verdict.setdefault("produced_by", provenance(ctx, res, "Spec judge", "claude-opus-5-5", {}))
    res.outputs["verdict.json"] = json.dumps(verdict, indent=2).encode()
    persist_outputs(ctx, "judge", res, prefix=f"r{round_no}-")
    ctx.event("judge.verdict", {"round": round_no, "result": verdict["overall"]["result"],
                                "blocking": verdict["overall"]["blocking_issue_count"]}, stage="judge", actor="agent")
    return verdict


def run_specify(ctx: RunContext, outcome, decomposition, human_comments_by_item: dict[str, str] | None = None,
                prior_specs: dict[str, dict] | None = None, *, previous_verdict: dict[str, Any] | None = None,
                round_offset: int = 0) -> tuple[dict[str, dict], dict[str, Any]]:
    """`human_comments_by_item` are the owner's gate notes per work item (also shown to the judge as settled
    decisions); `prior_specs` are the specs the owner commented on, keyed by work item, so a writer revises
    rather than restarts after a request-changes at Gate 2. On that re-entry `previous_verdict` is the verdict the
    owner saw and `round_offset` its round number: rounds continue from there (the first judge of the new cycle
    gets previous_verdict.json together with human_comments.md) and no artefact name is reused."""
    ctx.event("stage.entered", {"stage": RunStage.SPECIFYING.value}, stage="specify")
    items = {w["work_item_id"]: w for w in decomposition["work_items"]}
    specs: dict[str, dict] = {}
    issues_by_item: dict[str, list[dict]] = {}
    verdict: dict[str, Any] | None = previous_verdict
    to_write = set(items)
    first_round = round_offset + 1

    for round_no in range(first_round, first_round + ctx.policy.spec_judge_max_rounds):
        # All writers at once (default), or dependency-order waves when the policy asks for them.
        waves = _topological_waves(items) if ctx.policy.spec_waves else [list(items)]
        for wave in waves:
            batch = [w for w in wave if w in to_write]
            if not batch:
                continue
            with ThreadPoolExecutor(max_workers=min(ctx.policy.parallel_spec_writers, len(batch))) as pool:
                futures = {pool.submit(_write_one, ctx, outcome, decomposition, items[w],
                                       {s: specs[s] for s in specs if s != w},
                                       issues_by_item.get(w), (human_comments_by_item or {}).get(w), round_no,
                                       (prior_specs or {}).get(w) if round_no == first_round else None): w
                           for w in batch}
                for fut, w in futures.items():
                    spec = fut.result()
                    specs[spec["spec_id"]] = spec
                    ctx.event("work_item.stage", {"stage": WorkItemStage.SPEC_JUDGING.value, "spec_id": spec["spec_id"]},
                              work_item_id=w, stage="specify")
        # Deterministic pre-checks: schema and traceability, cheap and before the judge spends tokens.
        det = {sid: [checks.schema_valid(public(s), ctx.cfg.schemas_dir / "spec.schema.json", ctx.cfg.schemas_dir),
                     checks.spec_traceability(s, decomposition)] for sid, s in specs.items()}
        owner_notes = "\n\n".join(f"## {w}\n{c}" for w, c in (human_comments_by_item or {}).items()) or None
        verdict = _judge(ctx, outcome, decomposition, specs, round_no, verdict, owner_notes)
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


def _latest_spec_md(ctx: RunContext, work_item_id: str) -> str:
    """The writer's most recent spec.md for a work item, from the artifact store ('' when none)."""
    d = ctx.cfg.runs_dir / ctx.run_id / "artifacts" / "specify"
    prefix = f"{work_item_id}-r"
    cands = [p for p in d.glob(f"{prefix}*-spec.md") if not p.name.endswith(".sha256")] if d.exists() else []
    cands = sorted((p for p in cands if p.name[len(prefix):-len("-spec.md")].isdigit()),
                   key=lambda p: int(p.name[len(prefix):-len("-spec.md")]))        # numeric: r10 after r9, not before r2
    return cands[-1].read_text(errors="replace") if cands else ""


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
           artifact_urls: dict[str, str] | None = None):
    ctx.event("stage.entered", {"stage": RunStage.GATE_2_SPEC_READINESS.value}, stage="gate_2")
    kind = GateKind.SPEC_READINESS if verdict["overall"]["result"] == "pass" else GateKind.ESCALATION
    summary = verdict["overall"]["summary_for_human"]
    if kind == GateKind.ESCALATION:
        summary = (f"Spec judge result after {verdict['round']} round(s): {verdict['overall']['result']} "
                   f"({verdict['overall']['blocking_issue_count']} blocking).\n\n") + summary
    return open_gate_and_wait(
        ctx, kind, shas, summary, assignees=[outcome["owner"]["handle"]],
        options=(Decision.APPROVE, Decision.APPROVE_SUBSET, Decision.REQUEST_CHANGES, Decision.REJECT),
        urls=artifact_urls,
    )
