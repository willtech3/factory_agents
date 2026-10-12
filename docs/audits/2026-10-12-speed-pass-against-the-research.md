# The speed pass against the research: October 12, 2026

What this checked: every change in the speed pass (the PR that adds `factory timing`, sizes review to the change, opens PRs in code and scopes spec re-judgements) against [design-principles.md](../design-principles.md), [academic-2026.md](../research/academic-2026.md) (A) and [practitioners.md](../research/practitioners.md) (P). The second part lists drift that was already in the factory before the pass. It is not fixed here.

## The changes

| Change | What the evidence says | Verdict |
|---|---|---|
| One reviewer pass when a first round changes at most 150 hand-written lines; two above that | Principle 5 asks for one fresh-context pass per round. A §2 (Song): a single isolated pass beats multi-turn review. A §2 (Qiu and Gill): minimal, mandated disagreement beats bigger teams. A §2 (Bertalanic and Fortuna): same-model multiplicity costs tokens for equal or worse accuracy. P §1f: slices of 100 to 200 lines | **Aligned.** This is closer to principle 5 than the old default of two passes. Still open: [06-evaluation.md](../06-evaluation.md) asks whether a second pass adds detection, and nothing has measured it yet. Above 150 lines nothing changed, because live run 2's two passes found complementary defects |
| Later rounds: one fresh pass adjudicates each previous finding by id against its refutation condition, then reviews only the delta | A §2 (Song): extra rounds add 0.08 recall and 62% more false positives, and drift toward critiquing the conversation. A §2 (Parmar et al.; Jin and Chen): critiques must be evidence-gated | **Aligned.** The session is still fresh and the refutation protocol (ADR-0004) is unchanged. The merge judge still reads the whole diff. Remaining risk: a defect outside the delta lines that the delta exposes (A §5, SWE-Touch). The reviewer is told to report behaviour the delta breaks, and both the reviewer and the orchestrator run the full test plan at the head |
| The implementer's in-session grader gets only the criteria it scores | Principle 3 (the orchestrator computes the deterministic items); principle 17 and P §1d (instruction budgets); ADR-0002 | **Aligned.** This implements the cut that `rubrics/README.md` already documented and the code skipped |
| Hosted verification runs on `claude-haiku-5-5` at low effort | Principle 4 (tests are run by the orchestrator). Principle 20 and P §15 (re-tune and compare on every model change). P §2 (Cognition): a weak model failed when it had to judge | **Mostly aligned.** The session makes no judgement; it runs one script. The swap was made without an eval, which drifts from principle 20. Fixed in this pass: each verification record now names the model that ran it (`session_model`), so runs before and after compare, and `FACTORY_VERIFY_MODEL=agent` reverts the swap |
| The controller opens the change request; the integrator only rewords a draft the lint refuses | P §1b (12-Factor: own your control flow; small, focused agents). P §3 and §11: remove harness pieces as they stop earning their place. A §2 (Kim and Lee): keep the step that makes a change legible separate from the step that makes it correct | **Aligned.** Unchanged: the forge receipt is still the only proof a PR exists, and the PR still opens at the judged head |
| A spec re-judgement gets per-spec diffs and re-scores only what changed | A §2 (Song): re-reviewing unchanged material adds false positives. A §1 (Soumik): normalise the inputs, and the diffs are of normalised JSON. Against it: A §1 (Yagubyan; RuVerBench): one verdict is noisy and voting helps. P §3: evaluators are lenient | **Aligned, with one caveat.** A lenient round-1 pass is carried forward instead of getting a second look. Scores below 2 and every raised issue are still re-scored, and the judge still reads every spec in full |
| Gate pre-approval (`FACTORY_AUTO_APPROVE`) | Principles 1 and 14. [roadmap.md](../roadmap.md) defers "tiered or automatic approval of any gate" until there is gate telemetry. P §1c and §1f: front-loaded human alignment is the lesson of the lights-off reversal, because plans that read well hid wrong assumptions. A §6 (Choudhuri et al.): developers delegate design-oriented work least. Implication 18: reserve human decisions for design-defining choices. A §1 (Yagubyan): one judge call | **Drift as first written, fixed here.** The first commit offered it as a Console checkbox for any run, which contradicted the roadmap and the strongest practitioner finding. It is now limited to benchmark and evaluation runs started from the CLI, which is what post-mortem issue 20 proposed. Every policy decision is recorded as `decided_by: policy`, so those runs start the gate telemetry the roadmap asks for. Gate 3 was never touched |
| `factory timing <run_id>` | A §7 (Patel et al.; Jarmak), A eval implication 9: attribute results to pipeline components and keep raw trajectories. P §10 (Hashimoto): engineer away the mistakes you observe | **Aligned** |

Found while checking, and fixed in this pass: `e2e_coverage` is a property of the spec, and [01-pipeline.md](../01-pipeline.md) says it runs before the judge, but the code ran it only at merge (principle 21). At merge, an approved spec that lacks an e2e entry fails a check no implementer round can fix, so the run takes extra rounds and then escalates. The check now runs at the spec stage.

## Drift that predates this pass

These are not fixed here. They belong to the structural work, and most sit in the measurement half of the research, which the factory has not built yet.

1. **Each judgement is one call (principle 16; A §1, Yagubyan, RuVerBench, Norman et al.).**
   - Gate 2 and merge readiness each rest on one judge session. There are no repeated votes and no kappa against human labels.
   - The spec judge scores every spec in one batched session.
   - The research says to treat a single verdict as a soft signal and to verify rubric items one at a time with voting.
2. **No acceptance oracle the implementer cannot see in a production run (principle 6; A §2, Guo et al.; A §4, Destefanis and Aste).**
   - The hidden suites live in `evals/`, and `evals/run_eval.py::produce` does not drive agents.
   - In a run, the implementer writes the tests that its own criteria map to.
   - The reviewers' probes are the only independent oracle.
3. **No eval gates a prompt or model change (principle 20; P §15).**
   - About 35 commits changed agent prompts with no eval result. The reviewer and decomposer prompts are at version 7.
   - Prompts are tuned through live runs and post-mortems.
4. **Judges share the implementer's model (principle 2; A §1, Yang et al.).**
   - The implementer, both reviewers, the merge judge and the spec judge all run Opus 5.5. The security reviewer (Fable 5.1) and the spec writer (Sonnet 5.5) differ.
   - The principle asks for a different tier where possible, or a decomposed rubric.
5. **The authoritative test run in managed mode happens inside an agent session (principle 4; A §1, Mohammadi; A §4, Destefanis and Aste).**
   - The binding check compares the hashes of head, recipe, source and spec. It does not show that the session left the harness's outputs alone.
   - Checking the session's tool calls (one harness run, no writes to outputs) would close the gap. That checks the trajectory, not only the result.
6. **The instruction budget keeps growing (principle 17; P §1c, §1d, §11).**
   - The agent prompts total about 12,000 words, and the security reviewer's alone is about 2,900.
   - Every rule traces to a failure, as principle 17 asks. But nothing removes a rule when the failure stops recurring, which P §3 and §11 also ask for.
