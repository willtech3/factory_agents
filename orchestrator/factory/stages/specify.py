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
rounds, the current specs of the items it depends on or that depend on it (`sibling_specs/`, never its own) plus
its own previous draft (`prior/spec.json`) so a revision edits in place instead of rewriting with new names
(post-mortem issue 15). Interface mismatches between siblings are the judge's job to catch and route back, which
is cheaper than serialising the writers along the dependency chain.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .. import checks
from ..budget import reserve_budget
from ..config import agent_model
from ..gates import Decision, GateKind
from ..state import RunStage, WorkItemStage
from . import RunContext
from .common import auto_approval, hosted_planning_clause, make_ask_human, size_clause, open_gate_and_wait, persist_outputs, platform_skill, public, require_json, rubric_text, run_session, stamp_provenance

WRITER_DESCRIPTION = """Write the specification for work item {work_item_id} ({title}).
Inputs are under /mnt/session/uploads/ (outcome.json, decomposition.json, work_item.json, sibling_specs/,
and on a revision judge_issues.json and human_comments.md). {repo_clause} {skill_clause}
Write spec.json and spec.md to /mnt/session/outputs/ and validate spec.json with the validate.py tool."""

REPO_MOUNTED = "The repository is cloned at /workspace/repo."
REPO_ABSENT = ("The repository is NOT available in this session: write allowed_paths and interfaces from the decomposition "
               "and the outcome, and mark anything that depends on the actual code as an open question with a default.")

JUDGE_DESCRIPTION = """Judge the full set of specifications under /mnt/session/uploads/specs/ against rubric.md,
then judge the set's coherence and goal coverage against outcome.json and decomposition.json. {repo_clause}{owner_clause}
{skill_clause} This is round {round}. Write verdict.json to /mnt/session/outputs/ and validate it."""
OWNER_CLAUSE = """ The outcome owner's decisions from the previous gate are in /mnt/session/uploads/human_comments.md: treat
them as settled facts about what is acceptable (do not raise them again as owner decisions); judge whether the specs honour them."""
RESCORE_CLAUSE = """ This is a re-judgement. previous_verdict.json holds the scores you gave the drafts it names, and
/mnt/session/uploads/changes/<spec_id>.diff shows exactly what each writer changed since then ({unchanged}). Re-score only
the criteria a change touches, the criteria that scored below 2, and the issues previous_verdict.json raised; carry every
other score forward with its evidence. Re-check a carried score only when a change elsewhere in the set could move it (an
interface a sibling now relies on). Build or run a prototype only when a score you are re-deciding hinges on it."""
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
    skill_uploads, skill_clause = platform_skill(ctx)
    res = run_session(
        ctx, prefix=f"{wid}-r{round_no}-",
        agent_key="spec_writer", title=f"{ctx.run_id} spec {wid} r{round_no}", run_id=ctx.run_id, stage="specify",
        uploads={**uploads, **skill_uploads},
        description=WRITER_DESCRIPTION.format(work_item_id=wid, title=work_item["title"], repo_clause=repo_clause, skill_clause=skill_clause) + size_clause(ctx) + hosted_planning_clause(ctx),
        rubric_md=rubric_text(ctx, "spec", repo_available), max_iterations=ctx.policy.outcome_max_iterations,
        budget_usd=ctx.policy.stage_budget_usd["specify"], work_item_id=wid,
        custom_tools={"ask_human": make_ask_human(ctx, "spec_writer")}, repository=mount,
    )
    spec = require_json(res, "spec.json")
    spec["revision"] = round_no
    stamp_provenance(spec, ctx, res, "Spec writer", agent_model("spec_writer"))
    # The writer's own spec.md travels with the spec object (private key, stripped from everything persisted or
    # uploaded as spec.json) so the judge, the implementer, the reviewers and the merge judge all read this round's
    # prose, never a lexicographic "latest" from the store that an earlier cycle can win.
    markdown = res.outputs.get("spec.md", b"").decode(errors="replace").strip()
    if not markdown:
        raise RuntimeError(f"session {res.session_id} wrote spec.json for {wid} but no spec.md; outputs: {sorted(res.outputs)}")
    spec["_markdown"] = markdown
    res.outputs["spec.json"] = json.dumps(public(spec), indent=2).encode()
    persist_outputs(ctx, "specify", res, prefix=f"{wid}-r{round_no}-", work_item_id=wid)
    return spec


