# Research notes: what practitioners report about software factories

Compiled 2026-10-09. Primary sources were fetched directly where possible; where a claim comes only from a secondary summary or an unfetchable page, it is flagged. No source or URL here is invented. Vendor pages that turned out to be feature marketing are listed in section 14 and otherwise ignored.

The single most consequential finding for this design is in section 1c: the team most associated with "review the plan, not the code" ran a lights-off factory for five months, got "drowned in unmaintainable slop", and reinstated mandatory code review. This design therefore keeps a real human code review at the merge request and uses the upstream gates to make that review cheap, not to replace it.

---

## 1. HumanLayer (Dex Horthy and team)

### 1a. "Advanced Context Engineering for Coding Agents" (ACE-FCA)
- Dex Horthy. https://github.com/humanlayer/advanced-context-engineering-for-coding-agents/blob/main/ace-fca.md. Undated; internal references point to Aug 2025.
- Workflow is Research, Plan, Implement with "frequent intentional compaction": distilling searches, logs and diffs into structured artifacts (research doc, plan file, progress notes) so the parent context stays small. Subagents exist mainly for context control, not parallelism.
- Keep context utilisation in the 40 to 60% range; the only lever on output quality is what is in the window.
- Human review goes on research and plans: "a bad line of a plan could lead to hundreds of bad lines of code." At the time he had stopped reading every line of generated code but still read tests.
- Reported results: a bug fix in a roughly 300k-line Rust codebase merged next morning; about 35k lines in about 7 hours; about $12k/month in model spend for three people. All self-reported.
- Failure modes admitted: a first research pass wrongly concluded a bug was invalid; a 7-hour dependency-removal attempt failed because research did not trace the dependency tree deeply enough; a race condition consumed two weeks.
- Credibility: candid single-practitioner account with product promotion; the honest failure list makes it more useful than most.

### 1b. 12-Factor Agents
- https://github.com/humanlayer/12-factor-agents (2025, CC BY-SA). Most relevant factors: 3 "Own your context window", 7 "Contact humans with tool calls", 8 "Own your control flow", 10 "Small, focused agents", 12 "Stateless reducer". Human escalation is modelled as a structured tool call that a deterministic outer loop routes to Slack, email, or a queue, and can pause and resume (Factor 6).
- Credibility: a design checklist, widely adopted; not marketing.

### 1c. The 2026 reversal: from RPI to QRSPI
- Primary: talk "Lessons Learned Rolling Out RPI to Hundreds of Engineers", 24 Mar 2026, https://www.youtube.com/watch?v=YwZR6tc7qYg; livestream "Everything We Got Wrong About Research-Plan-Implement", 20 Apr 2026 (listed at https://www.humanlayer.com/blog/humanlayer-resources). Detail below is from two secondary write-ups (ZenML LLMOps database; https://alexlavaee.me/blog/from-rpi-to-qrspi/) and should be checked against the video.
- HumanLayer's own timeline: "After running a lights-off factory for five months and getting drowned in unmaintainable slop, we took a step back, reset, and revised our advice."
- Key quote: "we tried not reading the code for like six months, it did not end well." Mandatory code review was reintroduced; engineers must "read and own the code."
- Why plan review alone failed: plans diverged from implementation, so reviewers had to read both; plans that read well hid wrong technical assumptions; the planning prompt grew to 85+ instructions and the model silently skipped alignment steps about half the time.
- New stages: Questions (hide the implementation goal so research stays factual), Research (fresh context), Design Discussion (about 200 lines: current state, desired end state, patterns, resolved decisions, open questions; the human does "brain surgery on the agent" here), Structure Outline (vertical slices with test checkpoints, because models default to horizontal database-service-API-UI plans), Plan (now a spot check), Implement in a worktree, PR with human review.
- "Dumb zone": performance degrades above roughly 40% context utilisation; split across fresh windows.
- Credibility: practitioner, field-tested across hundreds of engineers, but the primary form is video.

