# Specification rubric

Scored per spec, per criterion, 0 to 3. Any 0 fails the spec. The spec judge reports these scores with evidence in `verdict.per_spec[].criteria`. Set-level coherence is judged separately (see `agents/spec_judge.md`).

## S1. Goal traces to the work item and the outcome
`goal` restates the work item's intent and names the success metric(s) it serves.
- 0: goal contradicts the work item or names no metric.
- 1: goal matches but is a paraphrase of the title.
- 2: goal states what is true after merge that was not before, and the metric.
- 3: as 2, and names what would indicate the goal was missed in production.

## S2. Acceptance criteria are verifiable
Each criterion is Given/When/Then, observable, and has `verification: automated_test` or a stated reason it cannot.
- 0: a criterion is unobservable ("the code is clean") or `manual_check` with no reason.
- 1: all observable, but some could be read two ways.
- 2: all observable and unambiguous; one reading only.
- 3: as 2, and includes at least one negative or boundary criterion per interface.

## S3. Criteria are complete for the goal
Together the criteria entail the goal; nothing the goal needs is missing.
- 0: the goal could be "met" with all criteria passing (an obvious case is uncovered).
- 1: covered, but relies on an implicit criterion the implementer must infer.
- 2: complete; a tester following only the criteria would confirm the goal.
- 3: as 2, and error paths and empty states are covered.

## S4. Interfaces are precise
Every introduced or changed interface has a signature, schema fragment, or example payload specific enough that two implementers produce compatible code.
- 0: an interface is named without a definition.
- 1: defined, but types or optionality are missing.
- 2: fully typed with an example.
- 3: as 2, and backwards-compatibility behaviour is stated.

## S5. Scope is bounded
`allowed_paths` is tight (no repository-wide globs), excludes existing tests unless the item is about them, `non_goals` names adjacent work deliberately excluded, and `estimated_lines` (hand-written changed lines, tests included, generated files excluded; post-mortem 2026-10-11 §3) is at size M or below under the run's budget, with every regenerated file declared in `regenerated`.
- 0: `allowed_paths` contains `**` at the root or includes existing test files without reason, or the estimate is above size M or implausible for the criteria.
- 1: bounded but broader than the criteria require.
- 2: paths match the criteria; non-goals present.
- 3: as 2, and `forbidden_changes` lists tempting but disallowed changes.

## S6. Grounded in the current code
`context` cites `path:line` evidence the writer actually read, and the claims hold.
- 0: a cited path does not exist or the claim is wrong.
- 1: grounded in directories, not lines.
- 2: specific and correct.
- 3: as 2, and notes an existing test the implementer must keep green.

## S7. Test plan is executable
`test_plan.commands` are test, build or lint runs the repository supports, runnable at any commit of the implementer's branch; a shell assertion that a file exists or that a docstring or README contains text (`test -f`, `grep`) is not a command but a `static_check` criterion. `new_tests` cover every criterion with `automated_test`. The planned tests are proportionate: the ones a maintainer would keep, each behaviour and boundary once. This is judgement, not a ratio of test lines to code lines (post-mortem 2026-10-11 §2: 71 lines of product change came with 1,024 lines of tests and fixture).
- 0: a command does not exist or is a file-existence or text assertion, or a criterion has no planned test.
- 1: commands exist; mapping incomplete, or the planned tests are far heavier than the behaviour (a suite per criterion, tests of test helpers).
- 2: every automated criterion maps to a planned test.
- 3: as 2, and names the fixture or harness the tests should reuse.

## S8. Dependencies and ordering
`dependencies.specs` matches the decomposition's `depends_on`, and consumed interfaces match the producing spec's definitions by name and shape.
- 0: a dependency contradiction or an interface mismatch with a sibling spec.
- 1: consistent, but a consumed interface is restated rather than referenced.
- 2: consistent and referenced.
- 3: as 2, and states what happens if the dependency ships late (feature flag, stub).