def spec_changes(previous: dict[str, dict], current: dict[str, dict]) -> tuple[dict[str, str], list[str]]:
    """Per spec the previous verdict judged: a unified diff of its normalised JSON to this round's draft (changed specs
    only), and the ids of the specs that did not change. A spec the previous verdict never saw is in neither: it is
    judged in full."""
    import difflib
    changed: dict[str, str] = {}
    unchanged: list[str] = []
    for sid, spec in sorted(current.items()):
        if sid not in previous:
            continue
        before = json.dumps(public(previous[sid]), indent=2, sort_keys=True).splitlines(keepends=True)
        after = json.dumps(public(spec), indent=2, sort_keys=True).splitlines(keepends=True)
        diff = "".join(difflib.unified_diff(before, after, f"previous/{sid}.json", f"specs/{sid}.json"))
        if diff:
            changed[sid] = diff
        else:
            unchanged.append(sid)
    return changed, unchanged


def _judge(ctx: RunContext, outcome, decomposition, specs: dict[str, dict], round_no: int,
           previous_verdict: dict | None, owner_comments: str | None = None,
           previous_specs: dict[str, dict] | None = None) -> dict[str, Any]:
    """`previous_specs` are the drafts `previous_verdict` scored, by spec id: with them, the judge gets what changed and
    re-scores only that (post-mortem issue 9: round-2 judges re-judged all 12 criteria from scratch and cost more than
    round 1)."""
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
    rescore = ""
    if previous_verdict:
        uploads["previous_verdict.json"] = json.dumps(previous_verdict, indent=2).encode()
        changed, unchanged = spec_changes(previous_specs or {}, specs)
        if changed or unchanged:
            for sid, diff in changed.items():
                uploads[f"changes/{sid}.diff"] = diff.encode()
            rescore = RESCORE_CLAUSE.format(unchanged=f"unchanged: {', '.join(unchanged)}" if unchanged else "every judged spec changed")
    if owner_comments:
        uploads["human_comments.md"] = owner_comments.encode()
    skill_uploads, skill_clause = platform_skill(ctx)
    reserve_budget(ctx, "judge")
    res = run_session(
        ctx, prefix=f"r{round_no}-",
        agent_key="spec_judge", title=f"{ctx.run_id} spec-judge r{round_no}", run_id=ctx.run_id, stage="judge",
        uploads={**uploads, **skill_uploads},
        description=JUDGE_DESCRIPTION.format(round=round_no, repo_clause=JUDGE_REPO_MOUNTED if repo_available else JUDGE_REPO_ABSENT,
                                             owner_clause=OWNER_CLAUSE if owner_comments else "", skill_clause=skill_clause) + rescore + size_clause(ctx) + hosted_planning_clause(ctx),
        rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["judge"], repository=mount,
    )
    verdict = require_json(res, "verdict.json")
    verdict["round"] = round_no
    stamp_provenance(verdict, ctx, res, "Spec judge", agent_model("spec_judge"))
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
    if ctx.store.state(ctx.run_id).stage != RunStage.SPECIFYING:   # `factory resume` re-enters the stage it paused in
        ctx.event("stage.entered", {"stage": RunStage.SPECIFYING.value}, stage="specify")
    items = {w["work_item_id"]: w for w in decomposition["work_items"]}
    specs: dict[str, dict] = {}
    by_item: dict[str, dict] = {}                 # work_item_id -> its current spec (the writer's own prior draft next round)
    issues_by_item: dict[str, list[dict]] = {}
    verdict: dict[str, Any] | None = previous_verdict
    # The drafts `verdict` scored, by spec id: the next judge gets what changed since (`spec_changes`).
    judged: dict[str, dict] = {s["spec_id"]: s for s in (prior_specs or {}).values()} if previous_verdict else {}
    to_write = set(items)
    first_round = round_offset + 1

    for round_no in range(first_round, first_round + ctx.policy.spec_judge_max_rounds):
        # Every writer of the round at once (none when the judge's issues name only specs outside the decomposition).
        batch = [w for w in items if w in to_write]
        if batch:
            reserve_budget(ctx, "specify", len(batch))      # the whole batch, before any writer starts
            with ThreadPoolExecutor(max_workers=min(ctx.policy.parallel_spec_writers, len(batch))) as pool:
                futures = {pool.submit(_write_one, ctx, outcome, decomposition, items[w],
                                       sibling_specs(items, w, specs),
                                       issues_by_item.get(w), (human_comments_by_item or {}).get(w), round_no,
                                       by_item.get(w) or ((prior_specs or {}).get(w) if round_no == first_round else None)): w
                           for w in batch}
                for fut, w in futures.items():
                    spec = fut.result()
                    specs[spec["spec_id"]] = spec
                    by_item[w] = spec
                    ctx.event("work_item.stage", {"stage": WorkItemStage.SPEC_JUDGING.value, "spec_id": spec["spec_id"]},
                              work_item_id=w, stage="specify")
        # Deterministic pre-checks: schema and traceability, cheap and before the judge spends tokens.
        det = {sid: [checks.schema_valid(public(s), ctx.cfg.schemas_dir / "spec.schema.json", ctx.cfg.schemas_dir),
                     checks.spec_traceability(s, decomposition),
                     checks.spec_size_estimate(s, ctx.policy.size_budget_lines),
                     checks.criteria_describe_product(s),
                     # A property of the spec, so it is checked here, before Gate 2: at merge only, an approved spec
                     # missing an e2e entry fails a check no implementer round can fix (design principle 21).
                     checks.e2e_coverage(s)] for sid, s in specs.items()}
        owner_notes = "\n\n".join(f"## {w}\n{c}" for w, c in (human_comments_by_item or {}).items()) or None
        verdict = _judge(ctx, outcome, decomposition, specs, round_no, verdict, owner_notes, previous_specs=judged)
        judged = dict(specs)
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


