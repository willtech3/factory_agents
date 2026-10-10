# Humans in the loop: what people see and do

The factory has three kinds of people: the **outcome owner** (product or business), **engineers** (code owners who review MRs and handle escalations), and a **factory operator** (whoever tunes agents and watches cost). This page walks through the run from each seat, using the Ledgerline example in `examples/ledgerline/`.

## Surfaces

| Surface | Who uses it | What for |
|---|---|---|
| **Factory Console** (internal web app, thin) | all | Runs board, run timeline, approval inbox, cost, links out |
| **GitLab design MR** | owner, engineers | Gate 1 and Gate 2 reviews with inline comments on `decomposition.md` and `specs/*.md` |
| **GitLab code MR** | engineers | Gate 3: ordinary code review and merge |
| **Slack / email** | owner, engineers | Notifications with deep links; blocking questions; optional approve button for low-risk gates |
| **Anthropic Console session viewer** | operator, curious engineers | Full trace of any session: tool calls, thinking progress, cost per thread. Every artifact's `produced_by.session_id` deep-links here. |
| **`ant beta:sessions connect`** | operator | Attach a terminal to a live session to watch, nudge, or answer a paused tool call |

The design choice that matters most: **reviews happen in GitLab, not in a bespoke UI.** The decomposition and specs are committed as files on a design branch and opened as a draft MR. Owners and engineers already know how to leave an inline comment on line 42 of a markdown file; that comment is exactly the feedback the agent needs, and GitLab already handles approvals, notifications, and audit. Boeckeler's complaint that spec-driven tools make you review "all these markdown files" with no good review experience is answered by using the best review experience the team already has ([practitioners.md §5](research/practitioners.md)).

## Walkthrough: the Ledgerline run

### 1. Intake (owner, 10 minutes)
Priya, Head of Product for Billing, opens the Console and clicks **New outcome**. The form asks for the statement in her own words, success metrics with baseline and target, constraints (`must_not`, budget, deadline), the target repository, and links. The form validates against `outcome.schema.json` and refuses vague metrics ("better retention") without a target. She submits; the run appears on the board in **Decomposing**.

### 2. A blocking question (owner, 2 minutes, async)
Twenty minutes in, Slack: *"The decomposer has a blocking question on run_7f3a: Should retries follow Stripe Smart Retries or our own schedule? Why it matters: own schedule adds a scheduler work item and a week. Default if unanswered: Stripe Smart Retries."* Two buttons with the options, or a text reply. Priya taps *Stripe Smart Retries*. The session resumes. The Q&A is appended to `outcome.clarifications` and shows on the run timeline.

Design rule: only **blocking** questions interrupt a human. Non-blocking ones take their default and are listed at the next gate. Oversight has a capacity; flooding reviewers reduces safety ([academic-2026.md §6](research/academic-2026.md), Turan).

### 3. Gate 1: decomposition review (owner, 15 minutes)
Slack: *"Decomposition ready for review: 4 work items, all 3 metrics covered, 2 open questions. Review in GitLab !412 or the Console."* The design MR shows `decomposition.md`:

- a one-paragraph *What you are approving*
- the work-item table (id, title, size, depends on, metrics)
- coverage per metric, out-of-scope list, open questions with defaults, assumptions, risks
- below the fold, the rubric scores the platform grader reached and the deterministic check results

Priya reads it in a couple of minutes. She leaves one inline comment on the suspension item: *"Suspension timing isn't here. If Stripe retries for 3 weeks, when do we actually suspend?"* and clicks **Request changes** in the Console (or applies the `factory:request-changes` label in GitLab). The orchestrator pulls her comment and any inline discussions into `feedback.md`, re-runs the decomposer with the prior decomposition attached, and bumps `revision` to 2. Twenty-five minutes later: revision 2 adds `wi-suspension-timing` with `human_judgement_required: true` and a note that she must set the maximum grace period. She approves.

Her approval is recorded with the sha256 of the exact files she saw. If the agent had rewritten the decomposition between her reading and her clicking, the decision would be rejected as stale and she would be asked to look again (Loopjacking defence; [practitioners.md §13](research/practitioners.md)).

### 4. Specification and the judge loop (nobody, ~90 minutes)
The owner sees the run move to **Specifying** and the timeline fill: four spec-writer sessions, then a spec-judge session (round 1: `revise`, 2 blocking issues about mismatched event names and an undefined scope), two targeted rewrites, a judge round 2: `pass`. Each judge verdict is a timeline entry with a one-line summary. No human is involved unless the loop escalates.

### 5. Gate 2: specification readiness (owner + one engineer, 30 minutes)
Slack to Priya and to the billing code owner: *"Specs ready: 4 specs passed the judge in 2 rounds. Judge summary: ... One judgement call for you: spec_dunning-emails includes the email copy as acceptance criteria, so you are approving customer-facing wording here."* The design MR now contains `specs/*.md`, `verdict.json`, and a rendered **goal coverage matrix** (metric → acceptance criteria). The Console's gate view puts the judge's `summary_for_human` at the top, then the matrix, then per-spec issues (the one `minor` about 3DS events).

Priya reads the email copy in `spec_dunning-emails.md` and changes two sentences via an inline comment. The engineer reads the interface definitions and the `allowed_paths` for each spec (the program-design pass practitioners call "criminally underemphasized"). They choose **Approve subset**: three specs approved, `spec_dunning-emails` back to the writer with the copy comments. It returns in 20 minutes; Priya approves it from the Slack button because only wording changed and the diff is tiny.

