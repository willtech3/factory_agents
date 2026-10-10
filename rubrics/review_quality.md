# Review quality rubric

Used by the merge judge to audit the reviewer, and by the eval harness to score reviewer output against planted-defect sets. A reviewer that scores badly here is retuned, not trusted more.

## R1. Findings are falsifiable
Every finding has a `claim`, `evidence.locations`, `violates`, and a `refutation_condition` that a competent implementer could meet or fail.
- 0: a finding missing any of these, or a refutation condition that is unmeetable ("prove there are no bugs").
- 2: all present and specific.
- 3: as 2, and blocking/major findings carry a reproduction with observed output.

## R2. Severity is calibrated
`blocking` only for criterion violations or data corruption; `major` for real defects outside criteria or security/concurrency; style is `nit`.
- 0: a style issue marked blocking, a criterion violation marked minor, or a reproduced concurrency, data-consistency or security defect marked below major.
- 2: calibrated.

## R3. Checks performed are real and sufficient
`checks_performed` lists the test plan run, probes on boundaries and failure paths, and the scope check, with methods a judge can repeat.
- 0: checks list is empty, or a claimed command produces different output when re-run.
- 1: tests run, no probes.
- 2: tests, probes, scope.
- 3: as 2, with concurrency or failure-injection probes where shared state or external calls exist.

## R4. Spec conformance table is evidence-backed
Every criterion has a status and evidence that points to a real test or probe.
- 0: a `met` with evidence that cannot be located.
- 2: all located.

## R5. Over-rejection check
Findings the implementer refuted with evidence meeting the refutation condition are marked `withdrawn`, with a reason. Findings re-asserted without new evidence count against the review.
- 0: a refuted finding upheld with no response to the evidence.
- 2: withdrawals and upholds each carry one sentence of reasoning.

## R6. Independence
No reference to the implementer's reasoning, report, or intent beyond treating the report's claims as hypotheses. No "the implementer says".
- 0: review defers to the implementer's claims as evidence.
- 2: independent.

## Eval metrics derived from this rubric (see `evals/`)
- Detection rate on planted defects by category and severity.
- False-positive rate on clean implementations (paired discrimination: flag rate on faulty minus flag rate on clean parent).
- Refutation precision: share of `refuted` responses the merge judge agreed with.
- Mean findings per review and share withdrawn after response.
