# Rubrics

Rubrics are used in two places, and the distinction matters:

1. **Platform outcome grader (within a session).** Each writing session (decomposer, spec writer, implementer) is kicked off with `user.define_outcome` and the matching rubric. Managed Agents runs an independent grader that scores each iteration against the rubric and feeds per-criterion gaps back until `satisfied` or `max_iterations`. This is the cheap inner loop: it catches "forgot a section", "criterion not testable", "tests not run". Its reasoning is opaque to us (we only see `result` and `explanation`), so it is a quality floor, not the gate.
2. **Factory judges (between sessions).** The spec judge scores the same rubric criteria (`spec.md`, uploaded as `rubric.md`) explicitly, with evidence per criterion, in a separate session with a different model tier. The merge judge gets no rubric: it rules on each acceptance criterion with located evidence, and reads the orchestrator's deterministic checks (`deterministic_checks.json`), which it may not override. Their verdicts are the gate inputs. They see the artifacts and the codebase; they never see the writer's session.

Writing rules for rubric criteria (from `docs/research/`):

- Each criterion is independently gradeable and names the evidence that satisfies it. "Acceptance criteria are testable" is bad; "every acceptance criterion names `automated_test` as verification, or states why not" is good.
- Anchors at 0 / 1 / 2 / 3 so two graders land on the same number. The spec judge reports the 0-3 score; the platform grader treats 2+ as satisfied.
- No criterion rewards length, formatting, or confidence. Normalise formatting before judging where possible.
- Hard thresholds: any criterion at 0 fails the artifact regardless of the others.
- Every criterion traces to a failure mode we have seen or a cited source. Remove criteria that stop catching anything.

Files:
- `decomposition.md`: Gate 1 input.
- `spec.md`: scored by the spec judge per spec; also the writer's outcome rubric.
- `implementation.md`: the implementer's outcome rubric. The grader's copy ends at the marker line (`<!-- grader copy ends here`); the deterministic items below it are computed by the orchestrator and handed to the merge judge. They are generated from the rule registry (`orchestrator/factory/rules.py`, `factory rules`); edit the registry, not the block.
- `review_quality.md`: what a good review is (R1 to R6). No session receives it and no grader reads it yet: the merge judge audits the reviewer by its own prompt (`agents/merge_judge.md`).

A change to any of these files needs an eval receipt for its bytes before CI passes (`evals/gate.py`; see `evals/README.md`).
