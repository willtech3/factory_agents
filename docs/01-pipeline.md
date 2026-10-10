# Pipeline mechanics

This document describes what happens between a human submitting an outcome and the merged change running in `dev`. The orchestrator in `orchestrator/factory/` implements it; `state.py` holds the legal transitions. The deployment platform (CI templates on GitHub Actions or GitLab CI, per-branch preview environments, the `dev` target) is assumed to exist and is described in [05-platform-and-deployment.md](05-platform-and-deployment.md).

## Vocabulary

| Term | Meaning |
|---|---|
| **Outcome** | The human-written business goal with success metrics and constraints. `schemas/outcome.schema.json`. |
| **Run** | One outcome's journey through the factory. Identified by `run_id`; all state is an append-only event log. |
| **Work item** | One node of the decomposition DAG: a feature, task, workflow, agent, spike, or migration. |
| **Spec** | The contract for one work item: acceptance criteria (each flagged `user_visible`), interfaces, allowed paths, test plan including `e2e` checks that run against a deployed instance. |
| **Preview environment** | The per-branch deployment the platform's CI creates for every branch. The end-to-end surface for implementer, reviewer, and the orchestrator's verification. |
| **Platform contract** | The skill (owned by the platform team; `skills/platform-contract/SKILL.md` is a placeholder) that says what the platform provides, what the app must define (a service manifest at a fixed path), and what agents must never touch. |
| **Stage session** | One Managed Agents session with one agent, one job, its own context and budget. |
| **Gate** | A point where a human decision is required before the run continues. Bound to artifact hashes. Read on an artifact page; decided on the gate surface (tracker, CLI, or forge; [ADR-0007](adr/0007-pluggable-gate-surfaces.md)). |
| **Change request (CR)** | A GitHub pull request or a GitLab merge request; the forge is pluggable ([ADR-0008](adr/0008-pluggable-forge-github-default.md)). |
| **Deterministic check** | A computation the orchestrator performs that no agent can override. |

## Stage by stage

### Stage 0: Intake
A human writes `outcome.json` (a form in the Factory Console, or a file). Required: statement, at least one success metric with a target, constraints (`must_not`, budget), the target repository, and an owner handle. The orchestrator validates the schema and opens the run.

### Stage 1: Decompose
One `decomposer` session, kicked off with `user.define_outcome` and `rubrics/decomposition.md`. The platform's outcome grader iterates the agent up to 4 times until the rubric is satisfied. The decomposer delegates factual questions about the codebase to goal-blind `codebase_researcher` threads so that research records what *is*, not what would justify the plan.

The decomposer may call `ask_human` for a **blocking** question. The session idles; the orchestrator surfaces the question (Console, Slack, email), waits for an answer, and returns it as the tool result. Non-blocking questions return their default immediately and are listed for the human at Gate 1.

The decomposer decomposes only as much as reviewability needs: one work item when the outcome is one coherent change a reviewer can approve as a single pull request, more only where parts must be approved or landed separately or an item would exceed size M, and never items that are not changes to the product (investigations, baselines, documentation riders). Rubric D2 scores this.

Outputs: `decomposition.json`, `decomposition.md`. Deterministic checks: schema, DAG acyclic, every success metric covered or explicitly marked not movable by code. One automatic retry on failure, then escalation.

### Gate 1: Decomposition review
The orchestrator renders `gate_1.html` (summary, metric coverage, work-item table, open questions, the full markdown, the artifact hashes) and opens the gate on the configured surface: a comment with the summary, links, and sha256 list on the run's tracker issue (default), or a **draft design change request** holding the files under `.factory/runs/<run_id>/` on a `factory/<run_id>/design` branch (`FACTORY_GATE_SURFACE=forge`). The owner reads the summary (under 150 lines), the work-item table, coverage, out-of-scope, and open questions. Decision options: approve, request changes (comments, from the tracker, the CLI, or inline on the design CR, become `feedback.md` for a re-run with `revision + 1`), reject (closes the run).

### Stage 2: Specify
One `spec_writer` session per work item with `rubrics/spec.md` as its outcome rubric, all at once (`Policy.parallel_spec_writers`, default 8). Each writer gets the whole decomposition so it knows what its siblings will do; interface mismatches between siblings are the judge's job to catch and route back, which is cheaper than serialising writers along the dependency chain (`Policy.spec_waves` restores waves). Then **one** `spec_judge` session receives the entire set, normalised to JSON (no prose rendering, so formatting cannot bias it), plus the rubric, outcome, and decomposition. It scores every criterion per spec with evidence and judges set-level coherence: interface mismatches, overlaps, gaps, ordering, contradictions, and a goal-coverage matrix mapping each success metric to the acceptance criteria that move it.

