# ADR-0015: One rule registry for the prompts, the checks and the judges

**Status:** accepted · **Date:** 2026-10-12 · Phase 4 of [ADR-0013](0013-durable-runs.md).

## Context
The same rules were written out in up to five places, and the copies drifted apart:

- the agent prompts (`agents/*.md`);
- the kickoff strings in Python;
- the rubrics;
- the deterministic checks and the fix texts of their findings;
- the platform skill.

Examples:

- **"No factory proof code in the diff"** appeared in more than twelve places.
- **The fix text for a failed check** lived in a dict inside `deterministic_findings`, away from the rule it fixed.
- **The judges' decision rules were prose only.** The prompts say "pass means no criterion scored 0" and "ready means every criterion met", but only one of them was enforced in code: a failed deterministic check downgraded `ready`.
- **Agent outputs were parsed but never validated.** Implementer, reviewer and judge outputs went unchecked, and only the decomposition and specs were. So the test fakes wrote invalid reports, findings and verdicts, and the tests still passed.

## Decision
1. **`factory/rules.py` is the registry.** Each `Rule` has:
   - an id that findings and checks cite;
   - the text agents read;
   - how it is enforced:
     - `check`: a deterministic check no judge can override;
     - `judge`: a judge's decision rule, enforced as a post-condition in code;
     - `advice`: practice the orchestrator cannot check;
   - the roles it binds;
   - for a check rule: the CheckResult names that enforce it, the fix a failed check tells the implementer (with a hosted variant), and the finding category;
   - the implementation-rubric item it is, if any.
2. **Prompts and the rubric hold generated blocks.**
   - Each agent prompt the registry binds (`rules.AGENT_ROLES`) ends in a `## Rules` block rendered for its role.
   - The deterministic part of `rubrics/implementation.md` is rendered from the check rules. It stays below the grader marker, so the in-session grader still never sees it.
   - `factory rules` re-renders the blocks; `factory rules --check` exits 1 on drift.
   - The test suite fails on drift. Hand-written prose that restated a rule now cites its id instead.
3. **Every check has a rule, and every check rule has a check that runs.** A test builds every check the pipeline produces (merge, spec and decomposition) from the functions that produce them, and compares that set with the registry.
4. **Failed checks cite the registry.** A `DET-*` finding's `violates` names the rule, and its `suggested_fix` and `category` come from it.
5. **The judges' decision rules are post-conditions in code.**
   - **`spec-pass`:** a spec judge `pass` with a criterion scored 0 or a blocking issue becomes `revise` (`rules.spec_pass_violations`).
   - **`merge-ready`:** a merge judge `ready` becomes `another_round` when a deterministic check failed or an acceptance criterion is not `met` in its criteria table (`rules.merge_ready_violations`).
   - Each override says which rule and why in `summary_for_human`.
   - The judge's re-rating of standing findings stays its own; no code can redo it.
6. **Agent outputs are validated on receipt (`artifact-schema`).**
   - **Reviewer and security-reviewer findings, the merge judge's verdict, and the spec judge's verdict** are validated against their schemas after the controller stamps their provenance. An invalid one is refused (`reject_output`): its session step fails, and a resume starts a new session for that work only. Nothing the schema does not hold reaches a gate, the merge judge or the change request.
   - **The implementer's report and review response** are validated too, but an invalid one is a failed check (`report_schema`), not a refused session. The branch and its work are good; the next round answers the finding like any other.
   - **During the session,** agents validate their own outputs with the uploaded `validate.py`, against the same schemas.

## What this phase did not do
- **No `submit_artifact` custom tool.** It would validate an output mid-session in the controller instead of with `validate.py`. It needs every agent redeployed with a new tool, and it would add an output channel beside `/mnt/session/outputs/` that a reattaching controller cannot replay. The receipt check covers what reaches the pipeline.
- **No deployment by content hash.** Agents are still deployed with `ant apply`, and the controller does not refuse a deployed version that differs from `agents/*.md`. This change edits the prompts, so they need `ant apply` with the code.
- **The per-mode preview clauses in the kickoff strings stay.** They describe each verification host's mechanics, not rules.

## Consequences
- A rule changes in one place. `factory rules` updates the prompts and the rubric, and the test suite refuses a commit that forgets to.
- A real agent that writes an invalid verdict or set of findings now fails its step instead of passing garbage downstream. Before, those artifacts were trusted as parsed.
- The test fakes write schema-valid outputs (`tests/support/artifacts.py`), so a test that passes says something about outputs a real agent could have written.
