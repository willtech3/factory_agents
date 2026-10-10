# factory-bench

End-to-end benchmark: outcomes against synthetic repositories with hidden acceptance suites. The only level that measures what the factory is for, and the most expensive to run, so it runs weekly and before every model or major prompt change.

## Repositories (private, rotated quarterly)
| repo | stack | size | why |
|---|---|---|---|
| `ledgerline` | Rails monolith with Stripe-shaped billing | ~40k lines | realistic webhook, job, mailer, admin surfaces; migrations |
| `notifier-ts` | TypeScript service, Postgres, queue | ~15k lines | interface-heavy; multi-spec coherence matters |
| `lintr` | Python CLI | ~6k lines | small; isolates pipeline overhead from codebase difficulty |

Repositories are synthetic (written for this purpose with realistic warts) so that model contamination from public code is not a factor, and are regenerated or substantially changed each quarter. Each has a hidden `acceptance/` suite per outcome that is never mounted into any agent session (SEAL, arXiv:2607.24300).

## Outcomes
10 outcomes, stratified: 3 simple, 5 medium, 2 hard. Each outcome has: `outcome.json`, hidden acceptance suite, a human "intervention script" (pre-written answers to the questions a reasonable agent would ask, and pre-written gate decisions) so runs can be unattended and reproducible, and a cost ceiling.

## Metrics (per outcome, 5 runs each)
- **Outcome achieved:** hidden suite pass rate at the end of the run (all MRs merged by the harness after `ready`). Reported as a Bayesian credible interval across runs (Hariri et al. 2510.04265), and pass^5 for reliability (Khanal et al. 2603.29231).
- **Partial credit:** share of hidden acceptance tests passing, since 0/1 hides progress on hard items (Li et al. 2607.08964).
- **Human interventions:** count of blocking questions, request-changes decisions, and escalations from the intervention script that were triggered.
- **Cost and time:** list cost per outcome and per merged change request; wall time per stage.
- **Quality erosion:** complexity and duplication delta of the repo after the run versus before (SlopCodeBench-style), because passing tests does not mean maintainable code.
- **Stage attribution:** for each failed outcome, which stage's artifact first diverged from the reference (decomposition, spec, implementation, review), so the number points at something fixable.

## Contamination controls
- Repos private; rotated; outcome statements rewritten each rotation.
- Membership-inference spot checks on repo snippets against the model (Zhao et al. 2609.09865) when a new model is introduced.
- Hidden suites never leave the harness; agents' probe scripts are inspected for attempts to locate them (Destefanis and Aste 2608.16801 report agents hunting grading material in ~80% of sealed runs).
