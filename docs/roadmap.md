# Roadmap: from design to a first real run

The design is complete enough to iterate on. What exists has not run against the live platform. The order below is chosen so that each step de-risks the next and produces measurable output.

## Spike 1: platform plumbing (1 to 2 days)
Goal: one `decomposer` session end to end on the Ledgerline example outcome against a real (fork of a) repository.
- `ant apply --dry-run agents environments`, review the plan, apply. Commit `claude-lock.json`.
- Create the vault and the `GITLAB_TOKEN` environment-variable credential. **Verify** that git over HTTPS to your GitLab accepts `Authorization: Bearer <PAT>` through the egress substitution. If it does not, implement the `push_branch` host-side custom tool fallback in ADR-0003.
- Run `factory doctor`, then `factory run examples/ledgerline/outcome.json --no-gitlab`.
- Fix whatever the live SDK surface disagrees with in `sessions.py` (field names on events, `files.list` shape, `usage.list_cost`). Expect some.
- Measure: cost, wall time, outcome iterations, whether `decomposition.json` validated first time.

## Spike 2: the spec loop and Gate 2 in GitLab (2 to 3 days)
- Run specify on the decomposition from spike 1. Watch the judge's round-1 verdict closely: it is the first real test of the rubric anchors.
- Wire the design MR: branch, commit files, open draft MR, harvest comments into `feedback.md`. Test request-changes round-trip.
- Wire `evals/run_eval.py::produce` to the driver and grade the spike's outputs with `--from-artifacts` first.

## Spike 3: one implementation through review and MR (3 to 5 days)
- Implement `spec_smart-retries` on the fork. Replace `LocalVerifier` with a GitLab CI job on the factory branch (same interface: commands, exit codes, diff).
- Watch the reviewer: are findings falsifiable, do refutation conditions make sense, how many are withdrawn? Tune `reviewer.md` from what you see, one rule per observed failure.
- Open the MR. Have a real engineer review it and time them.

## Milestone A: unattended end-to-end on factory-bench `lintr` (week 3)
- Build the smallest `factory-bench` repo (the Python CLI) with two outcomes and hidden suites.
- Run unattended with scripted gate decisions, 5 times. Report credible intervals, cost, interventions.
- Decide the first open eval questions in `06-evaluation.md` (reviewer passes 1 vs 2; outcome iterations 1 vs 4).

## Milestone B: first production outcome (week 5 to 6)
- A low-risk internal outcome, owner and engineer briefed, L3 telemetry on.
- Factory Console v0: a static page rendered from `events.jsonl` plus the approval inbox is enough.
- Slack notifications for gates and questions.

## Milestone C: evals that gate changes (week 8)
- `outcomes-golden` to 12 items, `spec-defects` to 60, `review-mutations` to 40, with human labels and kappa.
- CI: L0 on every commit; L1 on changes to `agents/` or `rubrics/`.

## Deliberately deferred
- **Tiered or automatic approval** of any gate. Needs six months of gate telemetry first.
- **Memory stores** for cross-run learning. Evidence is mixed; start with a human-curated read-only conventions store per repo once there are enough runs to curate from.
- **Multiple target repositories per outcome.** Schema allows it; orchestrator handles the first only.
- **Stacked MRs** for dependent specs. Currently serial: a dependent spec waits for the upstream merge.
- **Self-hosted sandboxes.** Only if a repository cannot be cloned into Anthropic's cloud environment.

## Known gaps in what is here

- `sessions.py`, `gitlab.py`, and the stage modules are written against documented APIs and have not been executed live. Treat field names as hypotheses until spike 1.
- `run_eval.py::produce` is a stub.
- The Factory Console is described, not built. Everything it needs is in `events.jsonl` and the queue files.
- `LocalVerifier` runs test commands on the orchestrator host; production must move this to CI.
- `merge_findings` de-duplicates by file and the first three characters of the line number, which is crude; good enough until real findings show what collides.
- Human comments at Gate 2 are broadcast to every spec's writer; per-spec routing from inline MR positions is straightforward and should be done in spike 2.
