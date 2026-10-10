# Implementation rubric

Used as the implementer session's outcome rubric and checked again by the merge judge. Items marked **[deterministic]** are computed by the orchestrator and handed to the judge; the judge may not override a failing deterministic item.

## I1. Every acceptance criterion has a test that fails without the change **[partly deterministic]**
`implementation_report.json` maps each `automated_test` criterion to a test; the orchestrator confirms each named test exists and passed on `head_sha`.
- 0: a criterion with no test, or a named test that does not exist.
- 2: all mapped and passing.
- 3: as 2, and the mapping shows the test failing on `base_sha` (red/green evidence).

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

## I5. Vertical slices with checkpoints
Commits are ordered slices of roughly 100 to 200 changed lines, each naming the criteria it covers and each leaving the test plan green.
- 0: a single squashed commit for a multi-criterion spec, or a commit that leaves tests red.
- 1: slices present but not labelled with criteria.
- 2: labelled, green at each checkpoint.
- 3: as 2, with structural (refactor) commits separated from behavioural ones.

## I6. No scope creep
No behaviour changed, dependency added, configuration introduced, or abstraction created that no criterion requires.
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

## I8. Build and static checks **[deterministic]**
The repository's build, type check, and lint (as configured in the repo) pass on `head_sha`.
- 0: any failure that did not exist on `base_sha`.
- 2: clean.

## I9. End-to-end checks pass on the preview environment **[deterministic]**
Every `spec.test_plan.e2e` entry with `target: preview` exits 0 when the orchestrator runs it against the branch's preview URL (read from the GitLab pipeline's environment, never from the implementer's report), with `$BASE_URL` substituted. The implementer must also have run them itself and recorded the output in `implementation_report.json`; the orchestrator's run is the one that counts. Unit tests passing while the deployed change does not work is the failure this exists to catch (practitioners.md §3; academic-2026.md §5, SWE-Cycle).
- 0: any entry fails, or the preview deploy for `head_sha` did not succeed.
- 2: all pass on the preview URL.
- 3: as 2, and the report shows the same entries failing on the base branch's preview (the behaviour is new, not pre-existing).
