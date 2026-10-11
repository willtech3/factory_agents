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

**Computed by the orchestrator.** Deterministic; handed to the merge judge with the results.

## I2. Test plan commands pass **[deterministic]**
Every command in `spec.test_plan.commands` exits 0 on `head_sha` in a clean sandbox run by the orchestrator, not by the implementer.
- 0: any failure.
- 2: all pass.

## I3. Diff stays inside allowed paths **[deterministic]**
Every changed file matches a glob in `allowed_paths`.
- 0: any file outside.
- 2: all inside.

## I4. Existing tests untouched **[deterministic]**
No existing test file is modified or deleted unless its path is in `allowed_paths` and the spec says the item is about those tests. No test is skipped, marked expected-fail, or weakened.
- 0: any violation.
- 2: clean.

## I8. Build and static checks **[deterministic]**
The repository's build, type check, and lint (as configured in the repo) pass on `head_sha`.
- 0: any failure that did not exist on `base_sha`.
- 2: clean.

## I9. End-to-end checks pass on the preview environment **[deterministic]**
Every `spec.test_plan.e2e` entry with `target: preview` exits 0 when the orchestrator runs it against the branch's preview URL (read from the forge's deployment record for `head_sha`, never from the implementer's report), with `$BASE_URL` substituted. The implementer must also have run them itself and recorded the output in `implementation_report.json`; the orchestrator's run is the one that counts. Unit tests passing while the deployed change does not work is the failure this exists to catch (practitioners.md §3; academic-2026.md §5, SWE-Cycle).
- 0: any entry fails, or the preview deploy for `head_sha` did not succeed.
- 2: all pass on the preview URL.
- 3: as 2, and the report shows the same entries failing on the base branch's preview (the behaviour is new, not pre-existing).
