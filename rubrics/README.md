# Rubrics

Rubrics are used in two places, and the distinction matters:

1. **Platform outcome grader (within a session).** Each writing session (decomposer, spec writer, implementer) is kicked off with `user.define_outcome` and the matching rubric. Managed Agents runs an independent grader that scores each iteration against the rubric and feeds per-criterion gaps back until `satisfied` or `max_iterations`. This is the cheap inner loop: it catches "forgot a section", "criterion not testable", "tests not run". Its reasoning is opaque to us (we only see `result` and `explanation`), so it is a quality floor, not the gate.
2. **Factory judges (between sessions).** The spec judge and merge judge score the same rubric criteria explicitly, with evidence per criterion, in a separate session with a different model tier. Their verdicts are the gate inputs. They see the rubric, the artifacts, and the codebase; they never see the writer's session.

Writing rules for rubric criteria (from `docs/research/`):

- Each criterion is independently gradeable and names the evidence that satisfies it. "Acceptance criteria are testable" is bad; "every acceptance criterion names `automated_test` as verification, or states why not" is good.
- Anchors at 0 / 1 / 2 / 3 so two graders land on the same number. Judges report the 0-3 score; the platform grader treats 2+ as satisfied.
- No criterion rewards length, formatting, or confidence. Normalise formatting before judging where possible.
- Hard thresholds: any criterion at 0 fails the artifact regardless of the others.
- Every criterion traces to a failure mode we have seen or a cited source. Remove criteria that stop catching anything.

Files:
- `decomposition.md`: Gate 1 input.
- `spec.md`: scored by the spec judge per spec; also the writer's outcome rubric.
- `implementation.md`: the implementer's outcome rubric. The grader's copy ends at the marker line (`<!-- grader copy ends here`); the deterministic items below it are computed by the orchestrator and handed to the merge judge.
- `review_quality.md`: used by the merge judge to audit the reviewer, and by evals to score reviewer output.