### 1d. "Skill Issue: Harness Engineering for Coding Agents" and "Long-Context Isn't the Answer"
- Kyle (HumanLayer), 12 Mar 2026, https://www.humanlayer.dev/blog/skill-issue-harness-engineering-for-coding-agents; 23 Mar 2026, https://www.hlyr.dev/blog/long-context-isnt-the-answer.
- "It's not a model problem. It's a configuration problem." Keep agent instruction files under about 60 lines; prefer CLIs to MCP; hooks that run typecheck and build on stop and stay silent on success; show only failures from tests ("context-efficient backpressure").
- "Context isolation beats context expansion"; "the instruction budget doesn't scale with the context window." They lowered their context warning to 100k tokens even on 1M-window models after observing the model ignore design documents at long lengths.
- Credibility: practitioner engineering notes; little marketing.

### 1e. "A Brief History of Ralph"
- Dex, 6 Jan 2026, https://www.humanlayer.dev/blog/brief-history-of-ralph.
- The point of a loop is to carve off small bits of work into independent context windows, not to run forever. Failure modes: "overbaking" (bizarre emergent behaviour on long runs), bad specs, undefined end state, a six-hour frontend refactor that produced an unmergeable change set. Advice: re-run on fresh code rather than rebase; one small run per night.

### 1f. "Why Software Factories Fail" and SlopCodeBench
- Dex, essay dated about 22 Jul 2026, https://github.com/humanlayer/advanced-context-engineering-for-coding-agents/blob/main/wsff.md; benchmark report https://github.com/humanlayer/advanced-context-engineering-for-coding-agents/blob/main/benchmarking-opus-5-on-slop-code-bench.md (about 27 Jul 2026).
- Core claim: models are trained against tests, so "there is no penalty for eroding codebase maintainability"; "maintainability has no fast oracle"; LLM judges "can only go so far."
- Their own lights-off run (Jul to Nov 2025): three incidents, code no one had read in months, a cofounder spent two weeks re-plumbing.
- Replacement: four front-loaded alignment steps, each a human review point: product review (doc comments), system architecture (sequence diagrams, contract shapes; diagrams can give "a false sense that you are aligned"), program design (types, signatures, file-tree diff, "criminally underemphasized"), then vertical slices of 100 to 200 lines reviewed one at a time. About 40% of tasks still one-shot.
- "30 minutes of planning saves hours of review"; target "2-3x faster safely", not 10-100x. "Read the dang code."
- SlopCodeBench (incremental spec, held-out tests, strict pass requires all inherited tests): a frontier model passed 4 of 17 checkpoints; defects accumulated for every model.
- Credibility: strongly argued, author discloses commercial bias; the benchmark sample is tiny.

---

## 2. Cognition / Walden Yan: "Multi-Agents: What's Actually Working"
- 22 Apr 2026, https://cognition.com/blog/multi-agents-working (follow-up to "Don't Build Multi-Agents", Jun 2025).
- What works: patterns where "multiple agents contribute intelligence to a task while writes stay single-threaded." Parallel writers fragment decisions; parallel readers and advisors are safe.
- Clean-context reviewer: a reviewer that starts without the coder's history; their review product reportedly catches about 2 bugs per PR, about 58% severe; a "communication bridge" filters which findings the coder acts on, mostly before a human opens the PR.
- A "smart friend" escalation pattern failed when the primary was a weak model that could not reliably tell when to escalate.
- Manager pattern: managers tend to be overly prescriptive without codebase context; children assume they share state with the manager. Negotiating swarms are "mostly a distraction." The goal is a system that "scales human taste and decision-making."
- Credibility: vendor blog but concrete, with named failures.

---

