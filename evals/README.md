# Evaluating the factory

The factory is a pipeline of non-deterministic stages separated by deterministic checks and human gates. Evaluating it as one end-to-end number hides where it breaks (Gorinova et al., arXiv:2606.17799; Jarmak, arXiv:2608.13867), so the eval plan has four levels, each with its own dataset and its own cadence.

| Level | What is held fixed | What is measured | Cadence | Cost |
|---|---|---|---|---|
| L0 Deterministic | everything | schema validity, traceability, scope, protected tests, DAG, command results | every commit (CI) | free |
| L1 Stage-isolated | upstream artifacts (golden inputs) | one stage's output quality against labels | every agent prompt or model change | low to medium |
| L2 End-to-end | a synthetic repo and outcome set | outcome achieved, interventions, cost, time | weekly and before model upgrades | high |
| L3 Online | nothing | gate decisions, edit distance, MR outcomes, reverts | continuous | free (telemetry) |

Stochastic measurements follow the methodology in `docs/06-evaluation.md`: at least 5 runs per item per configuration (11 or more judge votes where a judge is the measurement, per Yagubyan 2606.13685), Bayesian credible intervals rather than point pass@k (Hariri et al. 2510.04265), paired comparisons across agent versions on identical items, and Cohen's kappa against human labels for every LLM grader.

## Layout

```
evals/
  README.md                 this file
  datasets/                 one directory per dataset: README.md (provenance, labelling, splits) + items.jsonl (sample rows)
  graders/
    deterministic.py        wraps orchestrator/factory/checks.py for eval use
    stochastic.py           LLM judge with N votes, position swap, bootstrap CI, kappa
    reviewer_paired.py      paired discrimination for the reviewer (clean vs planted-defect)
  run_eval.py               runs one stage over one dataset for one agent version; writes results/<timestamp>/
  results/                  gitignored
```

## Datasets (see `datasets/*/README.md`)

| Dataset | Stage(s) | Items | Labels | Source |
|---|---|---|---|---|
| `outcomes-golden` | decomposer | 30 to 50 business outcomes with reference decompositions | expert rubric scores per criterion (D1 to D8), not reference similarity | internal history (anonymised) plus synthesized then human-validated |
| `spec-defects` | spec judge | 150 specs, half clean, half with one planted defect each (ambiguity, missing NFR, contradiction, untestable AC, interface mismatch with a sibling, scope leak) | defect type and location; clean/faulty | derived from `outcomes-golden` specs by mutation; human-checked |
| `spec-from-item` | spec writer | 60 work items with repos | rubric scores S1 to S10 by two humans; kappa reported | subset of `outcomes-golden` |
| `impl-from-spec` | implementer | 80 specs against real repos, each with a **hidden** acceptance test suite the implementer never sees | hidden tests pass/fail per AC; diff scope; protected tests | FeatureBench-style multi-file tasks (Zhou et al. 2602.10975) rewritten as specs, plus internal tasks; post-cutoff items only |
| `review-mutations` | reviewer, merge judge | 120 implementations: 60 clean, 60 with one injected defect (off-by-one, race, missing error path, spec deviation, security) paired to the clean parent | defect location and category; clean/faulty pairing | mutation of `impl-from-spec` passing solutions |
| `factory-bench` | end to end | 10 outcomes against 3 synthetic repos (Rails, TypeScript service, Python CLI) with hidden acceptance tests per outcome | outcome achieved (hidden suite), human interventions needed, cost, wall time | built for this purpose; refreshed quarterly to limit contamination |
| `gate-decisions` | online | every gate decision, edit, and comment in production | human decision, time to decide, edit distance between agent artifact and approved artifact | telemetry |

## Headline metrics per stage

- **Decomposer:** mean rubric score per criterion (D1 to D8) from a calibrated judge; share of items with any criterion at 0; blocking-question precision (share of blocking questions a human agreed were blocking); Gate 1 approval rate without changes (online).
- **Spec writer:** mean rubric score S1 to S10; share passing the judge in round 1; rounds to pass; Gate 2 edit distance (online).
- **Spec judge:** precision and recall on `spec-defects` by defect type; kappa against human scores on `spec-from-item`; position-swap consistency; false-pass rate on planted blocking defects (the number that matters most).
- **Implementer:** hidden-test pass rate per AC and per spec; pass^k over 5 runs (reliability, not luck); scope and protected-test violation rate; slice discipline (commits per spec, lines per commit); erosion metrics over consecutive specs on the same repo (SlopCodeBench-style: duplication, complexity delta).
- **Reviewer:** paired discrimination (flag rate on faulty minus flag rate on clean parent) by defect category; finding precision as judged by the merge judge and by humans on a sample; refutation-condition quality (share meetable); over-rejection rate on clean items.
- **Merge judge:** agreement with human merge decisions on a labelled sample (kappa); rate of `ready` verdicts later reverted or heavily amended in GitLab (online).
- **Pipeline:** `factory-bench` outcome success with credible intervals; human interventions per outcome; list cost per merged MR; wall time per stage; escalation rate and reasons.

## What the evals must never do

- Use the implementer's own tests as the oracle. Hidden suites or a panel of independent implementations only (Liang et al. 2608.19626).
- Report raw agreement for a judge. Kappa, with position swap and at least one perturbation control (Norman et al. 2606.19544; Zhao et al. 2604.16790).
- Compare two agent versions on different items or with one run each.
- Let the eval set leak into agent skills, rubrics, or memory stores. `factory-bench` repos are private and rotated.
