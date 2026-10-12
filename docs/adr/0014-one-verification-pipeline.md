# ADR-0014: One verification pipeline: `verify(target, plan)` and `judge(bundle, spec)`

**Status:** accepted · **Date:** 2026-10-12 · Phase 3 of [ADR-0013](0013-durable-runs.md).

## Context
A candidate head was verified in three ways, one per preview mode, and each fix landed in only one of them:

- `managed` ran everything in a hosted sandbox (`HostedVerifier`).
- `local` ran the commands in a per-run virtualenv on the controller and started the service from its manifest (`LocalVerifier`, `LocalPreview`).
- `platform` ran the commands the same way and the preview entries against the platform's per-branch deployment.

Stage 5 had a fourth path against dev. The paths disagreed:

- **A failing `target: local` e2e entry passed in every mode** unless a criterion mapped to it. `run_all_for_merge` judged only `target: preview` rows, and the controller never ran local entries outside the hosted sandbox.
- **Bun tests yielded evidence only in managed mode.** The hosted harness attached Bun's JUnit reporter; `LocalVerifier.run_commands` attached only pytest's.
- **"Passed" had four definitions.** The merge checks, the hosted record's status, Stage 5's failure list and `DeployObservation.passed` each wrote their own `exit_code == 0` test, and only some of them looked at browser evidence.
- **The test plan was shell text the spec writer wrote.** The planning prompt carried a long paragraph about Bun command shapes, because a package script, a wrapper or a reporter flag silently lost the evidence.
- **The whole recipe was pinned.** Its trust boundary (what the sandbox may reach, which credentials reach which hosts) and its runtime (how the app is built, started and checked) shared one fingerprint. A change that needed a different start command had to go back through onboarding.

## Decision
1. **`verification.verify(ctx, target, plan) -> EvidenceBundle`** is the only way a round verifies a head.
   - The target is `hosted` (preview mode managed), `local` or `platform`.
   - The bundle has the same shape on every target:
     - the setup check, and the environment check (hosted);
     - one row per plan command and per e2e entry, in the plan's order;
     - the test ids collected and reported;
     - the preview record.
   - Verification decides nothing.
2. **`verification.judge(bundle, spec, ...)`** turns a bundle into the deterministic checks the merge judge receives and may not override. It is the bundle's setup and environment checks plus `checks.run_all_for_merge`.
3. **One definition of a passing row**, `checks.row_passed`: a row passed when it ran, exited 0, and any browser evidence it owes passed. The command and e2e checks use it. So do the hosted record's status, Stage 5's failure list and `DeployObservation.passed`.
4. **Every e2e entry is judged.** The controller's clone runs entries in the plan's order. On the `local` target, both targets run against the instance the controller started, as in the hosted sandbox. On the `platform` target, preview entries run against the preview deployment, and local entries start what they need themselves. `e2e_local` and `e2e_preview` each fail on an entry that did not pass or has no row.
5. **One way to run a test command** (`environment_runner.instrumented_command`, `reported_outcomes`). The hosted harness and `LocalVerifier` attach the same reporters and read the same reports. Passing and known test ids come from one function, `evidence_from_reports`, on every host. Missing evidence fails the mapping only where it is required (hosted), as before.
6. **Structured test plans** (`plans.plan_of`):
   - A test run is `{"runner": "pytest" | "bun", "select": [...]}`. The controller renders it as `python3 -m pytest -q …` or `bun test …` and attaches its reporters. The schema refuses a selection that is a flag.
   - An e2e entry may name a `journey` instead of a `command`; it is rendered as `factory:journey <name>`.
   - Strings stay valid for builds, linters and older specs.
   - Everything that reads command text reads the rendered plan: verification, the merge checks, Stage 5, the pull request and the spec's reading page. Agents still see the spec as written.
7. **The recipe has a trusted part and a runtime part** (`test_environments.TRUST_KEYS`).
   - **Trusted:** `network`, `secrets`, `data` and `command_environment`. A person approves it at onboarding, and a change may not alter it.
   - **Runtime:** everything else, such as the lifecycle commands, services, browser and functional checks, and timeouts. It may change in a diff.
   - The hosted verifier accepts a candidate recipe in two cases:
     - it hashes to the approved fingerprint; or
     - the base holds the approved recipe and the candidate changes only the runtime part.
   - Its record says `runtime_changed`, and the run exercises the changed recipe like the rest of the change.

The code moved to match. `local_verifier.py` holds the controller's own verification host (`LocalVerifier`, `LocalPreview`). `verification.py` holds the pipeline, preview lookup and test evidence. `plans.py` holds plan rendering. `stages/implement.py` keeps the loop and re-exports the old names.

## What this phase did not do
- **The local target still runs on the controller's host**, in a per-run virtualenv. Running it in a container is a separate change.
- **Stage 5 keeps its injected e2e runner.** It reads its rows through `plan_of` and `row_passed`, but does not build a bundle. Hosted dev verification is still blocked, as before.
- **Agent prompts still carry per-mode preview clauses.** Collapsing them into one sentence ("the controller verifies your pushed head; the evidence is in verification.json") belongs to the rule registry (phase 4), which generates prompts. Phase 4 kept them in the kickoff strings, because they describe each verification host's mechanics, not rules ([ADR-0015](0015-rule-registry.md)).

## Consequences
- Specs with `target: local` entries that never passed now fail `e2e_local` on every target. This is the fix, and it can turn an old spec's green round red.
- A Bun-only repository verified on the controller now has test evidence instead of an "unverified" mapping.
- A recipe change limited to commands, services or checks no longer needs re-onboarding. It needs a passing hosted verification, like the code it ships with. Each such recipe gets its own environment definition (environments are keyed by the full recipe fingerprint), which `factory environment prune` already cleans up.
