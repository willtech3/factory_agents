# The factory in one page

A **run** takes one business outcome and ends with one or more GitLab merge requests that a human reviews and merges, followed by an observed deploy to `dev` that the orchestrator verifies end to end. Between the two sit six agent roles, three human gates, and a deterministic orchestrator that moves artifacts between them. The deployment platform (GCP, WIF identity for GitLab CI, CI templates, preview environments, dev) already exists; the factory uses it and never authors it.

```
                    ┌──────────────┐
  outcome.json ───► │  Decomposer  │ ───► decomposition.json ───► ▣ GATE 1  (owner: approve / changes / reject)
                    └──────────────┘                                   │
                           ▲  ask_human (blocking questions only)      ▼
                    ┌──────────────┐        ┌──────────────┐
                    │  Spec writer │ ◄────► │  Spec judge  │   loop ≤ 3 rounds, judge in a separate session
                    └──────────────┘        └──────────────┘
                           │ specs/*.json        │ verdict.json
                           └─────────┬───────────┘
                                     ▼
                              ▣ GATE 2  (owner: approve / approve subset / changes / reject)
                                     │
            per spec, in dependency waves, each on its own branch
                                     ▼
                    ┌──────────────┐  diff   ┌──────────────┐  findings  ┌──────────────┐
                    │ Implementer  │ ──────► │   Reviewer   │ ─────────► │ Merge judge  │
                    │  (slices)    │ ◄────── │ (fresh ctx)  │            │ (independent)│
                    └──────────────┘ response└──────────────┘            └──────────────┘
                          ▲    both run test_plan.e2e against the branch's PREVIEW env     │ ready
                          │    orchestrator runs tests + e2e on preview + deterministic checks
                          └── another_round ◄────────────────────────────────────┤
                                                                                  ▼
                                                                         ┌──────────────┐
                                                                         │  Integrator  │ ──► GitLab MR
                                                                         └──────────────┘
                                                                                  ▼
                                                                ▣ GATE 3  human code review + merge in GitLab
                                                                                  │ mr.merged
                                                                                  ▼
                                               platform CI deploys main ──► dev   (observed, never driven by an agent)
                                               orchestrator: deploy.* events, e2e on dev URL ──► DEPLOYED_DEV
                                                                                  │ failure
                                                                                  ▼
                                                                ▣ DEPLOY_FAILURE gate (log excerpt; back to implementing | close)
```

## The five design commitments

1. **Humans decide at three points, and the third is real code review.** Gate 1 approves the shape of the work; Gate 2 approves the contracts; Gate 3 is an ordinary GitLab merge request that an engineer reads. The team that popularised "review the plan, not the code" ran a lights-off factory for five months and reinstated code review ([practitioners.md §1c](research/practitioners.md)); the upstream gates exist to make Gate 3 cheap, not to remove it.
2. **Judges are separate from authors, in separate sessions.** The spec judge never sees the spec writer's reasoning; the reviewer never sees the implementer's; the merge judge sees both artifacts but neither transcript. Self-grading skews positive regardless of model capability ([academic-2026.md §1](research/academic-2026.md)).
3. **Every claim is falsifiable and execution-grounded.** A review finding without a reproduction and a refutation condition is not a finding. A refutation without evidence is not a refutation. Tests are run by the orchestrator, not reported by the agent.
4. **Deterministic checks sit between every pair of stages and nobody can override them.** Schema validity, DAG acyclicity, metric coverage, diff scope, protected tests, test commands. Cheap, exact, and the main protection against error cascades.
5. **Coordination happens through artifacts, not conversation.** Agents read and write JSON that validates against `schemas/`; the orchestrator moves files. No agent talks to another agent.
6. **Done means running, and the platform is not the factory's to change.** Every user-visible criterion has an end-to-end check that runs against the branch's preview environment; after merge the orchestrator watches the platform's own pipeline deploy `dev` and runs the checks again. No agent deploys or touches infrastructure; the platform contract is a skill the platform team owns ([05-platform-and-deployment.md](05-platform-and-deployment.md)).

## What is in this repository

| Path | What | Status |
|---|---|---|
| `docs/` | this design: pipeline, agents, human gates and UX, platform mapping, evaluation, principles, roadmap, ADRs | complete draft |
| `schemas/` | JSON Schema for every artifact, plus a placeholder service-manifest schema the platform team replaces | complete; validated by tests |
| `agents/` | Managed Agents definitions in `ant apply` format | complete draft; not yet applied to a workspace |
| `rubrics/` | per-criterion rubrics with 0 to 3 anchors, including deployability and end-to-end coverage | complete draft |
| `skills/` | `platform-contract`: a placeholder for the platform team's existing skill (headings only) | placeholder by design |
| `orchestrator/` | Python outer loop: state machine, gates, checks, session driver, stages (including the observed dev deploy), GitLab, CLI | state machine, checks, gates, graders, deploy observation tested (57 tests); session driver and GitLab client written against documented APIs but not yet run live; pipeline and environment reads unverified against the self-hosted instance |
| `evals/` | four-level eval plan, dataset specs with sample rows, graders, runner | graders tested; runner's produce step is a stub until the live spike |
| `examples/ledgerline/` | one complete schema-valid run on a fictional invoicing product | complete |

Start with [01-pipeline.md](01-pipeline.md) for the mechanics, [03-human-in-the-loop-ux.md](03-human-in-the-loop-ux.md) for what a person experiences, [05-platform-and-deployment.md](05-platform-and-deployment.md) for the platform boundary, and [roadmap.md](roadmap.md) for what to build first.
