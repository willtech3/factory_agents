# Implementation rubric

Used in two places. The in-session outcome grader (Managed Agents `user.define_outcome`) scores the items under **Scored by the grader** from `implementation_report.json` and the working tree. The items under **Computed by the orchestrator** are deterministic: the orchestrator computes them after the session from its own clone and hands the results to the merge judge, who may not override a failing one. The grader does not score those items and must not clone the repository, start the service, run mutation tests or re-run the test plan to anticipate them; the marker line between the two sections is where the orchestrator cuts the grader's copy.

**Scored by the grader.**

## I1. Every acceptance criterion has a test that fails without the change **[partly deterministic]**
`implementation_report.json` maps each `automated_test` criterion to a test (pytest node ids as `pytest --collect-only -q` prints them); the grader scores the mapping and the red/green evidence in the report, and the orchestrator confirms afterwards that each named test was collected and passed on `head_sha`, never trusting the report's own list.
- 0: a criterion with no test, or a named test that does not exist.
- 2: all mapped and passing.
- 3: as 2, and the mapping shows the test failing on `base_sha` (red/green evidence).

## I5. Vertical slices with checkpoints (advisory)
Commits are ordered slices of roughly 100 to 200 changed lines, each naming the criteria it covers. Green at a checkpoint means the repository's protected tests (those that existed on `base_sha`) pass at that commit; the full `test_plan` is required only at `head_sha`, where I2 checks it. This item ranks implementations and never fails one: a score below 2 is reported, not revised, and pushed history is never rewritten to raise it (a force-push that answers this item is a defect, not a fix).
- 1: a single squashed commit for a multi-criterion spec, slices not labelled with criteria, or a checkpoint that leaves the protected tests red.
- 2: labelled slices, protected tests green at each checkpoint, full test plan green at `head_sha`.
- 3: as 2, with structural (refactor) commits separated from behavioural ones.

## I6. No scope creep
No behaviour changed, dependency added, configuration introduced, or abstraction created that no criterion requires. A committed provenance capture or evidence file no product test reads, and test code about the factory's evidence (tests of a browser runner, its step classifier or its redaction), are scope creep (post-mortem 2026-10-11 §2); a browser command or recipe baseline the approved spec plans is not.
- 0: a new dependency or public interface the spec did not ask for.
- 1: small unrequested changes with a stated reason.
- 2: changes map to criteria.
- 3: as 2, and the diff is smaller than a reasonable reviewer would expect.

## I7. Report is honest and specific
`implementation_report.json` lists commands with real output summaries, files changed, `test_concerns`, and residual uncertainty. Claims are checkable.
- 0: a claim the orchestrator's run contradicts ("all tests pass" when one failed).
- 1: accurate but vague.
- 2: accurate with locations.
- 3: as 2, and names what the implementer would look at first if it were reviewing.

<!-- grader copy ends here: the orchestrator sends nothing below this line to the in-session grader -->

<!-- rules:begin: generated from orchestrator/factory/rules.py by `factory rules`; edit the registry, not this block -->
**Computed by the orchestrator.** Deterministic; handed to the merge judge with the results. Each item is 0 when its check failed and 2 when it passed; the judge cannot override a failure.

## I2. test-plan-passes **[deterministic: `test_plan_commands`]**
Every `test_plan.commands` entry (the tests, and the builds and linters the spec lists) exits 0 on the head the orchestrator verifies, from a clean checkout. A structured entry `{runner, select}` is run by the orchestrator with its own reporters.

## I3. allowed-paths **[deterministic: `diff_scope`]**
Change only files that match the spec's `allowed_paths`; a file outside them fails the merge. If the change needs another path, the spec is wrong: stop and say so (`blocked.md`).

## I4. protected-tests **[deterministic: `protected_tests`]**
Existing tests are protected. Only an existing test file the approved spec names by its exact path in `allowed_paths` may be updated, to test the specified behaviour; globs do not authorize it. Never delete or rename an existing test file.

## I4. no-test-weakening **[deterministic: `test_weakening`]**
Never skip a test or weaken an assertion to make the change pass.

## I9. e2e-passes **[deterministic: `e2e_local`, `e2e_preview`]**
Every `test_plan.e2e` entry passes in the orchestrator's run of the head, `local` and `preview` entries alike: it ran, exited 0, and any browser evidence it owes passed. Not run is not passed.
<!-- rules:end -->
