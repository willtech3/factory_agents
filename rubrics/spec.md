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
`allowed_paths` is tight (no repository-wide globs), excludes existing tests unless the item is about them, and `non_goals` names adjacent work deliberately excluded.
- 0: `allowed_paths` contains `**` at the root or includes existing test files without reason.
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
`test_plan.commands` are commands that exist in the repository and run; `new_tests` cover every criterion with `automated_test`.
- 0: a command does not exist, or a criterion has no planned test.
- 1: commands exist; mapping incomplete.
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
