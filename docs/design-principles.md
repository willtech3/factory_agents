# Design principles

Each principle names where it is enforced in this repository and the evidence it rests on. If the evidence changes, the principle should too. Sources: [practitioners.md](research/practitioners.md) (P) and [academic-2026.md](research/academic-2026.md) (A).

| # | Principle | Enforced in | Evidence |
|---|---|---|---|
| 1 | Humans review design, then contracts, then code. Code review is not skipped. | Gates 1, 2, 3 in `01-pipeline.md`; the factory never merges | P §1c, §1f (HumanLayer reversal); P §9 (Willison); A §6 (Monperrus as counterpoint) |
| 2 | Authors never grade themselves; judges run in separate sessions, on a different model tier where possible, and never see the author's reasoning or self-assessment. | `agents/spec_judge.md`, `agents/reviewer.md`, `agents/merge_judge.md`; stage modules upload artifacts only | P §3 (Anthropic harness); A §1 (Yang et al. self-preference) |
| 3 | A verifier sits between every pair of stages and nobody can override it. | `checks.py`; `merge_judge` prompt; `implement.py` forces `another_round` on failure | A §4 (Kim et al.; Xie et al. cascades) |
| 4 | Critique is execution-grounded: findings carry reproductions and refutation conditions; refutations carry evidence; the orchestrator runs the tests. | `review_findings.schema.json`, `review_response.schema.json`, `LocalVerifier` | A §2 (Parmar et al.; Rakasi et al.); A §1 (Jin and Chen) |
| 5 | One fresh-context review pass per round; the reviewer is told to disagree; a critic (merge judge) audits the review. | `implement.py` (new session per round, `reviewer_passes`), `reviewer.md` mandate, `merge_judge.md` | A §2 (Song; Qiu and Gill); P §2 (Cognition clean-context reviewer) |
| 6 | Keep an acceptance oracle the implementer cannot see. | Hidden suites in `evals/`; orchestrator-run tests; rubrics not mounted into implementer sessions | A §2 (SEAL); A §4 (Destefanis and Aste) |
| 7 | Resolve ambiguity before code: blocking questions interrupt, non-blocking ones default and surface at the gate. | `ask_human` tool; `open_questions` with `default_if_unanswered` in both schemas | A §3 (Orchid; ClarifyCodeBench); P §11 (Osmani) |
| 8 | Specs are contracts with traceability: criteria map to tests, interfaces are typed, scope is bounded. | `spec.schema.json`; rubric S2, S4, S5, S7; `ac_test_mapping` and `diff_scope` checks | A §3 (VeriSpecGen; CodeSpec; ReqGenX); P §1f (program design pass) |
| 9 | Decompose along the dependency graph into vertical slices; implement in isolated branches; integrate via tests. | `decomposition.schema.json` DAG; rubric D2, D3; `implementer.md` slices; per-spec branches | A §4 (CAID); P §1f (vertical slices) |
| 10 | Coordinate through artifacts, not chat; a dedicated coordinator across stages adds little. | Orchestrator design (ADR-0001); every agent reads and writes files | A §4 (Destefanis and Aste); P §2 (Cognition manager pattern) |
| 11 | Research is goal-blind. | `codebase_researcher.md`; lead prompts instruct not to reveal intent | P §1c (Questions before Research) |
| 12 | Existing tests are protected; test weakening is a deterministic failure. | `protected_tests`, `test_weakening` checks; rubric I4 | P §3 (Anthropic), §6 (Beck on test cheating) |
| 13 | Approvals bind to what was shown. | `gates.py` artifact hashes; `StaleDecision` | P §13 (Loopjacking) |
| 14 | Interrupt humans rarely and specifically; escalate early when out of depth. | Blocking-only questions; escalation gates with reasons; `blocked.md` | A §6 (Turan; Lin and Zhang); P §10 (Hashimoto) |
| 15 | Give human reviewers focused summaries with locations; never present passing tests as proof. | `mr_template.md`; `merge_judge.md` summary rules | A §6 (Gao et al.; Dhanorkar et al.); A §1 (Counsel: trust location over rationale) |
| 16 | Judges score per criterion with anchors, normalised inputs, repeated votes, and kappa against humans. | `rubrics/`; `evals/graders/stochastic.py`; `specify.py` uploads JSON only | A §1 (Norman et al.; Yagubyan; Soumik; RuVerBench) |
| 17 | Instruction budgets: short prompts that point at skills; every rule traces to a failure. | `agents/README.md` conventions | P §1c, §1d (HumanLayer), §10 (Hashimoto), §11 (Osmani) |
| 18 | Passing tests are necessary, not sufficient: track erosion and scope creep. | Rubric I6; `factory-bench` erosion metric | P §1f (SlopCodeBench); A §5 (Orlanski et al.); P §8 (Ronacher) |
| 19 | Keep design throughput ahead of implementation; merge serially through human review. | Cheap gates, expensive implementation; `parallel_implementations`; Gate 3 in GitLab | P §7 (Yegge); P §2 |
| 20 | Expect to re-tune per model release; version everything and compare on frozen items. | Versioned agents; `evals/run_eval.py` pins versions; L2 before model changes | P §7, §4; A §7 (Patel et al. predictive validity) |
