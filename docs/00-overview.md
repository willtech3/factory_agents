# The factory in one page

A **run** takes one business outcome and ends with one or more change requests (GitHub pull requests by default, GitLab merge requests as the second forge) that a human reviews and merges, followed by an observed deploy to `dev` that the orchestrator verifies end to end. Between the two sit six agent roles, three human gates, and a deterministic orchestrator that moves artifacts between them. Humans read gates on artifact pages and decide them on the tracker (Linear by default, Jira later), the CLI, or the forge. The deployment platform (CI templates, preview environments, dev) already exists; the factory uses it and never authors it.

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
                                                                         │  Integrator  │ ──► change request (PR/MR)
                                                                         └──────────────┘
                                                                                  ▼
                                                                ▣ GATE 3  human code review + merge on the forge
                                                                                  │ cr.merged
                                                                                  ▼
                                               platform CI deploys main ──► dev   (observed, never driven by an agent)
                                               orchestrator: deploy.* events, e2e on dev URL ──► DEPLOYED_DEV
                                                                                  │ failure
                                                                                  ▼
                                                                ▣ DEPLOY_FAILURE gate (log excerpt; back to implementing | close)
```

## The six design commitments

1. **Humans decide at three points, and the third is real code review.** Gate 1 approves the shape of the work; Gate 2 approves the contracts; Gate 3 is an ordinary change request (pull request or merge request) that an engineer reads. The team that popularised "review the plan, not the code" ran a lights-off factory for five months and reinstated code review ([practitioners.md §1c](research/practitioners.md)); the upstream gates exist to make Gate 3 cheap, not to remove it.
2. **Judges are separate from authors, in separate sessions.** The spec judge never sees the spec writer's reasoning; the reviewer never sees the implementer's; the merge judge sees both artifacts but neither transcript. Self-grading skews positive regardless of model capability ([academic-2026.md §1](research/academic-2026.md)).
3. **Every claim is falsifiable and execution-grounded.** A review finding without a reproduction and a refutation condition is not a finding. A refutation without evidence is not a refutation. Tests are run by the orchestrator, not reported by the agent.
4. **Deterministic checks sit between every pair of stages and nobody can override them.** Schema validity, DAG acyclicity, metric coverage, diff scope, protected tests, test commands. Cheap, exact, and the main protection against error cascades.
5. **Coordination happens through artifacts, not conversation.** Agents read and write JSON that validates against `schemas/`; the orchestrator moves files. No agent talks to another agent.
6. **Done means running, and the platform is not the factory's to change.** Every user-visible criterion has an end-to-end check that runs against the branch's preview environment; after merge the orchestrator watches the platform's own pipeline deploy `dev` and runs the checks again. No agent deploys or touches infrastructure; the platform contract is a skill the platform team owns ([05-platform-and-deployment.md](05-platform-and-deployment.md)).

## What is in this repository

The layout, which module does what, and what is live, stubbed or unused are kept in one place: [repo-map.md](repo-map.md).

Start with [01-pipeline.md](01-pipeline.md) for the mechanics, [03-human-in-the-loop-ux.md](03-human-in-the-loop-ux.md) for what a person experiences, [05-platform-and-deployment.md](05-platform-and-deployment.md) for the platform boundary, [07-personal-e2e-setup.md](07-personal-e2e-setup.md) to run it with a personal GitHub repo and Linear workspace, [08-factory-console.md](08-factory-console.md) for the console, [09-app-onboarding.md](09-app-onboarding.md) for hosted verification, and [roadmap.md](roadmap.md) for what to build first.
