# Humans in the loop: what people see and do

The factory has three kinds of people: the **outcome owner** (product or business), **engineers** (code owners who review change requests and handle escalations), and a **factory operator** (whoever tunes agents and watches cost). This page walks through the run from each seat, using the Ledgerline example in `examples/ledgerline/`.

## Surfaces

Each surface does one job. Reading is separated from deciding so that the thing a human reads can be rich (a page) and the thing they decide on can be where they already work (the tracker). Which surface records the decision is configuration ([ADR-0007](adr/0007-pluggable-gate-surfaces.md)); the CLI is always available alongside it.

| Surface | Role | Who uses it | What for | Status |
|---|---|---|---|---|
| **Factory Console** (`factory web`; [08-factory-console.md](08-factory-console.md)) | workbench for the first stages | owner, engineers | Write an outcome; the runs board; the inbox of gates and questions; Gate 1 and Gate 2 read as a document with a sign-off rail that binds the hashes; per-item notes that become feedback; approve-subset by checkbox; once pull requests exist, **Watch** starts the change-request watcher and the run page follows the merge and the dev deploy | built (v0, local) |
| **CLI / Cowork** (`factory gates`, `factory decide`, `factory answer`) | the same, from a terminal | operator, owner | Open gates and questions with their hashes; decide; answer | built |
| **Artifact pages** (`runs/<run_id>/pages/gate_*.html`) | reading | owner, engineers | One self-contained page per gate: judge summary first, goal-coverage matrix, per-spec sections, open questions, the hash list. Local files; publishing as a claude.ai artifact is a stub | built (local) |
| **Tracker** (Linear by default; Jira stub) | board and record | all | A project per run, an issue per work item whose state mirrors the pipeline, gates as comments with a label, decisions by label plus comment, questions as threaded comments | built, unverified live |
| **Slack / email** | notifications | owner, engineers | Deep links to the page and the issue; blocking questions; optional approve button for low-risk gates | later (Milestone B) |
| **Forge change request** (GitHub PR by default, GitLab MR) | from Gate 3 | engineers | Ordinary code review and merge, with the approved spec and verdicts linked from Factory’s artifact store.. Gates 1 and 2 never appear here ([ADR-0011](adr/0011-no-design-change-requests.md)) | built, unverified live |
| **Anthropic Console session viewer** | trace | operator, curious engineers | Full trace of any session: tool calls, grader iterations, cost per thread. Every artifact's `produced_by.session_id` deep-links here | platform |
| **`ant beta:sessions connect`** | live attach | operator | Attach a terminal to a live session to watch, nudge, or answer a paused tool call | platform |

The design choice that matters most: **decisions happen where the team already works, and code review stays real.** With the tracker surface the owner sees a Linear issue with a short summary, a link to the page, and three labels to choose from; with the forge surface the decomposition and specs are files on a draft change request and inline comments on line 42 of a markdown file are the feedback. Either way Gate 3 is an ordinary pull request or merge request that an engineer reads. Boeckeler's complaint that spec-driven tools make you review "all these markdown files" with no good review experience is answered by rendering a page for reading and by using the review tool the team already has for code ([practitioners.md §5](research/practitioners.md)).

## Walkthrough: the Ledgerline run

### 1. Intake (owner, 10 minutes)
Priya, Head of Product for Billing, opens the Console and clicks **New outcome**. The form asks for the statement in her own words, success metrics with baseline and target, constraints (`must_not`, budget, deadline), the target repository, and links. The form validates against `outcome.schema.json` and refuses vague metrics ("better retention") without a target. She submits; the run appears on the board in **Decomposing**.

### 2. A blocking question (owner, 2 minutes, async)
Twenty minutes in, Slack: *"The decomposer has a blocking question on run_7f3a: Should retries follow Stripe Smart Retries or our own schedule? Why it matters: own schedule adds a scheduler work item and a week. Default if unanswered: Stripe Smart Retries."* Two buttons with the options, or a text reply. Priya taps *Stripe Smart Retries*. The session resumes. The Q&A is appended to `outcome.clarifications` and shows on the run timeline.

