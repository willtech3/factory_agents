# ADR-0016: Evals from recorded sessions, and receipts that gate prompt and rubric changes

**Status:** accepted · **Date:** 2026-10-12 · Phase 5 of [ADR-0013](0013-durable-runs.md).

## Context
The eval plan (`evals/README.md`) describes labelled datasets that are mostly not built. `run_eval.py` could only grade pre-produced artifacts: its `produce` was a stub. Nothing tied a change to an agent prompt or rubric to an eval. A prompt edit, including the generated rule blocks of ADR-0015, could merge with no measurement at all.

Since ADR-0013 every agent session is a durable step that records what it returned. To ask that session again of another agent version, the step also needs what the session was given. Real runs are then the cheapest dataset there is: real inputs, real outputs, and what the run later made of them.

## Decision
1. **A session step records its inputs** (`stages/common.session_inputs_record`). The record keeps:
   - the fields `session_inputs_sha` hashes: agent, title, description, rubric, limits, overrides, environment, metadata, custom tool names and repository (never its token);
   - the work item, the spec and the cap;
   - every upload by sha256, with its bytes as a blob up to 4 MiB. A larger upload, such as a hosted verification's source archive, is kept by sha256 only, and that session cannot be replayed.
2. **A run's recorded sessions are eval items** (`factory/recordings.py`). Each item carries its labels, which say what the run made of that work:
   - **implementer:** the orchestrator's own checks of its head, and where the round went;
   - **reviewer:** the merged findings, and where the round went;
   - **merge judge:** its verdict after the post-conditions;
   - **decomposer, spec writer and spec judge:** the owner's decision at the gate that followed.

   `factory eval export <run_id> --out <dir>` adds them to a dataset: `items.jsonl` and content-addressed `blobs/`. Adding the same run twice adds nothing.
3. **One grade for recorded and replayed outputs** (`recordings.grade`). It is what the pipeline does on receipt:
   - the main output exists, parses and validates against its schema (`artifact-schema`);
   - the judges keep their decision rules (`spec-pass`, `merge-ready`).
4. **`evals/run_eval.py --recorded <dir> --agent <role>`:**
   - **`--regrade`** grades the recorded outputs with today's checks.
   - **`--replay --runs N`** asks the agent version the controller is configured with each recorded session again: the same instructions and uploads, and questions answered with their defaults. It grades the outputs the same way and reports both rates as Beta credible intervals, plus how often a judge reached the recorded result.
   - **The decision rule:** a replay regresses when its interval lies wholly below the recorded versions'.
5. **Receipts gate prompt and rubric changes.**
   - `evals/receipts.json` keeps, per role, the sha256 of its prompt and rubrics and how they were last cleared:
     - `replayed`: `run_eval.py --replay --receipt` on a replay that did not regress, with its summary;
     - `waived`: `factory eval waive --agent <role> --reason ...`, with who decided and why.
   - CI runs `evals/gate.py` in the lint job. It fails while any role's prompt or rubric bytes differ from its receipt. A rule change in the registry changes the generated prompt blocks, so it is gated too.
6. **The baseline is waived, and says so.** No recorded dataset existed when receipts were introduced, so every role starts with a waiver whose reason states that. The first replay against a recorded dataset replaces it.

## What this phase did not do
- **CI does not replay sessions itself.** A replay calls the model, costs money and needs credentials that CI does not hold. CI checks that a receipt exists for the exact bytes; the replay runs on a machine with the owner's key.
- **The kickoff descriptions in Python** (`stages/*.py`) are not under receipts, only `agents/*.md` and `rubrics/*.md`. They change with code that is reviewed and tested as code.
- **A replayed session gets no repository mount.** Sessions that read `/workspace/repo` are replayed from their uploads alone. Their grade is about the artifact's shape and decision rules, not about the code they would have read.
- **The labelled datasets of the eval plan** (hidden test suites, planted defects) are still the measure of quality. Recordings measure regression against what the factory did before.

## Consequences
- A prompt or rubric change cannot merge on nobody's say-so: it carries either a replay summary or a reason, in the diff.
- Every run adds eval material for free. Recordings hold repository content and owners' notes, so they stay with the runs, never in this repository (`evals/recordings/` is ignored).
- Session steps grow by one blob per session plus the uploads under 4 MiB, deduplicated by sha256 across sessions (a spec uploaded to five sessions is stored once).