def sibling_specs(items: dict[str, dict], work_item_id: str, specs: dict[str, dict]) -> dict[str, dict]:
    """The current specs of the items `work_item_id` depends on or that depend on it, keyed by spec id: what the
    writer brief calls sibling_specs/. Never the item's own spec (`specs` is keyed by spec id, the items by work
    item id; the old filter compared the two and leaked every spec, the writer's own included)."""
    related = set(items.get(work_item_id, {}).get("depends_on", []))
    related |= {w for w, it in items.items() if work_item_id in it.get("depends_on", [])}
    return {sid: s for sid, s in specs.items() if s["work_item_id"] != work_item_id and s["work_item_id"] in related}


def _latest_spec_md(ctx: RunContext, work_item_id: str) -> str:
    """The writer's most recent spec.md for a work item, from the artifact store ('' when none)."""
    d = ctx.cfg.runs_dir / ctx.run_id / "artifacts" / "specify"
    prefix = f"{work_item_id}-r"
    cands = [p for p in d.glob(f"{prefix}*-spec.md") if not p.name.endswith(".sha256")] if d.exists() else []
    cands = sorted((p for p in cands if p.name[len(prefix):-len("-spec.md")].isdigit()),
                   key=lambda p: int(p.name[len(prefix):-len("-spec.md")]))        # numeric: r10 after r9, not before r2
    return cands[-1].read_text(errors="replace") if cands else ""


def gate_2(ctx: RunContext, outcome, specs: dict[str, dict], verdict: dict[str, Any], shas: dict[str, str],
           artifact_urls: dict[str, str] | None = None):
    ctx.event("stage.entered", {"stage": RunStage.GATE_2_SPEC_READINESS.value}, stage="gate_2")
    kind = GateKind.SPEC_READINESS if verdict["overall"]["result"] == "pass" else GateKind.ESCALATION
    summary = verdict["overall"]["summary_for_human"]
    if kind == GateKind.ESCALATION:
        summary = (f"Spec judge result after {verdict['round']} round(s): {verdict['overall']['result']} "
                   f"({verdict['overall']['blocking_issue_count']} blocking).\n\n") + summary
    blockers = ([] if kind == GateKind.SPEC_READINESS and not verdict["overall"].get("blocking_issue_count")
                else [f"judge: {verdict['overall']['result']}"])
    blockers += [q["question"] for s in specs.values() for q in s.get("open_questions", []) if q.get("blocking")]
    return open_gate_and_wait(
        ctx, kind, shas, summary, assignees=[outcome["owner"]["handle"]],
        options=(Decision.APPROVE, Decision.APPROVE_SUBSET, Decision.REQUEST_CHANGES, Decision.REJECT),
        urls=artifact_urls, auto_approve=auto_approval(ctx, "gate_2", blockers),
    )