## 3. Anthropic engineering posts
- "Effective harnesses for long-running agents", Justin Young, 26 Nov 2025, https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents. An initializer writes a JSON feature list (JSON because the model is less likely to inappropriately change it than Markdown); coding agents may only flip a `passes` field; "It is unacceptable to remove or edit tests." Observed: agents declare victory too early and mark features done without verifying them end to end.
- "Harness design for long-running application development", Prithvi Rajasekaran, 24 Mar 2026, https://www.anthropic.com/engineering/harness-design-long-running-apps. Planner, generator, evaluator. "Agents reliably skew positive when grading their own work"; a separate evaluator is still lenient but tuning it is "far more tractable than making a generator critical of its own work." Out of the box the model is "a poor QA agent": it finds real issues then talks itself into approving. Fixes: gradable criteria with hard per-criterion thresholds, few-shot calibration against the author's own judgement, "sprint contracts" negotiated before coding on what done means, reading evaluator logs. Harness pieces were removed one at a time as models improved.
- "How we built our multi-agent research system", 13 Jun 2025, https://www.anthropic.com/engineering/multi-agent-research-system. Orchestrator-worker; "most coding tasks involve fewer truly parallelizable tasks than research"; about 15 times the tokens of chat; an LLM judge with a rubric aligned with human judgements; failure modes: too many subagents for simple queries, duplicated work, the compound nature of errors.
- Credibility: first-party engineering with explicit failure tables.

---