Design rule: only **blocking** questions interrupt a human. Non-blocking ones take their default and are listed at the next gate. Oversight has a capacity; flooding reviewers reduces safety ([academic-2026.md §6](research/academic-2026.md), Turan).

### 3. Gate 1: decomposition review (owner, 15 minutes)
Slack (later; today the Linear notification): *"Decomposition ready for review: 4 work items, all 3 metrics covered, 2 open questions."* The run's Linear issue has a comment with that summary, the artifact list with hashes, and a link to `gate_1.html`, which shows:

- a one-paragraph *What you are approving*
- the work-item table (id, title, size, depends on, metrics)
- coverage per metric, out-of-scope list, open questions with defaults, assumptions, risks
- below the fold, the rubric scores the platform grader reached and the deterministic check results

Priya reads it in a couple of minutes. She comments on the issue: *"Suspension timing isn't here. If Stripe retries for 3 weeks, when do we actually suspend?"* and applies the `factory:request-changes` label (in the Factory Console she would leave it as a note on the work item; from a terminal, `factory decide`). The orchestrator pulls her comment into `feedback.md`, re-runs the decomposer with the prior decomposition attached, and bumps `revision` to 2. Twenty-five minutes later: revision 2 adds `wi-suspension-timing` with `human_judgement_required: true` and a note that she must set the maximum grace period. She approves.

Her approval is recorded with the sha256 of the exact files she saw: the gate comment lists them and the orchestrator copies them from its own comment into the decision. If the agent had rewritten the decomposition between her reading and her clicking, the decision would be rejected as stale and she would be asked to look again (Loopjacking defence; [practitioners.md §13](research/practitioners.md)).

### 4. Specification and the judge loop (nobody, ~90 minutes)
The owner sees the run move to **Specifying** and the timeline fill: four spec-writer sessions, then a spec-judge session (round 1: `revise`, 2 blocking issues about mismatched event names and an undefined scope), two targeted rewrites, a judge round 2: `pass`. Each judge verdict is a timeline entry with a one-line summary. No human is involved unless the loop escalates.

### 5. Gate 2: specification readiness (owner + one engineer, 30 minutes)
Slack to Priya and to the billing code owner: *"Specs ready: 4 specs passed the judge in 2 rounds. Judge summary: ... One judgement call for you: spec_dunning-emails includes the email copy as acceptance criteria, so you are approving customer-facing wording here."* `gate_2.html` puts the judge's `summary_for_human` at the top, then the **goal coverage matrix** (metric → acceptance criteria), then a section per spec with its acceptance criteria, interfaces, allowed paths, test plan, and the judge's issues (the one `minor` about 3DS events). Work-item issues on the board are in Todo.

Priya reads the email copy in the `spec_dunning-emails` section and writes two replacement sentences in a comment. The engineer reads the interface definitions and the `allowed_paths` for each spec (the program-design pass practitioners call "criminally underemphasized"). They choose **approve subset**: the comment ends with `approve: spec_smart-retries, spec_at-risk-admin, spec_suspension-timing` and the label `factory:approve-subset` goes on; `spec_dunning-emails` is not built in this run (approve-subset never sends a spec back; the selection only has to include the prerequisites of what it approves). The copy change follows as its own small run, and Priya approves that from her phone because only wording changed and the diff is tiny.

### 6. Implementation, review, judging (nobody, hours)
Specs are implemented in dependency waves: `spec_smart-retries` first, then the three that depend on it in parallel on their own branches. The run board shows each work item's state. The timeline shows: implementer (2 iterations with the platform grader), orchestrator verification (34 examples, 0 failures), two reviewer passes merged into 4 findings (1 blocking, 1 major, 1 minor, 1 nit), implementer response (3 fixed, 1 refuted), a fresh reviewer round 2 (3 fixed confirmed, refutation accepted, 0 new), merge judge: `ready`.

If a human is curious or worried, every entry links to the session in the Anthropic Console, where the whole trace is visible. The operator can also attach a terminal.

