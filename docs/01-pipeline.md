# Pipeline mechanics

This document describes what happens between a human submitting an outcome and a human merging code. The orchestrator in `orchestrator/factory/` implements it; `state.py` holds the legal transitions.

## Vocabulary

| Term | Meaning |
|---|---|
| **Outcome** | The human-written business goal with success metrics and constraints. `schemas/outcome.schema.json`. |
| **Run** | One outcome's journey through the factory. Identified by `run_id`; all state is an append-only event log. |
| **Work item** | One node of the decomposition DAG: a feature, task, workflow, agent, spike, or migration. |
| **Spec** | The contract for one work item: acceptance criteria, interfaces, allowed paths, test plan. |
| **Stage session** | One Managed Agents session with one agent, one job, its own context and budget. |
| **Gate** | A point where a human decision is required before the run continues. Bound to artifact hashes. |
| **Deterministic check** | A computation the orchestrator performs that no agent can override. |

## Stage by stage

### Stage 0: Intake
A human writes `outcome.json` (a form in the Factory Console, or a file). Required: statement, at least one success metric with a target, constraints (`must_not`, budget), the target repository, and an owner handle. The orchestrator validates the schema and opens the run.

### Stage 1: Decompose
One `decomposer` session, kicked off with `user.define_outcome` and `rubrics/decomposition.md`. The platform's outcome grader iterates the agent up to 4 times until the rubric is satisfied. The decomposer delegates factual questions about the codebase to goal-blind `codebase_researcher` threads so that research records what *is*, not what would justify the plan.

The decomposer may call `ask_human` for a **blocking** question. The session idles; the orchestrator surfaces the question (Console, Slack, email), waits for an answer, and returns it as the tool result. Non-blocking questions return their default immediately and are listed for the human at Gate 1.

Outputs: `decomposition.json`, `decomposition.md`. Deterministic checks: schema, DAG acyclic, every success metric covered or explicitly marked not movable by code. One automatic retry on failure, then escalation.

### Gate 1: Decomposition review
The orchestrator commits `decomposition.md` and `decomposition.json` to `.factory/runs/<run_id>/` on a `factory/<run_id>/design` branch and opens a **draft design MR** in the target repository. The owner reads the summary (under 150 lines), the work-item table, coverage, out-of-scope, and open questions. Decision options: approve, request changes (comments, including inline MR comments, become `feedback.md` for a re-run with `revision + 1`), reject (closes the run).

### Stage 2: Specify
For each work item, in dependency-order waves (so a spec can reference the interfaces of specs it depends on), one `spec_writer` session with `rubrics/spec.md` as its outcome rubric. Writers run in parallel within a wave. Then **one** `spec_judge` session receives the entire set, normalised to JSON (no prose rendering, so formatting cannot bias it), plus the rubric, outcome, and decomposition. It scores every criterion per spec with evidence and judges set-level coherence: interface mismatches, overlaps, gaps, ordering, contradictions, and a goal-coverage matrix mapping each success metric to the acceptance criteria that move it.

Verdict `pass` goes to Gate 2. `revise` routes issues back to the owning specs only (unchanged specs are not rewritten) for up to 3 rounds. `escalate`, or 3 rounds without a pass, opens an escalation gate with the judge's summary; the human can send the run back to decomposition.

Deterministic checks before the judge spends tokens: schema validity, traceability to the decomposition. Failures are injected into the verdict as blocking issues.

### Gate 2: Specification readiness
The design MR is updated with `specs/*.json` and `verdict.json`. The owner sees the judge's `summary_for_human` first, then the goal-coverage matrix, then per-spec issues. Options: approve all, approve a subset (the rest return to the writer), request changes, reject. Approval marks each spec `SPEC_APPROVED` and binds the decision to the artifact hashes.

