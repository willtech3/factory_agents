# The Factory Console (v0): writing outcomes and deciding the first gates

The Console is the `web` gate surface ([ADR-0007](adr/0007-pluggable-gate-surfaces.md)): a local web app over the run store and the file queue, for the people who write outcomes and decide Gates 1 and 2. It does not replace the tracker or the forge; it is the workbench for the stages before code exists, and the place to read a gate the way the judge meant it to be read.

```bash
cd orchestrator && .venv/bin/pip install -e ".[web]"
.venv/bin/factory web                 # http://127.0.0.1:8765/
.venv/bin/factory web --demo          # also seeds a simulated run from examples/ledgerline
```

It reads `runs/<run_id>/events.jsonl` and the artifacts, and writes decisions and answers to `runs/_queue/`, exactly where `factory decide` and `factory answer` write them. A `factory run` in another terminal (or started from the Console) picks them up unchanged. No agent, forge, or tracker call happens from the Console.

## What it shows

**Runs.** The front page opens with what is waiting on you: gates to decide and blocking questions to answer, oldest first, each with the run title and the judge's or agent's one-paragraph summary. Below it, every run with a stage track (intake, decomposing, Gate 1, specifying, Gate 2, implementing, done; gates drawn as squares because they are the human points), spend so far, and when it last moved.

**New outcome.** A form in the owner's vocabulary: what should be true when this is done, metrics with baseline and target and where they are measured, constraints, the target repository, the owner. It validates against `schemas/outcome.schema.json` on save and names what is missing. From the saved outcome you start a live run (when `factory doctor` is clean) or a demo run.

**A run.** The stage track, the outcome statement, what is waiting on you (questions answered inline; gates linked), the work items with their stage and change request, the timeline derived from the event log, and every artifact with its sha256.

**A gate.** Two columns. The document on the left, set for reading: Gate 1 opens with the decomposer's summary, then the metric-coverage table, then each work item with its intent, dependencies, and a note field; then open questions, scope, assumptions, deterministic checks, and the full `decomposition.md`. Gate 2 opens with the judge's summary and result, then the goal-coverage matrix, cross-set coherence issues, and a section per spec: acceptance criteria as given / when / then, what the judge flagged, interfaces, allowed paths, test plan, the spec as written, and a note field. Each spec has an "include in approval" checkbox for approve-subset.

The sign-off rail on the right lists the exact files and hash stubs the decision binds to ([ADR-0005](adr/0005-gates-bind-to-artifact-hashes.md)), a comment field, and the options the gate allows. Notes left under an item travel with Request changes prefixed by the item id (`wi-suspension-timing: ...`), which is how the orchestrator routes feedback. Once recorded, the rail says so and then says when the run has applied it; a decision already recorded cannot be recorded twice.

## The demo run

`--demo` (or "Save and run as a demo" on the form) replays the Ledgerline example through the real store, state machine, checks, and file queue: a blocking question from the decomposer, Gate 1 with revision 2 on request-changes, four specs and a judge verdict at Gate 2, approve-subset, change requests, completion. Every event it appends is one the orchestrator would append, so the Console shows a demo run exactly as it would a real one. It exists so the human loop can be exercised, and tested, without the platform.

## What it does not do yet

- No sessions, no streaming of agent progress: the timeline is the event log. Live runs are started as a subprocess (`factory run`) and log to `runs/_logs/`.
- No Gate 3: code review is the forge's.
- No authentication: it binds to localhost and records decisions as `console`. Put it behind something before sharing it.
- Pages for Gates 1 and 2 are also available standalone at `/gates/<id>/page` (the renderer in `surfaces/artifact_pages.py`), for pasting into a tracker or an artifact.
- A stale decision (an agent rewrote an artifact between opening and deciding) is refused by the queue, as everywhere else; the Console shows the refusal and the gate reopens with new hashes.