### 6. Implementation, review, judging (nobody, hours)
Specs are implemented in dependency waves: `spec_smart-retries` first, then the three that depend on it in parallel on their own branches. The run board shows each work item's state. The timeline shows: implementer (2 iterations with the platform grader), orchestrator verification (34 examples, 0 failures), two reviewer passes merged into 4 findings (1 blocking, 1 major, 1 minor, 1 nit), implementer response (3 fixed, 1 refuted), a fresh reviewer round 2 (3 fixed confirmed, refutation accepted, 0 new), merge judge: `ready`.

If a human is curious or worried, every entry links to the session in the Anthropic Console, where the whole trace is visible. The operator can also attach a terminal.

### 7. Gate 3: the merge request (engineer, 20 to 40 minutes)
GitLab notifies the code owner of MR !418, titled `spec_smart-retries: Enable Stripe Smart Retries and record attempts`. The description, rendered from `templates/mr_template.md`:

1. **Summary** by the merge judge: what changed in two sentences; what was contested and how it settled (the concurrency bug the reviewer found, fixed and re-probed 20/20; the nit refuted on project convention); what to read first.
2. **Decisions needed from you**: none in this case. (When an implementer marks `accepted_risk`, it appears here; that is the one place the human must decide rather than verify.)
3. **Acceptance criteria table** with status and the located test for each.
4. **What was contested**: one line per disputed finding with the chain reviewer → implementer → reviewer → judge.
5. **Verification**: the exact commands the orchestrator ran and their results, plus deterministic check results. The description never says "all tests pass" as proof; it lists which tests cover which criteria.
6. **Where to start reading**: `app/models/payment_attempt.rb` (the idempotent insert), then the two handler branches, then the migration.
7. **Provenance**: a table of every session, agent version, and cost, each linking to the Console trace.

The engineer reads the code, and can click the preview URL in the Verification section to poke the deployed branch themselves: the three preview end-to-end checks the spec declared are listed with the orchestrator's exit codes, next to the reviewer's own probes of the deployed instance. This is real review: HumanLayer's conclusion after five months of plan-only review was "read the dang code". The factory's job is to make this review take 20 minutes instead of two hours, by having removed the bugs a machine can find and by telling the reviewer exactly where to look and what was already contested. The engineer approves and merges; the webhook records `mr.merged`; the dependent specs become ready.

If the engineer wants a change, they comment on the MR as usual. The operator (or an automation on a label) can send the comments back as a new implementer round; the engineer does not have to take the branch over.

### 8. After the merge: the dev verification result (engineer, 0 to 10 minutes)
Nothing for the engineer to do when it works. The platform's pipeline deploys `main` to dev as it would for any merge; the run board shows the work item as **Deploying to dev** with the pipeline link, then **Deployed to dev** once the orchestrator has watched the pipeline succeed and re-run the spec's three end-to-end checks and the team's smoke suite against the dev URL. The timeline entry reads: *deploy.succeeded (pipeline #4812, 6m 40s) · e2e on https://dev.ledgerline.internal: 3 of 3 passed · smoke: 2 of 2*. Slack: *"spec_smart-retries is live in dev and verified."* Promotion beyond dev is the team's normal release process; the factory does not touch it.

When it does not work, the engineer gets a **Deploy failure** gate instead, with the failed job's log tail (or the failing e2e command and its output) and three buttons: **Request changes** sends the item back to an implementer on a fresh branch with the engineer's comment as the first finding, so a follow-up MR appears without anyone taking the branch over; **Approve** says the pipeline failure was the platform's (a flaky runner) and the next pipeline should be watched; **Reject** closes the item. In the Ledgerline run, the migration needed an extension the preview database had but dev did not; the engineer wrote one line, the follow-up MR arrived 40 minutes later with the additive fix and the dev checks green.

Two months later Priya fills in `measured_at` and `measured_value` on the outcome's success metrics from the Metabase dashboard. No agent does that; it is the one field that closes the loop from code to the business outcome.

### 9. Escalations (engineer or owner, as needed)
Escalations open a gate with the reason and the relevant artifacts: a `blocked.md` from an implementer who found two criteria in conflict, a merge judge that saw the same finding survive two rounds, a spec judge that thinks the decomposition itself is wrong, or a session that hit its budget cap. The human can send the run back to a specific stage with comments, raise a budget, or close the work item. Escalations are the factory telling you it is out of its depth, which it should do early and specifically.

## What the Factory Console shows

**Runs board**: columns = run stages; cards show outcome title, owner, days in stage, cost so far against budget, and a red marker when a gate or question is waiting on a person. Sorted by waiting-on-human first.

**Run detail**: left, the event timeline (derived from `events.jsonl`) with filters by stage and actor; right, the work-item DAG with state colours (merged items show a dev marker: deploying, verified in dev, or failed); top, cost per stage and the budget; every artifact and session is one click away, as are the preview and dev URLs.

**Approval inbox**: every open gate and blocking question across runs, oldest first, with the summary and the artifact links. One keyboard shortcut to open the design MR. Decisions require the artifact hashes shown, which the UI supplies.

**Agent versions**: which agent version is live, the last three eval results per agent (from `evals/results/`), and a pin/unpin control that sets `FACTORY_AGENT_<ROLE>_ID` for canary runs.

## UX principles, each traced to evidence

1. **Review design, then contracts, then code. Never skip code.** Plan-only review failed in production ([practitioners.md §1c](research/practitioners.md)).
2. **Put the judge's critique next to the artifact.** Humans agree most with moderate, focused explanations ([academic-2026.md §6](research/academic-2026.md), Gao et al.).
3. **Show what changed since the last review**, not the whole thing again. Revision diffs on the design MR do this for free.
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