### Stage 3: Implement, review, judge (per spec)
Specs are scheduled in dependency waves; up to `parallel_implementations` run at once, each on its own branch `factory/<run_id>/<spec_id>` cut from the default branch (or from the dependency's branch when stacked).

1. **Implementer** session, outcome rubric `rubrics/implementation.md`. Clones the repository (GitLab token substituted at egress; see ADR-0003), works in vertical slices with a test checkpoint each, commits per slice with the criteria covered, pushes. Writes `implementation_report.json`. May write `blocked.md` instead, which escalates.
2. **Authoritative verification** by the orchestrator (in production: a CI job on the branch): checkout `head_sha`, run `test_plan.commands`, compute the diff, run the deterministic checks (scope, protected tests, test weakening, criterion-to-test mapping, command results). Results are stored and later handed to the merge judge verbatim.
3. **Reviewer**: `reviewer_passes` (default 2) independent `reviewer` sessions, each a fresh context receiving spec, diff, implementation report (as hypotheses), and the branch. Findings are merged and de-duplicated by location, strictest severity wins. Each finding has a claim, evidence with reproduction, the criterion it violates, and a refutation condition.
4. If any blocking or major finding stands, or a deterministic check failed: **Implementer** again (a new session on the same branch) with `findings.json`. It must answer every finding: `fixed` with commit and test, `refuted` with evidence meeting the refutation condition, or `accepted_risk` for a human to decide. Back to step 2.
5. Round 2 review is a **new** reviewer session that receives the previous findings and the response, judges each response against the refutation condition (`upheld` / `withdrawn`), then reviews the new hunks. Default `review_max_rounds = 2`; more rounds raise false positives faster than recall ([academic-2026.md §2](research/academic-2026.md), Song).
6. **Merge judge**: one independent session with spec, diff, findings, responses, the orchestrator's test results and deterministic checks. Re-runs reproductions itself. Decides `ready`, `another_round` (with exactly what to do), or `escalate`. A failing deterministic check forces `another_round` regardless of what the judge writes.

### Stage 4: Integrate
`integrator` session assembles the MR description from `templates/mr_template.md` (judge summary first, decisions needed, acceptance criteria table with evidence, contested findings, verification, provenance links to every session) and calls the `open_merge_request` custom tool. The orchestrator executes it with its own GitLab token, applies labels, assigns reviewers, and links the design MR.

### Gate 3: Human code review
Ordinary GitLab review. The factory never merges. An MR watcher (webhook from GitLab) records `mr.merged` or `mr.closed`; a merge unblocks dependent specs. Human review comments on a factory MR can be routed back as a new implementer round (`MR_OPEN -> IMPLEMENTING`), which is how a human steers an almost-right change without taking it over.

## Run-level state

```
intake ─► decomposing ─► gate_1 ─► specifying ─► gate_2 ─► implementing ─► completed
             │  ▲          │ changes  │  ▲          │ changes      │
             │  └──────────┘          │  └──────────┘              ├─► escalated ─► (back to a stage | closed)
             │                        │ reject                     ├─► paused_budget ─► (resume | closed)
             └─► escalated            └─► closed                   └─► failed
```

Work items have their own state (`pending → spec_drafting → spec_judging → spec_approved → implementing → reviewing → responding → merge_judging → integrating → mr_open → merged`, with `blocked`, `escalated`, `closed` side exits). `state.py` encodes both tables and rejects illegal transitions, so a bug in a stage module cannot put a run into an impossible state.

## Budgets and limits

| Knob | Default | Why |
|---|---|---|
| Run budget | `outcome.constraints.budget_usd`, else $150 | Every session carries a hard dollar cap (platform-enforced); the run total is the sum of stage caps. |
| Stage caps | decompose 15, specify 10, judge 8, implement 40, review 15, merge judge 8, integrate 2 (USD, list price) | A session that hits its cap pauses with `budget_reached`; the orchestrator escalates rather than silently raising it. |
| Outcome iterations | 4 | Platform grader loop inside writing sessions. |
| Spec judge rounds | 3 | Then escalate. |
| Review rounds | 2 | One fresh review per round. |
| Reviewer passes | 2 | Independent copies, merged. |
| Parallel implementations | 3 | Separate branches; serial merge via Gate 3. |

## Failure handling

- **Session error or `retries_exhausted`:** the stage is retried once with the same inputs; then escalation.
- **Schema-invalid artifact:** the sandbox validator catches most; the orchestrator re-validates with full `jsonschema` and treats failure like a deterministic check failure.
- **`blocked.md`:** implementer could not proceed without changing scope or resolving a spec conflict. Escalation gate with the file's content; the human can send the spec back to the writer.
- **Non-convergence:** same finding across two rounds, or judge `escalate`. Escalation gate with the exchange summarised.
- **Budget:** `budget_reached` on any session pauses the run; a human raises the cap or closes.

## Why not one coordinator session for the whole run

Managed Agents supports a coordinator with a roster, and it is the right tool *within* a stage (the implementer rostering researchers; the reviewer rostering copies of itself). It is the wrong tool *across* stages: human gates need the run to park for hours or days, judges need context that provably excludes the author's reasoning, each stage wants its own versioned agent and budget, and evals need to run one stage in isolation with frozen inputs. See [ADR-0001](adr/0001-orchestrator-driven-stage-sessions.md).
