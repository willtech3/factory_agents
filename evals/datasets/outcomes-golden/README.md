# outcomes-golden

Business outcomes with expert-validated decompositions. Used to evaluate the Decomposer (L1) and as the seed for `spec-defects` and `spec-from-item`.

## Items
- Target size: 30 to 50. Start with 12 (enough for a paired comparison with credible intervals; grow as production runs accumulate).
- Each item: `outcome.json` (schema-valid), a target repository snapshot (commit sha of a real or synthetic repo), and a **reference decomposition** written by a senior engineer and product owner together.
- Stratified by difficulty: `simple` (1 to 3 work items, one repo area), `medium` (4 to 8 items, cross-cutting), `hard` (needs product decisions, external systems, migrations).
- Half should contain at least one deliberate ambiguity that a good decomposer must surface as a question or assumption. Labelled in `expected_questions`.

## Labels
Not reference similarity. Per DELTA (Ly et al., ASE 2026), text similarity to a reference does not track expert preference. Instead:
- Two humans score the agent's decomposition per rubric criterion D1 to D8 on a 10% sample each eval run; kappa reported.
- A calibrated LLM judge (see `graders/stochastic.py`) scores the rest, with few-shot anchors drawn from the human-scored sample.
- Binary labels that are cheap and exact: every success metric covered (yes/no), DAG valid (yes/no), expected question surfaced (yes/no), constraint violated (yes/no).

## Splits
- `dev` (40%): used while tuning the decomposer prompt and rubric.
- `test` (60%): touched only for the headline number. Contamination guard: items never appear in skills, memory stores, or prompts.

## Sample row (`items.jsonl`)
See the file. Fields: `item_id`, `difficulty`, `outcome` (inline), `repo` {url, sha}, `reference_decomposition_path`, `expected_questions` [], `must_cover_metrics` [], `forbidden` [].
