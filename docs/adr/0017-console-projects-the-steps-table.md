# ADR-0017: The Console reads the run as its steps table shows it

**Status:** accepted · **Date:** 2026-10-12 · Phase 6 of [ADR-0013](0013-durable-runs.md).

## Context
Since ADR-0013, a run's database has two records of what happened. The event log says what happened when. The steps table says what each unit of work is: running, done or failed, which session it holds, what it cost, and what a resume will do with it.

The Console read only the events. It rebuilt live sessions from `session.started`/`session.finished` pairs and the `sessions_to_follow` of `run.resumed`. It could not say:

- where a resume would continue;
- which of the sessions a dead controller left working a resume would follow, and which it would start again;
- how many finished sessions a replay reuses without asking;
- what each pipeline step cost.

Those are the questions an owner looking at a stopped run asks, and the steps table answers each of them directly.

## Decision
1. **`factory/projection.py` projects the steps table.** It is a pure function of the steps, the run's finished sessions and its session events:
   - **The pipeline.** Every pipeline step in the order it started, nested under the step it runs in: decomposition and spec passes, gates, approvals, each spec's build, its rounds and implementer, its merge judgements, its pull request. Each step has its status and attempts, its sessions, its own cost, and its cost rolled up from the steps under it. Bookkeeping sub-steps (a judgement's artefact name) are left out.
   - **Live sessions.** Session steps still running with a session. With a controller alive they are being followed. Without one, they are working under their own caps and a resume follows them.
   - **What a resume does:**
     - it continues at the deepest unfinished pipeline steps;
     - it follows the live sessions;
     - it starts again the sessions a stop interrupted;
     - it reuses every finished session.
   - **Cost:** per step, and per stage in total. It equals the run's recorded spend once every session has finished.
2. **The Console reads it.** `/api/runs/<id>` carries the projection as `steps`. The run page gains a **Steps** card that opens on its own while sessions are live:
   - the live sessions, with links to the Anthropic Console;
   - what a resume would do;
   - the pipeline as a table: step, status, sessions, cost; on a phone, step, status and cost;
   - the cost by stage.

   The Resume box says what the resume will do, from the same projection, instead of a fixed sentence.
3. **`factory steps <run_id>`** prints the same projection (`--json` for the whole of it).

## What this phase did not do
- **The run status, the crew roster and the story still read the events.** They describe who did what and when, which is the event log's job. Only the questions the steps table answers better moved.
- **A running session's cost is not shown.** The platform reports it when the session ends.

## Consequences
- An owner sees, before pressing Resume, exactly what it will cost them: which sessions it follows and which it starts again.
- A run from before ADR-0013 has no steps. The card is absent and `factory steps` says why.
