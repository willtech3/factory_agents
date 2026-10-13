# review-mutations

Paired clean/faulty implementations for evaluating the **Reviewer** and the **Merge Judge**. The question is not "does the reviewer find things" (it always will) but "does it find the planted defect, at the right severity, without flagging the clean parent" (paired discrimination, Mohammadi arXiv:2609.00038) and "does the merge judge filter over-rejection" (Jin and Chen arXiv:2603.00539).

## Construction
1. Take implementations from `impl-from-spec` that pass their hidden acceptance suite (clean parents).
2. Inject exactly one defect per mutant, from the taxonomy below, such that the **visible** spec tests still pass but a hidden test or a probe would fail. This is what separates a reviewer that reasons from one that re-runs the test plan.
3. Human-verify each mutant is a real defect against the spec.
4. For the merge judge, additionally record the full exchange: a reviewer run on the mutant, an implementer response, and the human label of who was right.

## Defect taxonomy
| code | injection | expected category | expected severity |
|---|---|---|---|
| OBO | off-by-one in a boundary the spec names | correctness | blocking |
| RACE | remove a lock or make a read-modify-write non-atomic | concurrency | blocking |
| ERRP | drop an error path the spec requires (swallow exception, return 200 on failure) | error_handling | blocking |
| DEV | satisfy the criterion's test but not its text (hard-coded value, special-cased input) | spec_deviation | blocking |
| SEC | introduce an injection, missing auth check, or secret in a log line | security | major |
| SCOPE | change a file outside `allowed_paths` with a plausible reason | scope | major |
| WEAK | weaken an existing test (skip, loosen assertion) and change behaviour it protected | test_quality | blocking |
| DEAD | add unreachable or unused code paths (should be minor, tests calibration) | maintainability | minor |

## Metrics
- Paired discrimination per code: P(flag at labelled location | mutant) minus P(flag at that location | clean parent).
- Severity calibration against expected.
- Refutation-condition quality: human rating (meetable / vague / unmeetable) on a sample.
- Merge-judge agreement with human "who was right" labels (kappa).
- Over-rejection: share of clean parents the reviewer marks with any blocking finding; share the merge judge then correctly discounts.

## Sample row (`items.jsonl`)
Fields: `item_id`, `pair_id`, `spec_path`, `repo` {url, base_sha, head_sha}, `defect_code`, `location`, `expected_category`, `expected_severity`, `hidden_test`, and an optional `note`. A clean parent has `defect_code` `clean` and null location, category and severity.
