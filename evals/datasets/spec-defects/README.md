# spec-defects

Labelled specifications for evaluating the **Spec Judge**: does it catch planted defects, and does it leave clean specs alone? This is the dataset that tells us whether Gate 2 is being fed signal or noise.

## Construction
1. Take passing specs from `outcomes-golden` runs (clean set).
2. For each clean spec, create one mutated copy with exactly one planted defect from the taxonomy below. Mutations are written by a human or by a model and then **human-verified** to be real defects (a mutation that is arguably fine is discarded).
3. Keep the clean parent and the mutant as a pair so paired discrimination can be computed (Mohammadi, arXiv:2609.00038).
4. For set-level defects (interface mismatch, overlap, gap), the item is a *set* of specs with one planted inconsistency.

## Defect taxonomy
| code | defect | rubric criterion it should trip |
|---|---|---|
| AMB | an acceptance criterion with two reasonable readings | S2 |
| UNT | a criterion that cannot be verified ("the code is clean", "fast enough") | S2 |
| INC | goal needs a case no criterion covers (e.g. the error path) | S3 |
| IFV | interface named without a definition, or type/optionality missing | S4 |
| SCP | `allowed_paths` with a root-level `**`, or includes existing tests without reason | S5 |
| GRD | `context` cites a path that does not exist in the repo | S6 |
| TST | a test plan command that does not exist, or a criterion without a planned test | S7 |
| DEP | `dependencies.specs` contradicts the decomposition's `depends_on` | S8 |
| MIS | set-level: spec A produces `billing.retries_exhausted`, spec B consumes `billing.final_failure` | coherence.interface_mismatches |
| OVL | set-level: two specs modify the same file with no declared dependency | coherence.overlaps |
| GAP | set-level: a decomposition work item has no spec | coherence.gaps |
| CON | set-level: two specs assert contradictory behaviour | coherence.contradictions |

## Labels
`defect_code` (or `clean`), `location` (spec_id and section or AC id), `expected_severity` (blocking/major/minor), `pair_id` linking mutant to parent.

## Metrics
- Recall by defect code (did the judge report an issue at the labelled location with severity at or above expected).
- False-positive rate on clean parents: share of clean specs where the judge reported a blocking or major issue.
- Paired discrimination per code: flag rate on mutant minus flag rate on parent.
- Severity calibration: confusion matrix of expected vs reported severity.
- Position-swap consistency: judge the same set with specs in a different order; report disagreement rate.
- Over 11 votes per item (Yagubyan 2606.13685): majority verdict and vote entropy.

## Sample row (`items.jsonl`)
Fields: `item_id`, `pair_id`, `kind` (single|set), `spec_paths` [], `decomposition_path`, `outcome_path`, `defect_code`, `location`, `expected_severity`.