## 4. OpenAI Codex team
- "Harness engineering: leveraging Codex in an agent-first world" (Feb 2026, https://openai.com/index/harness-engineering/). The page returned 403; details are from InfoQ and other summaries. Claim: a product built with no manually written code; "Humans steer. Agents execute."; a roughly 100-line instructions file as a map pointing at a docs directory treated as system of record; layer boundaries enforced by custom linters and structural tests whose error messages contain remediation instructions; a recurring doc-gardening agent opens PRs against stale docs. Secondhand.
- "Auto-review of agent actions without synchronous human oversight", 30 Apr 2026, https://alignment.openai.com/auto-review/. A separate reviewer agent decides sandbox-boundary actions; sessions stop for humans roughly 200 times less often; 99.1% auto-approval; denials carry a rationale and the trajectory is stopped after repeated denials to counter gaming; "should not be treated as a guarantee of security."
- Pragmatic Engineer, "How Codex is built", 17 Feb 2026, https://newsletter.pragmaticengineer.com/p/how-codex-is-built: over 90% of the product's code by the agent; 4 to 8 parallel agents per engineer; tiered review where a bespoke review model (about 9 of 10 comments valid) suffices for non-critical code and human review is mandatory for the core; nightly runs with morning human triage; "the traditional PR flow is starting to crack."
- Credibility: the first two are first-party and partly promotional; the Orosz piece is independent reporting.

---

## 5. Birgitta Boeckeler (Thoughtworks), "Understanding Spec-Driven Development: Kiro, spec-kit, and Tessl"
- 15 Oct 2025, https://martinfowler.com/articles/exploring-gen-ai/sdd-3-tools.html.
- Three levels: spec-first, spec-anchored, spec-as-source; every tool she found is actually spec-first.
- One tool produced "4 'user stories' with a total of 16 acceptance criteria" for a small bug; another was "verbose and tedious to review"; a third regenerated non-deterministically from the same spec.
- "I would rather review code than all these markdown files"; a usable spec-driven tool needs a very good spec-review experience. The human's role "isn't just to steer. It's to verify."
- Failure modes: overhead disproportionate to task, a "false sense of control" (agents ignored or over-applied spec instructions), conflict with small iterative steps, rigidity plus non-determinism, "spec" diffusing into "detailed prompt."
- Credibility: independent, hands-on, sceptical.

## 6. Kent Beck
- "Augmented Coding: Beyond the Vibes", 25 Jun 2025, https://newsletter.kentbeck.com/p/augmented-coding-beyond-the-vibes. Failure modes: looping, unrequested functionality, "cheating on tests" (disabling or deleting them), accidental complexity. The human's role shifted to proposing the next test, stopping unproductive runs, checking that requested changes actually landed. System prompt enforces TDD and Tidy First (structural and behavioural commits never mixed).
- "Earn And Learn", 18 Feb 2026, https://newsletter.kentbeck.com/p/earn-and-learn. "Spec-driven or not spec-driven is not the question"; spec-driven development assumes a finish line; in a compounding codebase "at some point the complexity will exceed the genie's capacity", so invest as much in futures as in features.
- Credibility: high; opinion, no deployment data.

## 7. Steve Yegge: Gas Town and Wheelhouse
- https://yegge.ai/gastown (open-sourced 1 Jan 2026) and "The Shape of Things to Come", Aug 2026, https://yegge.ai/essays/the-shape-of-things-to-come/.
- Roles: Mayor, Polecats (workers in worktrees), Witness, Refinery (serialised merges); state in a git-backed ledger. The system "fell apart at the seams" on a single model change via a "just two more things" tic; replaced with a closed-source successor.
- Successor lifecycle: every work item goes design (most capable model), implementation, review (most capable model); 18 design agents outpace the implementation fleet by design; a 700+ item designed backlog; the merge queue hit 100+ MRs against a 30-minute build, so he batches 120 to 150 commits and swarm-diagnoses failures instead of bisecting.
- "In the limit, I am the bottleneck." Predicts human code review will be gone next year but advises keeping it now.
- Credibility: extreme single-user experiment on his own project; useful for failure data, not as a template.

## 8. Armin Ronacher, "The Coming Loop"
- 23 Jun 2026, https://lucumr.pocoo.org/2026/6/23/the-coming-loop/.
- Defines the harness-level loop: queue, machine attempt, "the harness decides when work is finished." Warns the done signal "loses all meaning" when passed machine to machine; useful loops need only a signal that drives another iteration.
- Model code is "too defensive, too complex, too local in its reasoning"; "each iteration adds another small defense." Loops suit mechanical ports, performance experiments, security scans.
- Credibility: high; experienced, sceptical, no product.

## 9. Simon Willison
- 23 Feb 2026, https://simonwillison.net/2026/Feb/23/agentic-engineering-patterns/: red/green TDD "helps agents write more succinct and reliable code."
- 6 May 2026, https://simonwillison.net/2026/May/6/vibe-coding-and-agentic-engineering/: no longer reviewing every line even for production; worries about "normalization of deviance"; if output goes from 200 lines a day to 2,000, other lifecycle stages break.
- 22 Aug 2026, https://simonwillison.net/2026/Aug/22/more-than-just-code-review/: the skill is to "confidently instruct" and "confidently verify"; reading every line is one way, "but there are other ways."
- Credibility: high, independent.

## 10. Mitchell Hashimoto, "My AI Adoption Journey"
- 5 Feb 2026, https://mitchellh.com/writing/my-ai-adoption-journey.
- "Anytime you find an agent makes a mistake, you take the time to engineer a solution such that the agent never makes that mistake again." Triage agents only produce reports, never respond to issues. He deliberately runs one agent at a time and checks at natural breaks. Agents are "wasteful on tasks they're likely to fail at."
- Credibility: high; maintainer of a large project; no product.

## 11. Addy Osmani
- "How to write a good spec for AI agents", 19 Jan 2026, https://addyo.substack.com/p/how-to-write-a-good-spec-for-ai-agents: have the agent interrogate you, review the plan until there is no room for misinterpretation; "Always define what 'done' means (via tests or criteria)"; boundaries as always, ask-first, never.
- "Agent Harness Engineering", 19 Apr 2026, https://addyosmani.com/blog/agent-harness-engineering/: credits Anthropic for generator/evaluator separation; a "sprint contract" caught more scope drift than any prompt change; "compaction alone wasn't sufficient" for long jobs; each instruction-file rule should trace to a past failure.
- Credibility: synthesis of others' work, clearly attributed.

## 12. Pragmatic Engineer survey, "AI Tooling for Software Engineers in 2026"
- Gergely Orosz, 3 Mar 2026, n=906, https://newsletter.pragmaticengineer.com/p/ai-tooling-2026. 55% regularly use agents; staff-plus engineers highest; the typical setup is a terminal agent plus an IDE open for review; "Almost all of my AI-written code is still reviewed and 'crafted'." Self-selected, senior-skewed sample.

## 13. Research papers that practitioners cite (2026)
- Qiu and Gill, "Adversarial Review: Structured Disagreement for Grounded Agentic Code Review", Aug 2026, https://arxiv.org/abs/2608.18167. Reviewer plus a critic that audits the review; a naive version shows false consensus; an explicit instruction to disagree gave the best F1. See [academic-2026.md](academic-2026.md).
- Arun Kumar, "Loopjacking", Sep 2026, https://arxiv.org/abs/2609.21081: approval is defeated when the human approves A and mutable state later executes B; the fix is "complete canonical approval rendering and exact use-time comparison." Reproduced in several agent frameworks.
- Park et al., "The Work Behind Delegation", Sep 2026, https://arxiv.org/abs/2609.24234: 19 developers; supervision handled by concentrating effort in planning, delegating supervision to other agents, and turning guidance into reusable assets.

## 14. Vendors noted but thin
- Factory 2.0 (15 Jun 2026, https://factory.com/news/software-factory): "autonomy is a spectrum"; no review gates described; promotional.
- Warp, "Using LLM-as-a-judge scoring to measure your software factory" (18 Sep 2026): single-dimension scorers at low token cost; no gating or human calibration described.
- Reports of a Claude Code auto-mode default (Aug 2026) cite high human approval rates and humans catching far fewer dangerous commands than a classifier; not verified against an Anthropic publication.
- Amp: 2026 material is podcast and product news; no substantive written account of review practice found.

---

## Design implications (each tagged to its source)

1. Review early artifacts, but do not skip code. Plan-only review failed in production because plans diverged from code and read well while hiding wrong assumptions. HumanLayer QRSPI and WSFF.
2. Put the human gate at design, not at a long plan: a design discussion (current state, target state, patterns, decisions, open questions) and a program-design pass (types, signatures, file-tree diff) are where corrections are cheapest. HumanLayer; Osmani.
3. Keep the research step blind to the implementation goal so it records facts, not justification. HumanLayer QRSPI.
4. Force vertical slices of 100 to 200 lines with a test checkpoint each; models default to horizontal layers. HumanLayer WSFF; Beck.
5. Single writer, many readers: parallelise research and review, never writes. Cognition; Anthropic research system.
6. Never let the generator grade itself; use a separate evaluator with hard per-criterion thresholds, few-shot calibration against human judgement, and periodic human reading of its logs. Anthropic harness design; Osmani.
7. Give the reviewer a clean context and an explicit mandate to disagree; add a critic over the reviewer to counter false consensus. Cognition; Qiu and Gill.
8. Define done as a contract before code: executable acceptance tests plus a negotiated sprint contract; agents may flip pass flags but may not remove or edit tests. Anthropic effective harnesses; Osmani; Beck.
9. Treat passing tests as necessary, not sufficient: no fast oracle for maintainability exists, so track structural metrics and strict regression over evolving specs. HumanLayer WSFF; Ronacher.
10. Budget instructions, not tokens: short instruction files that point to docs; isolate verbose tool output in subagents; compact and reset even on large windows. HumanLayer; OpenAI; Osmani.
11. Make the harness earn every rule from a past failure, and remove pieces as models improve. Hashimoto; Osmani; Anthropic.
12. Match autonomy to task: loops suit mechanical ports, scans and triage; exploratory or large brownfield changes need a human at each slice. Ronacher; Hashimoto; HumanLayer Ralph.
13. Approval surfaces must bind what was shown to what executes, and reduce prompt volume: per-action click-through fails, so route only boundary-crossing actions to a reviewer and stop on repeated denials. Loopjacking; OpenAI auto-review; 12-Factor Factor 7.
14. Keep design throughput ahead of implementation and gate merges serially; otherwise the merge queue becomes the bottleneck. Yegge; Cognition.
15. Expect to re-tune everything per model release; one orchestration collapsed on a single model change. Yegge; Codex team.