Verdict `pass` goes to Gate 2. `revise` routes issues back to the owning specs only (unchanged specs are not rewritten) for up to 3 rounds. `escalate`, or 3 rounds without a pass, opens an escalation gate with the judge's summary; the human can send the run back to decomposition.

Deterministic checks before the judge spends tokens: schema validity, traceability to the decomposition, and `e2e_coverage` (every `user_visible` criterion has a `test_plan.e2e` entry). Failures are injected into the verdict as blocking issues. The judge additionally scores S11 (deployable through the platform contract; manifest present and valid) and S12 (end-to-end coverage, with honest `user_visible` flags).

### Gate 2: Specification readiness
`gate_2.html` is rendered (judge summary first, goal-coverage matrix, a section per spec, open questions, hashes) and the gate opens on the surface; with the forge surface the design CR is updated with `specs/*.json`, `specs/*.md`, and `verdict.json`. The owner sees the judge's `summary_for_human` first, then the goal-coverage matrix, then per-spec issues. Options: approve all, approve a subset (the rest return to the writer), request changes, reject. Approval marks each spec `SPEC_APPROVED` and binds the decision to the artifact hashes.

### Stage 3: Implement, review, judge (per spec)
Specs are scheduled in dependency waves; up to `parallel_implementations` run at once, each on its own branch `factory/<run_id>/<spec_id>` cut from the default branch (or from the dependency's branch when stacked).

1. **Implementer** session, outcome rubric `rubrics/implementation.md`. Gets the repository the forge's way (GitHub: mounted at `/workspace/repo` through the native `github_repository` resource, token injected by Anthropic's git proxy; GitLab: clones with the vault credential of ADR-0003), works in vertical slices with a test checkpoint each, commits per slice with the criteria covered, pushes. The push triggers the platform's pipeline, which deploys the branch to its preview environment; the session's `preview.json` (read by the orchestrator from the forge's workflow-run and deployment records) carries the URL, and the implementer must run every `test_plan.e2e` entry against it and record the results. Writes `implementation_report.json`. May write `blocked.md` instead, which escalates.
2. **Authoritative verification** by the orchestrator (in production: a CI job on the branch): checkout `head_sha`, run `test_plan.commands`, compute the diff, read the preview URL for `head_sha` from the forge's deployment record (the platform's CI deploys the factory branch and records the Deployment; the orchestrator never derives the URL from an agent's report), run `test_plan.e2e` against it (`LocalVerifier.run_e2e`), and run the deterministic checks (scope, protected tests, test weakening, criterion-to-test mapping, command results, `e2e_coverage`, `manifest_valid`, `e2e_preview`). An e2e entry that could not run counts as failed. Results are stored and later handed to the merge judge verbatim.
3. **Reviewer**: `reviewer_passes` (default 2) independent `reviewer` sessions, each a fresh context receiving spec, diff, implementation report (as hypotheses), `preview.json`, and the branch. Each runs the preview e2e entries itself and probes the deployed instance; a `user_visible` criterion it could not exercise is `not_assessed`. Findings are merged and de-duplicated by location, strictest severity wins. Each finding has a claim, evidence with reproduction, the criterion it violates, and a refutation condition.
4. If any blocking or major finding stands, or a deterministic check failed: **Implementer** again (a new session on the same branch) with `findings.json`. It must answer every finding: `fixed` with commit and test, `refuted` with evidence meeting the refutation condition, or `accepted_risk` for a human to decide. Back to step 2.
5. Round 2 review is a **new** reviewer session that receives the previous findings and the response, judges each response against the refutation condition (`upheld` / `withdrawn`), then reviews the new hunks. Default `review_max_rounds = 2`; more rounds raise false positives faster than recall ([academic-2026.md §2](research/academic-2026.md), Song).
6. **Merge judge**: one independent session with spec, diff, findings, responses, the orchestrator's test results and deterministic checks. Re-runs reproductions itself. Decides `ready`, `another_round` (with exactly what to do), or `escalate`. A failing deterministic check forces `another_round` regardless of what the judge writes.

### Stage 4: Integrate
First the orchestrator commits the **run record** on the implementation branch from its own checkout: `.factory/runs/<run_id>/specs/<spec_id>.{json,md}`, the spec judge's `verdict.json`, and `evidence/<spec_id>/merge_readiness.json`, with a fixed author and date so the commit sha is reproducible. Then the `integrator` session assembles the change-request description from `templates/cr_template.md` (judge summary first, decisions needed, acceptance criteria table with evidence, contested findings, verification, provenance links to every session) and calls the `open_change_request` custom tool. The orchestrator executes it with its own forge token, applies labels, requests reviewers, and links the design CR or tracker issue.

### Gate 3: Human code review
Ordinary pull-request or merge-request review on the forge. The factory never merges. The change-request watcher (`factory watch <run_id>`, `orchestrator/factory/watcher.py`; the Console's **Watch** button starts the same process) polls each open change request through the `Forge` protocol and records `cr.merged` (with the merge commit) or `cr.closed`; a merge moves the item to `MERGED` and starts Stage 5. The watcher is idempotent across restarts: it re-derives the run from the event log and appends only what is missing. Human review comments on a factory CR can be routed back as a new implementer round (`CR_OPEN -> IMPLEMENTING`), which is how a human steers an almost-right change without taking it over.

### Stage 5: Observed deploy to dev
No agent runs. The platform's own pipeline on the default branch deploys to `dev` with its existing templates, as it would for any merge. The orchestrator (`stages/deploy_observe.py`) moves the work item `MERGED -> DEPLOYING_DEV`, finds the pipeline for the merge commit (a forge webhook may report it first; polling through the `Forge` protocol is the source of truth), and records `deploy.started`, then `deploy.succeeded` or `deploy.failed` with a log excerpt. On success it reads the dev URL from the `dev` deployment or environment record, runs the spec's `test_plan.e2e` entries plus an optional smoke suite against it, and records `e2e.dev_passed` or `e2e.dev_failed`. Success ends the work item at `DEPLOYED_DEV`.

Any failure opens a `DEPLOY_FAILURE` gate carrying the pipeline or e2e log excerpt. Options: `request_changes` routes the item back to `IMPLEMENTING` on a fresh branch with the comments as the first finding (a follow-up change request, not a revert); `approve` says the failure was the platform's and the next pipeline is watched once more; `reject` closes the item. Promotion beyond `dev` is a human act outside the factory. Rationale and evidence: [05-platform-and-deployment.md](05-platform-and-deployment.md), [ADR-0006](adr/0006-observed-deployment-not-agent-driven.md).

## Run-level state

```
intake ─► decomposing ─► gate_1 ─► specifying ─► gate_2 ─► implementing ─► completed
             │  ▲          │ changes  │  ▲          │ changes      │
             │  └──────────┘          │  └──────────┘              ├─► escalated ─► (back to a stage | closed)
             │                        │ reject                     ├─► paused_budget ─► (resume | closed)
             └─► escalated            └─► closed                   └─► failed
```

Work items have their own state (`pending → spec_drafting → spec_judging → spec_approved → implementing → reviewing → responding → merge_judging → integrating → cr_open → merged → deploying_dev → deployed_dev`, with `blocked`, `escalated`, `closed` side exits; a dependant may start once its upstream is `merged` or later). `state.py` encodes both tables and rejects illegal transitions, so a bug in a stage module cannot put a run into an impossible state.

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
| Preview wait | 30 min | How long authoritative verification waits for the platform's pipeline to deploy `head_sha` to its preview before `e2e_preview` fails closed. |
| Dev pipeline timeout | 60 min | Stage 5: how long the orchestrator watches the dev pipeline before opening a `DEPLOY_FAILURE` gate. |

## Failure handling

- **Session error or `retries_exhausted`:** the stage is retried once with the same inputs; then escalation.
- **Schema-invalid artifact:** the sandbox validator catches most; the orchestrator re-validates with full `jsonschema` and treats failure like a deterministic check failure.
- **`blocked.md`:** implementer could not proceed without changing scope or resolving a spec conflict. Escalation gate with the file's content; the human can send the spec back to the writer.
- **Non-convergence:** same finding across two rounds, or judge `escalate`. Escalation gate with the exchange summarised.
- **Budget:** `budget_reached` on any session pauses the run; a human raises the cap or closes.
- **No preview for the branch:** `preview.json` reports `pending` or `failed`; agents run `local` e2e entries meanwhile, and the orchestrator's `e2e_preview` check fails closed until the platform's pipeline has deployed `head_sha`. A persistent platform failure is a `blocked.md` from the implementer, not something it works around.
- **Dev deploy or dev e2e failure after merge:** `DEPLOY_FAILURE` gate (Stage 5) with the log excerpt; back to `IMPLEMENTING` on a fresh branch, re-observe, or close.

## Why not one coordinator session for the whole run

Managed Agents supports a coordinator with a roster, and it is the right tool *within* a stage (the implementer rostering researchers; the reviewer rostering copies of itself). It is the wrong tool *across* stages: human gates need the run to park for hours or days, judges need context that provably excludes the author's reasoning, each stage wants its own versioned agent and budget, and evals need to run one stage in isolation with frozen inputs. See [ADR-0001](adr/0001-orchestrator-driven-stage-sessions.md).
