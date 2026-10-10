# Evaluation strategy

The detailed plan, datasets, and graders live in [`evals/`](../evals/README.md). This page is the reasoning.

## The problem with evaluating a pipeline

A factory run is six stochastic stages, three human decisions, and a codebase. An end-to-end pass rate tells you that something is wrong, not what. Benchmarks that collapse model, harness, and environment into one score have been criticised for exactly this ([academic-2026.md §5](research/academic-2026.md), Gorinova et al.), and most "model failures" in coding agents originate in the harness (Jarmak). So the plan evaluates each stage in isolation with frozen upstream inputs, and the pipeline end to end only to catch what isolation misses.

## Four levels

**L0 Deterministic.** Everything in `orchestrator/factory/checks.py`: schema validity, DAG acyclicity, metric coverage, spec traceability, diff scope, protected tests, test weakening, criterion-to-test mapping, command exit codes. These run in CI on every change to this repo and inside every run. They are exact, free, and the same code in both places, so an eval that passes them is measuring what production enforces.

**L1 Stage-isolated.** Each agent is run on a labelled dataset with its upstream inputs frozen, N times, and graded. This is where agent prompt and model changes are decided. The datasets:
- `outcomes-golden` for the decomposer (criteria rubric, not reference similarity, per DELTA);
- `spec-from-item` for the spec writer (rubric scores with human calibration);
- `spec-defects` for the spec judge (planted defects, paired with clean parents);
- `impl-from-spec` for the implementer (hidden acceptance suites the implementer never sees);
- `review-mutations` for the reviewer and merge judge (paired clean/faulty implementations; a human label of who was right in the exchange).

**L2 End-to-end.** `factory-bench`: ten outcomes against three private synthetic repositories with hidden acceptance suites and scripted human interventions so runs are unattended and reproducible. Measures outcome success with credible intervals, partial credit, interventions needed, cost, wall time, and code-quality erosion. Runs weekly and before any model change.

**L3 Online.** Production telemetry: gate decisions and time-to-decide, edit distance between agent artifact and approved artifact, rounds to convergence, change-request approval and revert rates, cost per merged MR, escalation reasons. This is the only level that measures the thing that matters (did humans accept the work) and the cheapest, so instrument it from day one.

## Deterministic versus stochastic measurement

**Deterministic** metrics are computed from artifacts and git: the L0 checks, counts (work items, criteria, findings, rounds), coverage (criteria with tests), scope violations, cost, time. They are the backbone because they are exact and repeatable.

**Stochastic** metrics need a judgement: is this decomposition well-shaped, is this spec unambiguous, is this finding real. Two sources of stochasticity compound: the agent under test and the judge measuring it. The plan handles each:

*Agent variance:* at least 5 runs per item per configuration; report mean with bootstrap CI; report pass^k (all k runs succeed) as the reliability metric, because capability and reliability rankings diverge on long tasks ([academic-2026.md §7](research/academic-2026.md), Khanal et al.); compare configurations on identical items and declare a difference only when credible intervals do not overlap (Hariri et al.).

*Judge variance and bias:* per-criterion scoring, never holistic; 11 votes per criterion with majority and entropy reported (Yagubyan found 11 trials needed to recover a stable verdict); position swap on every pairwise comparison; formatting normalised before judging (style bias dominates, Soumik); and **Cohen's kappa against human labels on a 10% sample every run**, because raw agreement overstates discrimination by 33 to 41 points (Norman et al.). A judge whose kappa drops is retuned before its scores are used for anything.

**Paired discrimination** for the spec judge and reviewer: flag rate on the faulty item minus flag rate on its clean parent, per defect code. A reviewer that flags everything scores zero. This single metric distinguishes a useful adversarial reviewer from a noisy one (Mohammadi).

## Oracles

The implementer's own tests are never the oracle. Generated tests agree with independent implementations only 28 to 50% of the time and mostly encode implemented behaviour, not intended behaviour ([academic-2026.md §5](research/academic-2026.md), Liang et al.; Bodicoat et al.). Oracles are: hidden acceptance suites written from the spec by humans (or by a model from the spec alone, then human-checked), kept outside every agent session; and for the reviewer, planted defects with known locations.

## Contamination and gaming

- Public benchmarks are used only as a design reference (FeatureBench's shape) because SWE-bench Verified shows memorisation effects and a large share of audited failures were test flaws.
- `factory-bench` repositories are synthetic, private, and rotated quarterly; outcome statements are rewritten at rotation.
- Hidden suites never leave the harness; agent probe scripts are inspected for attempts to find them, since agents in sealed studies sought grading material in roughly 80% of runs (Destefanis and Aste).
- Eval items never appear in skills, memory stores, or prompts; a membership-inference spot check runs when a new model is introduced.

## What a change to the factory must show before it ships

1. L0 green.
2. L1 for the affected stage: the headline metric's credible interval does not overlap the current version's, on the `test` split, with the judge's kappa reported.
3. For reviewer or judge changes: paired discrimination not lower on any defect code where the previous version was above zero.
4. For model changes: L2 `factory-bench` run, with stage attribution of failures.
5. A note in the agent file's commit message linking the results directory.

## Open questions the evals should answer first

- Does the second reviewer pass add detection or only cost? (Run `review-mutations` with `reviewer_passes` 1 versus 2.)
- Does the platform outcome grader's inner loop improve the spec judge's first-round pass rate enough to justify its cost? (Run `spec-from-item` with `max_iterations` 1 versus 4.)
- Is Sonnet 5.5 sufficient for the spec writer when Opus 5.5 is the judge, or do specs improve enough on Opus to pay for themselves in fewer judge rounds?
- How often does Gate 2 edit a spec the judge passed, and what did the judge miss? (L3, from day one.)
