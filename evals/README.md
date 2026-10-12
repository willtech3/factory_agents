# Evaluating the factory

The factory is a pipeline of non-deterministic stages separated by deterministic checks and human gates. Evaluating it as one end-to-end number hides where it breaks (Gorinova et al., arXiv:2606.17799; Jarmak, arXiv:2608.13867), so the eval plan has four levels, each with its own dataset and its own cadence.

| Level | What is held fixed | What is measured | Cadence | Cost |
|---|---|---|---|---|
| L0 Deterministic | everything | schema validity, traceability, scope, protected tests, DAG, command results | every commit (CI) | free |
| L1 Stage-isolated | upstream artifacts (golden inputs) | one stage's output quality against labels | every agent prompt or model change | low to medium |
| L2 End-to-end | a synthetic repo and outcome set | outcome achieved, interventions, cost, time | weekly and before model upgrades | high |
| L3 Online | nothing | gate decisions, edit distance, change-request outcomes, reverts | continuous | free (telemetry) |

Stochastic measurements follow the methodology in `docs/06-evaluation.md`: at least 5 runs per item per configuration (11 or more judge votes where a judge is the measurement, per Yagubyan 2606.13685), Bayesian credible intervals rather than point pass@k (Hariri et al. 2510.04265), paired comparisons across agent versions on identical items, and Cohen's kappa against human labels for every LLM grader.

## Layout

```
evals/
  README.md                 this file
  datasets/                 one directory per dataset: README.md (provenance, labelling, splits) + items.jsonl (sample rows);
                            factory-bench holds a reference run (sample-app/) instead of items.jsonl
  graders/
    deterministic.py        wraps orchestrator/factory/checks.py for eval use
    stochastic.py           LLM judge with N votes, position swap, bootstrap CI, kappa
  run_eval.py               runs one stage over one dataset for one agent version; writes results/<timestamp>-.../
                            producing is a stub: --from-artifacts grades artifacts made elsewhere
                            --recorded: regrades or replays a dataset of recorded sessions (below)
  gate.py                   CI: every graded role's prompt and rubric has an eval receipt for its exact bytes
  receipts.json             per role: the prompt and rubric sha256 last cleared, and how (replayed or waived)
  results/                  gitignored
  recordings/               gitignored: datasets exported from real runs
```

## Recorded sessions and receipts ([ADR-0016](../docs/adr/0016-evals-from-recordings.md))

Every agent session of a real run records what it was asked and what it returned. `factory eval export <run_id> --out
evals/recordings/<name>` turns a run's sessions into a dataset, labelled with what the run made of each one (the
orchestrator's checks of an implementer's head, a reviewer's merged findings, a merge verdict, the owner's gate decision).

```
python evals/run_eval.py --recorded evals/recordings/live --agent merge_judge --regrade
python evals/run_eval.py --recorded evals/recordings/live --agent merge_judge --replay --runs 3 --receipt
```

`--regrade` grades the recorded outputs with today's checks. `--replay` asks the agent version the controller is
configured with the same sessions again and grades both the same way: the output validates on receipt and the judges keep
their decision rules. It regresses when its credible interval lies wholly below the recorded versions'.

A change to the prompt or rubric of a graded role (decomposer, spec writer, spec judge, implementer, reviewer, security
reviewer, merge judge: `ROLE_FILES` in `orchestrator/factory/receipts.py`) needs a receipt for its bytes before CI
passes (`evals/gate.py`, or `factory eval gate`). The integrator, codebase researcher and app onboarding prompts are not
gated. `--replay --receipt` records one for a replay that did not regress, and `factory eval waive --agent <role> --reason
"..."` records a person's decision that the change needs none. Recordings hold repository content and owners' notes:
keep them out of the repository.

## Datasets (see `datasets/*/README.md`)