### 7. Gate 3: the change request (engineer, 20 to 40 minutes)
GitHub notifies the code owner of pull request #418, titled `Enable Stripe Smart Retries and record attempts` (on GitLab, a merge request; the forge is configuration). The description is written for an engineer who knows the app and nothing about the factory, and fits on one screen (at most 60 lines; [post-mortem 2026-10-11](postmortems/2026-10-11-evergreen-frontier-models-pr.md) §4). The orchestrator renders it from `templates/cr_template.md` and refuses one with factory ids, factory vocabulary, costs, first-person text or links to the owner's machine:

1. **What changed and why**, in two to four plain sentences (the merge judge's `pr_summary`).
2. **Needs your decision**: none in this case. When something does (an implementer's `accepted_risk`, a product behaviour the spec left open), it is one sentence with the `file:line` it concerns, product-affecting items first. This is the one place the human must decide rather than verify.
3. **See it**: for a UI change, up to three screenshots and one short demo from a passing browser check.
4. **How it was tested**: one paragraph counted from the orchestrator's own run (which commands passed, how many tests, which browser checks at which viewports) and what the tests cover, plus the commands to run locally. It never says "all tests pass" as proof.
5. **Size**: the changed lines.
6. **Start reading at**: `app/models/payment_attempt.rb` (the idempotent insert), then the two handler branches, then the migration.
7. **One link to the full run record** in the owner's Factory Console: the acceptance criteria with located evidence, what was contested and how it settled (the concurrency bug the reviewer found, fixed and re-probed 20/20; the nit refuted on project convention), the exact verification and checks, every screenshot and recording, and every session with its cost.

The engineer reads the code. This is real review: HumanLayer's conclusion after five months of plan-only review was "read the dang code". The factory's job is to make this review take 20 minutes instead of two hours, by having removed the bugs a machine can find and by telling the reviewer exactly where to look and what needs their call. The engineer approves and merges. The change-request watcher (`factory watch <run_id>`, or **Watch** on the run page in the Console) polls the pull request through the forge, records `cr.merged` with the merge commit, moves the item to merged, and starts the dev verification below; the Linear issue moves on. Nobody edits an event by hand. A pull request closed without merging is recorded as `cr.closed` and closes the item.

If the engineer wants a change, they comment on the pull request as usual. The operator (or an automation on a label) can send the comments back as a new implementer round; the engineer does not have to take the branch over.

### 8. After the merge: the dev verification result (engineer, 0 to 10 minutes)
Nothing for the engineer to do when it works. The platform's pipeline deploys `main` to dev as it would for any merge; the watcher that saw the merge finds the workflow run for the merge commit and the run board shows the work item as **Deploying to dev** with the pipeline link, then **Verified in dev** once the watcher has seen the pipeline succeed, read the dev URL from the deployment record, and re-run the spec's three end-to-end checks and the team's smoke suite against it from its own checkout. The timeline entry reads: *deploy.succeeded (pipeline #4812, 6m 40s) · e2e on https://dev.ledgerline.internal: 3 of 3 passed · smoke: 2 of 2*. The Linear issue moves to Done. Promotion beyond dev is the team's normal release process; the factory does not touch it.

When it does not work, the engineer gets a **Deploy failure** gate instead, with the failed job's log tail (or the failing e2e command and its output) and three buttons: **Request changes** sends the item back to an implementer on a fresh branch with the engineer's comment as the first finding, so a follow-up change request appears without anyone taking the branch over; **Approve** says the pipeline failure was the platform's (a flaky runner) and the next pipeline should be watched; **Reject** closes the item. In the Ledgerline run, the migration needed an extension the preview database had but dev did not; the engineer wrote one line, the follow-up change request arrived 40 minutes later with the additive fix and the dev checks green.

The watcher is a plain process (`factory watch <run_id>`; the Console starts the same command and shows its heartbeat). It can be stopped and started at any time: every poll re-reads the run's event log and appends only what the log does not hold, so a merge, a deploy, or a gate is never recorded twice, a gate a dead watcher left open is picked up rather than re-opened, and a decision recorded while no watcher was running is applied by the next one. It stops by itself when no item is left waiting on a pull request or a deploy, or after its deadline (72 hours by default).

Two months later Priya fills in `measured_at` and `measured_value` on the outcome's success metrics from the Metabase dashboard. No agent does that; it is the one field that closes the loop from code to the business outcome.

### 9. Escalations (engineer or owner, as needed)
Escalations open a gate with the reason and the relevant artifacts: a `blocked.md` from an implementer who found two criteria in conflict, a merge judge that saw the same finding survive two rounds, a spec judge that thinks the decomposition itself is wrong, or a session that hit its budget cap. The human can send the run back to a specific stage with comments, raise a budget, or close the work item. The **run budget** is its own gate: when the next agent session would take the run past `constraints.budget_usd`, the run parks with nothing running and a `budget` gate shows what was spent, what the next step could cost, and the smallest budget that lets it continue; the owner raises the budget (and the run resumes from exactly where it stopped) or closes the run. Escalations are the factory telling you it is out of its depth, which it should do early and specifically.

## What the Factory Console shows

**Runs board**: columns = run stages; cards show outcome title, owner, days in stage, cost so far against budget (the outcome's `constraints.budget_usd`, enforced), and a red marker when a gate or question is waiting on a person. Sorted by waiting-on-human first.

**Run detail**: left, the event timeline (derived from the run's event log) with filters by stage and actor; right, the work-item DAG with state colours (merged items show a dev marker: deploying, verified in dev, or failed); top, cost per stage and the budget; every artifact and session is one click away, as are the preview and dev URLs. Once pull requests exist, a **Pull requests and the dev deploy** card lists what is still open, starts the watcher, and says when it last checked.

**Approval inbox**: every open gate and blocking question across runs, oldest first, with the summary and the artifact links. Decisions require the artifact hashes shown, which the UI supplies from the gate record it read.

**Agent versions** (not yet built): which agent version is live, the last three eval results per agent (from `evals/results/`), and a pin/unpin control that sets `FACTORY_AGENT_<ROLE>_ID` for canary runs.

## UX principles, each traced to evidence

1. **Review design, then contracts, then code. Never skip code.** Plan-only review failed in production ([practitioners.md §1c](research/practitioners.md)).
2. **Put the judge's critique next to the artifact.** Humans agree most with moderate, focused explanations ([academic-2026.md §6](research/academic-2026.md), Gao et al.).
3. **Show what changed since the last review**, not the whole thing again. Pages and the Console should grow a diff view between revisions.
4. **Interrupt only for blocking questions; batch the rest at gates.** Oversight capacity is finite ([academic-2026.md §6](research/academic-2026.md), Turan).
5. **Bind every approval to what was shown.** Hash-bound decisions ([practitioners.md §13](research/practitioners.md), Loopjacking).
6. **Never present a passing test as proof.** List which tests cover which criteria ([academic-2026.md §6](research/academic-2026.md), Dhanorkar et al.).
7. **Make escalation specific and early.** A `blocked.md` with a concrete conflict beats an implementer that guesses.
8. **Keep design throughput ahead of implementation.** Gates 1 and 2 are cheap and human; implementation is expensive and slow. The board should rarely show implementations waiting on specs ([practitioners.md §7](research/practitioners.md)).
9. **Every artifact links to its trace.** Trust comes from being able to look, not from being told.
10. **Show the running thing, not just the diff.** Preview URL at Gate 3 and the dev result after merge, with the exact end-to-end commands and exit codes. Agents declare victory without end-to-end verification unless the harness demands it ([practitioners.md §3](research/practitioners.md)); the human should see what was actually exercised.

## What this UX deliberately does not do (yet)

- **Auto-approve anything.** No gate is skipped by policy in v1. Tiered review (a bespoke review model sufficing for low-risk paths) is what the Codex team does ([practitioners.md §4](research/practitioners.md)); it belongs after six months of gate-decision telemetry shows where human approval adds nothing.
- **Let humans chat with agents mid-stage.** Steering happens through gate comments and `ask_human` answers, which are recorded and replayable. Ad-hoc chat would make runs unreproducible and break the evals.
- **Merge.** Ever.
- **Deploy, promote, or touch infrastructure.** The platform's pipeline deploys; the factory watches and reports. Promotion beyond dev is the team's release process ([05-platform-and-deployment.md](05-platform-and-deployment.md)).