## S9. Open questions have defaults
Every open question states a default and whether it blocks; no question the code or the outcome already answers.
- 0: a blocking question with no default, or one answered by the repository.
- 2: all questions defaulted; blocking ones justified.
- 3: as 2, and fewer than four questions.

## S10. Length and density
`spec.md` is under 200 lines and contains nothing the implementer does not need.
- 0: over 300 lines, or restates large parts of the codebase.
- 1: under 300 but padded.
- 2: under 200, dense.
- 3: under 150 with no loss.

## S11. Deployable through the platform contract
The spec conforms to the platform-contract skill (`skills/platform-contract/SKILL.md`): the service manifest at the contract's fixed path is present and valid against `schemas/service_manifest.schema.json` (the orchestrator's `manifest_valid` check), any manifest change the work needs is declared as a `config` interface, and nothing in the spec asks the implementer to touch what the contract reserves (IAM, networking, the platform repository). Agents that cannot see the deployment boundary ship changes that pass tests and cannot run; the full-cycle drop in SWE-Cycle (academic-2026.md §5) and boundary-crossing actions belonging to humans (practitioners.md §4, §1b Factor 7) are the evidence.
- 0: the manifest is missing or invalid, or the spec requires a change the contract forbids.
- 1: manifest valid but a needed env var, migration, or health change is not declared.
- 2: manifest valid and every needed change declared.
- 3: as 2, and the spec states what the preview environment must contain (seed data, test-mode keys) for `test_plan.e2e` to run.

## S12. End-to-end coverage of user-visible behaviour
Every acceptance criterion with `user_visible: true` is covered by at least one `test_plan.e2e` entry, and every criterion is honestly flagged. The entry runs against a deployed instance (`target: preview`) whenever one verification run can observe the behaviour. When it cannot, because the behaviour needs time to pass or an event no single run can produce (a date rollover, a scheduled retry, a third party's webhook), the honest shape is `user_visible: true`, a `target: local` entry that forces the condition (a patched clock, a replayed event) and a `verification_note` on the criterion naming why one run cannot observe it and what the local entry stands in for. Agents declare victory without end-to-end verification unless the harness demands it (practitioners.md §3, Anthropic effective harnesses); the orchestrator's `e2e_coverage` check enforces the mapping and counts `local` and `preview` entries alike, the judge checks the flags and the targets.
- 0: a behaviour that one verification run can observe on the deployed service is flagged `false`, has no e2e entry, or has only a `local` entry.
- 1: covered, but an entry's command would not fail if the criterion were unmet (asserts only a 200), or a `local` stand-in has no `verification_note`.
- 2: every user-visible criterion covered by a command that fails when the behaviour is wrong: a `preview` entry where one run can observe it, otherwise a `local` entry plus the `verification_note`.
- 3: as 2, with a `local` counterpart for every `preview` entry so the implementer can iterate before the preview deploys.

## S13. Criteria describe the product, not the proof
Each acceptance criterion is behaviour a user, an operator or a caller of the app can observe through the app. The evidence the factory collects to prove it is not a criterion: the browser evidence format or its count of screenshots and recordings, a test harness's classification of its own outcomes (blocked versus failed), redaction of test credentials, the shape of an evidence file, or "values derive from captured evidence". A capture that proves provenance (the raw API response behind derived data) is a run artifact, planned as a committed fixture only when a product test reads it. Post-mortem 2026-10-11 §2: two criteria about one feature's browser test (its `result.json` and media, its status classifier) turned review onto the test, and every contested finding of that run was about the test's own plumbing. The orchestrator's `criteria_describe_product` pre-check refuses a criterion that names the evidence protocol itself (`FACTORY_*` variables, `viewport_observed`).
- 0: a criterion is about the factory's evidence or a test's own mechanics, or the plan commits a provenance capture no product test reads.
- 2: every criterion is product behaviour someone can observe.
- 3: as 2, and each criterion says who observes it (user, operator, caller).