| Dataset | Stage(s) | Items | Labels | Source |
|---|---|---|---|---|
| `outcomes-golden` | decomposer | 30 to 50 business outcomes with reference decompositions | expert rubric scores per criterion (D1 to D8), not reference similarity | internal history (anonymised) plus synthesized then human-validated |
| `spec-defects` | spec judge | 150 specs, half clean, half with one planted defect each (ambiguity, missing NFR, contradiction, untestable AC, interface mismatch with a sibling, scope leak) | defect type and location; clean/faulty | derived from `outcomes-golden` specs by mutation; human-checked |
| `spec-from-item` | spec writer | 60 work items with repos | rubric scores S1 to S13 by two humans; kappa reported | subset of `outcomes-golden` |
| `impl-from-spec` | implementer | 80 specs against real repos, each with a **hidden** acceptance test suite the implementer never sees | hidden tests pass/fail per AC; diff scope; protected tests | FeatureBench-style multi-file tasks (Zhou et al. 2602.10975) rewritten as specs, plus internal tasks; post-cutoff items only |
| `review-mutations` | reviewer, merge judge | 120 implementations: 60 clean, 60 with one injected defect (off-by-one, race, missing error path, spec deviation, security) paired to the clean parent | defect location and category; clean/faulty pairing | mutation of `impl-from-spec` passing solutions |
| `factory-bench` | end to end | 10 outcomes against 3 synthetic repos (Rails, TypeScript service, Python CLI) with hidden acceptance tests per outcome | outcome achieved (hidden suite), human interventions needed, cost, wall time | built for this purpose; refreshed quarterly to limit contamination |
| `gate-decisions` | online | every gate decision, edit, and comment in production | human decision, time to decide, edit distance between agent artifact and approved artifact | telemetry |

The table is the target. In this repository so far: `outcomes-golden` (3 sample rows), `spec-defects` (7) and `review-mutations` (5),
whose rows point at repositories and files that are not in this repository (the grader tests use the og-001 artifacts in
`examples/ledgerline/`), and `factory-bench`, which holds one reference run (`sample-app`), not a hidden suite.
`spec-from-item`, `impl-from-spec` and `gate-decisions` have no directory yet.

## Headline metrics per stage

- **Decomposer:** mean rubric score per criterion (D1 to D8) from a calibrated judge; share of items with any criterion at 0; blocking-question precision (share of blocking questions a human agreed were blocking); Gate 1 approval rate without changes (online).
- **Spec writer:** mean rubric score S1 to S13; share passing the judge in round 1; rounds to pass; Gate 2 edit distance (online).
- **Spec judge:** precision and recall on `spec-defects` by defect type; kappa against human scores on `spec-from-item`; position-swap consistency; false-pass rate on planted blocking defects (the number that matters most).
- **Implementer:** hidden-test pass rate per AC and per spec; pass^k over 5 runs (reliability, not luck); scope and protected-test violation rate; slice discipline (commits per spec, lines per commit); erosion metrics over consecutive specs on the same repo (SlopCodeBench-style: duplication, complexity delta).
- **Reviewer:** paired discrimination (flag rate on faulty minus flag rate on clean parent) by defect category; finding precision as judged by the merge judge and by humans on a sample; refutation-condition quality (share meetable); over-rejection rate on clean items.
- **Merge judge:** agreement with human merge decisions on a labelled sample (kappa); rate of `ready` verdicts later reverted or heavily amended on the forge (online).
- **Pipeline:** `factory-bench` outcome success with credible intervals; human interventions per outcome; list cost per merged change request; wall time per stage; escalation rate and reasons.

## What the evals must never do

- Use the implementer's own tests as the oracle. Hidden suites or a panel of independent implementations only (Liang et al. 2608.19626).
- Report raw agreement for a judge. Kappa, with position swap and at least one perturbation control (Norman et al. 2606.19544; Zhao et al. 2604.16790).
- Compare two agent versions on different items or with one run each.
- Let the eval set leak into agent skills, rubrics, or memory stores. `factory-bench` repos are private and rotated.
