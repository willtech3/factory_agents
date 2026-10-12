# ADR-0013: Durable runs: a database per run, and agent sessions as steps a new controller can follow

**Status:** accepted (phase 1 built) · **Date:** 2026-10-12 · Refines [ADR-0001](0001-orchestrator-driven-stage-sessions.md).

## Context
A run is a Python call stack. The stages are nested function calls, and each one blocks on its agent sessions and gates. When the controller process dies, everything the stack knew is gone. The only record is the event log, and that log was written in a way that broke:

- **An illegal event made the run unreadable.** `append` wrote first and `state` reduced on read, so one bad write raised on every later read, in the Console, `factory resume` and `factory replay` alike (run_8d9b17b4f5).
- **A crash threw away paid-for work.** `_drive` interrupted every session in flight when the controller raised, and `factory resume` started those sessions again from scratch. Live run 1 lost five sessions this way. A hung `sessions.create`, a dropped stream after its reconnects, or a bug in a stage all ended in the same place.
- **Resume was code per stage.** `resume_points`, `pending_gate2`, `_persisted_specify_rounds` and the budget-pause paths each rebuild "where was I" from artefacts, and each new stage needs another one.
- **Concurrent writers used an flock and a cached sequence number.** The spec writers and reviewer passes append from worker threads, and the Console and `factory resume` from other processes (post-mortem 2026-10-10, issue 2).

Durable execution engines (Temporal, Restate, DBOS, Inngest) all use the same pattern: every side effect is a step with a durable key, a step records what it started before it waits, and a restarted worker replays the log to the step it was on. They don't record the stack. We don't need an engine. We need that pattern on SQLite, because a run is one machine's work, and the Console and evals read the same file.

## Decision
1. **One SQLite database per run** (`runs/<run_id>/run.db`, WAL). It holds two tables:
   - `events`: the same events as before.
   - `steps`: key, kind, status, attempt, inputs_sha, session_id, result, error.
   
   Artefacts stay as files. Blobs are content-addressed (`runs/<run_id>/blobs/<sha256>`).
2. **Events are validated when they are written.** `append` folds the new event into the run's state inside one `BEGIN IMMEDIATE` transaction. When the reducer refuses it, nothing is written and the caller gets the `IllegalTransition`. SQLite serialises writers across threads and processes, which replaces the flock.
3. **Older runs are imported, not migrated.** The first open of a run that has only `events.jsonl` imports it as it is, once, and renames it `events.imported.jsonl`. `factory export-events` writes the JSONL form back out, which is what the eval reference runs use.
4. **Every agent session is a step** (`stages/common.session_step`, which every session goes through via `run_session`):
   - **Key.** `session/<stage>/<prefix><title>#<attempt>`. Titles already name the run, the stage, the spec and the round. `attempt` is the budget re-run.
   - **Inputs hash.** Everything that decides the work: agent, description, rubric, limits, model and system overrides, environment, vaults, repository, metadata, schemas, custom tool names, and each upload's sha256.
   - **The id is recorded before anything waits.** `SessionDriver.run(on_created=...)` hands the new session's id to the step before the driver follows the session.
   - **Reattach.** A call that finds its step `running` with a session and the same inputs follows that session (`run(attach_session_id=...)`) instead of creating one. The driver's drain already catches up on history first, so a session that finished while nobody watched returns at once. One still working is followed to its end.
   - **Supersede.** A running step whose inputs changed has its session interrupted. It is charged (`session.finished`, stop reason `superseded`) and recorded as `session.superseded`, and a new session starts.
   - **Stopped sessions are not followed.** A session that a stop (SIGTERM, SIGINT) interrupted already has a `session.finished` with stop reason `interrupted` or `unreachable`. Its step is failed, and a new session starts.
   - **No session, failed step.** A step whose session was never created (the create call failed) is failed, and the error is kept.
   - **Order of the last two writes.** `session.finished` is written before the step closes. A crash between the two reattaches to a session that already returned, and the spend is counted once (below).
5. **A crash leaves sessions working; a stop interrupts them.** A stop is someone deciding the run should not spend. A crash is the controller's failure, not the sessions'. `run.failed` after a crash lists `sessions_left_running`, and each one keeps working under its own cap. `run.resumed` lists `sessions_to_follow`, which are the steps still running with a session. The resumed pipeline reattaches to each one when it reaches its step again.
6. **A session's spend counts once.** `RunState.finished_sessions` maps a session id to its stop reason, and the reducer ignores a second `session.finished` for the same id. `record_session_finished` doesn't write one either.
7. **Tool handlers are idempotent on replay.** A reattached controller replays the session's unanswered custom tool calls:
   - `ask_human` finds an earlier question by its `custom_tool_use_id`. It returns the answer when there is one, and otherwise waits on the open question until its original deadline. It never asks twice.
   - `open_change_request` returns the change request already opened for the branch. The forge never gets a second one.
8. **Sessions nobody will follow are closed when the run ends.** A resumed run may take another path, such as an escalation or a different decision at a gate. Once the run is terminal, `close_orphaned_sessions` interrupts and charges every session still running under a step. A run that only paused keeps them for the resume that follows.
9. **A finished step is not reused (yet).** Whether a finished judgement or verification may stand is the caller's decision. The merge judge reuses only a ready verdict on unchanged inputs, and a build recovery wants fresh evidence (#47). A finished step records how its session ended, plus its outputs as blobs named by sha256, so phase 2 can decide reuse per step kind with the data already in place.

## Phases
Each phase deletes code that the previous one made redundant.

1. **Database and session steps** (this ADR, built). Events in SQLite with write-time validation, durable session steps, reattach, supersede, idempotent tool handlers, crash leaves sessions working.
2. **Gates and checks as steps; a reconciler replaces the resume paths.** Gate waits, deterministic checks and verification become steps with recorded results. `factory resume` replays the pipeline and returns recorded results for finished steps whose inputs are unchanged. This removes `resume_points`, `pending_gate2`, `_persisted_specify_rounds` and the per-stage budget-resume paths. Reuse rules per step kind live here.
3. **One verification pipeline.** `verify(target, plan) -> EvidenceBundle` and `judge(bundle, spec)`, with structured test plans and the recipe split into what is trusted and what is runtime.
4. **A rule registry.** The rules the prompts state, the checks enforce and the rubrics score come from one registry, so they can't drift apart. Prompts are generated from it, and outputs are schema-validated when submitted.
5. **Evals from recordings.** The recorded steps of real runs (inputs by sha256, outputs as blobs) become the replay dataset. A prompt or rubric change is gated on it.
6. **The Console as a projection.** It reads the step table as well as the events: live sessions, what a resume will follow, and the cost per step.

## Consequences
- A controller crash costs the time to notice it and run `factory resume`, not the sessions in flight. A session left running is bounded by its own cap, as before.
- A run's database is the run. Copying `runs/<run_id>/` copies it, and `factory export-events` gives the old JSONL for tools that want it.
- Session titles are now part of a step's identity. A title that includes something nondeterministic (a timestamp, a random id) would never reattach. It would supersede, which is safe and costs a session.
- Uploads are hashed into the inputs. An input artefact regenerated with different bytes (a fresh `produced_at`) supersedes rather than reattaches.
- A session left running by a crash whose run is never resumed is followed by nobody and charged to nobody until the run is resumed or closed. This is the same exposure as before, minus the interrupt.
