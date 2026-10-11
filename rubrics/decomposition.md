# Decomposition rubric

Scored per criterion, 0 to 3. Any 0 fails. Target for Gate 1: all criteria at 2 or above.

## D1. Coverage of success metrics
Every success metric in `outcome.json` appears in `coverage` with at least one work item, or is explicitly marked as not movable by code with a reason.
- 0: a metric is missing from `coverage`.
- 1: all metrics present but at least one has an empty or hand-wavy rationale.
- 2: all metrics covered with a rationale that names the mechanism.
- 3: as 2, and the summary says which items matter most for each metric.

## D2. Right-sized decomposition
There are as few work items as reviewability allows: one item when the outcome is one coherent change a reviewer can approve as a single pull request; more only where parts must be approved or landed separately or an item would exceed size M. Size is `estimated_lines`: hand-written changed lines, tests included, generated files excluded, against the run's budget (default XS ≤ 50, S ≤ 150, M ≤ 400). A refresh that rewrites a large generated file is its own item that lands first, unless it is small (post-mortem 2026-10-11 §3). Every item is a change to the product with a testable behaviour (vertical slice), not a layer, an investigation, or a documentation rider.
- 0: an item is not a change to the product (a "confirm current behaviour", "baseline", "investigate", or docs-only item when docs are not the outcome), or an item is a horizontal layer ("all database changes").
- 1: more items than a reviewer needs (a change a single reviewer would approve as one pull request is split into several), or an L item with no stated reason it cannot be split.
- 2: as few items as can each be reviewed as one change, all XS to M by a plausible `estimated_lines`, large regenerated data in its own item, each with an intent a tester could check.
- 3: as 2, and the summary says why this count and not fewer.

## D3. Dependency graph
`depends_on` forms a DAG; items touching the same paths are either dependent or merged.
- 0: a cycle, or two items with overlapping `likely_paths` and no dependency.
- 1: DAG, but dependencies are over-declared (a chain where a fan-out was possible) or under-explained.
- 2: DAG with minimal dependencies that match the shared-path analysis.
- 3: as 2, and the summary states the critical path.

## D4. Grounding in the codebase
Claims about current behaviour cite researcher evidence (`path:line`), and `likely_paths` exist in the repository.
- 0: a `likely_paths` entry does not exist, or a stated current behaviour is contradicted by the code.
- 1: grounded, but thin: items reference directories, not files.
- 2: each item cites at least one real path and one piece of evidence.
- 3: as 2, and the decomposition notes an inconsistency or surprise found in the code.

## D5. Explicit non-scope and assumptions
`explicitly_out_of_scope` and `assumptions` are non-empty and name things a reasonable owner might otherwise expect.
- 0: both empty.
- 1: present but generic ("performance is out of scope").
- 2: specific exclusions with reasons; assumptions a human can confirm or deny.
- 3: as 2, and each `human_judgement_required` item carries the decision the owner must make.

## D6. Questions are worth the owner's time
Open questions are ones whose answer changes the shape of the work; each has a default. No question the code could have answered.
- 0: a blocking question that the repository or outcome already answers.
- 1: questions have no `default_if_unanswered`.
- 2: every question has a default and a stated consequence.
- 3: as 2, and non-blocking questions are batched and few (five or fewer).

## D7. Readability of the summary
`decomposition.md` can be read by the outcome owner in under two minutes and matches `decomposition.json`.
- 0: summary and JSON disagree on item count, titles, or dependencies.
- 1: consistent but over 150 lines or jargon-heavy.
- 2: consistent, under 150 lines, plain language.
- 3: as 2, with a one-paragraph "what you are approving" at the top.

## D8. Constraint compliance
Nothing in the decomposition violates `constraints.must_not` or `compliance`.
- 0: a violation.
- 2: compliant and the summary says how each `must_not` was respected.
- 3: as 2, with a risk entry for each constraint that is hard to keep.
