# ADR-0013: Durable runs: a database per run, and agent sessions as steps a new controller can follow

**Status:** accepted (phases 1 and 2 built, including 2b) · **Date:** 2026-10-12 · Refines [ADR-0001](0001-orchestrator-driven-stage-sessions.md).

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
9. **A finished step records its outputs.** It keeps how its session ended, plus its outputs as blobs named by sha256. Phase 2 reuses them (below).

### Phase 2: the pipeline is steps, and resume replays it
10. **Every unit of the pipeline is a step with a recorded result** (`steps.run_step`). The steps are:
    - `decompose#n` and `gate_1#n` for each pass of Stage 1;
    - `specify#n` and `gate_2#n` for each pass of Stage 2;
    - `approve#n` for the Gate 2 approval;
    - `implement/<spec>` and `integrate/<spec>` for each spec.

    A step that finished with the same inputs returns its recorded result without running. Results go through the same encoding on the live path and on replay, so both see the same values. Large results, such as the specs and the decomposition, are blobs, kept in key order, so a spec written back out is the bytes the owner was shown.
11. **`resume` is a replay.** It settles what is pending: a build-recovery decision, a budget gate, or a new cap. It records `run.resumed` with the stage the run stopped in (`runner.stage_to_continue`), then runs `pipeline`, the same function `run` uses, from the top. Every finished step returns its result, so the run continues at the first step that did not finish:
    - a crash mid-decomposition continues that decomposition;
    - a stop at Gate 1 waits on that gate;
    - a budget pause in the spec pass continues that pass;
    - an escalated build continues once the owner recovers it.

    Between steps the pipeline only computes from earlier steps' results. A gate's step records the feedback as the owner wrote it then, because tracker comments can change, so a replay reaches the same steps with the same inputs. The run's controller lock is held for the whole replay.
12. **A gate is found, not reopened.** `open_gate_and_wait` identifies a gate by the step that opens it, its kind, its work item and the exact artefacts it presents (`gate_identity`):
    - a gate found decided returns the recorded decision;
    - a gate found open is waited on again, and the surface's record must match the log;
    - only a gate not found opens.

    A step that runs again and presents different artefacts supersedes the gate it opened before. The reading page is published only when a gate opens.
13. **Finished sessions are reused when the same step asks for the same work.** That is what makes a replayed step that did not finish cheap: a spec pass paused before its judge gets its writers back and runs only the judge. Three exceptions:
    - Hosted verification always runs again, because evidence is never replayed (#47).
    - A session that did not finish is not reused.
    - A session whose output a stage refused (`reject_output`: a missing or invalid file) is not reused either. Its step is failed, so the replay starts a new session for that work only.

    Budget reservations count only the sessions that will start, so a replay never pauses for work already paid for. A session's step key includes its pipeline step, because the second decomposition pass asks the same agent under the same title. Its inputs hash leaves out the dollar cap, which bounds the work but does not change it.
14. **A build recovery reruns only that spec's implementation.** The latest recovery decision on a spec is an input of its `implement/<spec>` step. A recheck verifies the last head afresh and asks a new merge judge. A request for changes sends the round it names back to the implementer with the owner's instructions, and that corrective cycle stays authorized through later rechecks. Specs that finished, or already have a change request, are not touched.
15. **Older runs are adopted, not migrated.** A run an older controller wrote has no pipeline steps. `legacy_adoption` records the steps that controller could itself have continued from, and refuses the rest with the reason:
    - From a Gate 2 approval record: the approved contracts, each still the bytes it approved.
    - From an approval it had not acted on: the decision.
    - From a Gate 2 it stopped idle at: the artefacts it presented, each checked against the sha256 the owner was shown. Nothing else may follow the gate in its log.

    The rest are older runs stopped mid-design, and those are refused: start a new run from the outcome.

What phase 2 deleted:
- `pending_gate2`, `PendingGate2`, `_resume_pending_gate2`, `wait_for_gate`;
- `_resume_implementation`, `_resume_after_budget_pause`'s per-stage branches;
- `_persisted_specify_rounds`, `_pending_gate1_feedback`, `_pending_specify_inputs`;
- the `pending` path through `_from_specify`.

### Phase 2b: rounds are steps
16. **Each implement/review round is a step, and so is the merge judgement.** Inside `implement/<spec>`:
    - `implement/<spec>/r<n>` is round `n`: the implementer, the orchestrator's own verification of the head it reported, and the review unless the round goes straight to the judge. Its result is what the next round and the judge need: the report, the response to the last findings, the journeys, the head and base, the deterministic checks, the merged findings, and which way the loop goes next. Larger parts are blobs.
    - `implement/<spec>/r<n>/author` is the implementer's session inside it, so a round that died after its implementer reported verifies that report instead of asking again.
    - `implement/<spec>/judge#<k>` is the merge judgement, with `k` the number of the owner's recovery decisions on the spec. Its inputs are the final round's result and whether it is a recheck. The judge's artefact name is chosen in a sub-step (`.../name`), so a judge interrupted after it was named keeps its name.

    A round's inputs are what it answers: the findings and response it carries, the journeys, the owner's notes, and whether its review was already spent. A replay gets the rounds that finished back and continues at the first that did not. Verification runs again only inside a round that did not finish, never for a recorded one.
17. **Recovery decisions are routed by round, not reconstructed.** A recheck (approve) gets every round back from its step and asks a new judge, which verifies the final head again. A request for changes on round `m` turns round `m`'s judge-bound result into round `m + 1` with the owner's findings and instructions, and allows one more review. A round whose implementer wrote `blocked.md` raises `BuildBlocked`; its step is failed, so the decision that follows runs that implementer again.
18. **An older controller's rounds are adopted from their artefacts** (`adopt_rounds`), once, the first time a run without round steps builds the spec. Every round with a report and merged findings becomes a finished round step. The last round becomes only its author step, so its verification and review run again, unless a corrective decision names it. A round report without the previous round's merged findings is a store the loop cannot continue from, and the build stops with the reason.
19. **The step holds a new session's id before `session.started` is written.** `SessionDriver.run` hands the id to the step first, so a controller that dies in between leaves a session its step names and the next controller follows it.

What phase 2b deleted: `resume_points`, `ResumePoint` and the `resume_from` path through `run_implement_one`.

## Phases
Each phase deletes code that the previous one made redundant.

1. **Database and session steps** (this ADR, built). Events in SQLite with write-time validation, durable session steps, reattach, supersede, idempotent tool handlers, crash leaves sessions working.
2. **The pipeline as steps; resume is a replay** (built, decisions 10–15). Decompositions, gates, spec passes, approvals, implementations and integrations are steps with recorded results, and `factory resume` replays the pipeline. Its second part (built, decisions 16–19) makes each implement/review round and each merge judgement a step, which removed `resume_points`.
3. **One verification pipeline** (built, [ADR-0014](0014-one-verification-pipeline.md)). `verify(target, plan) -> EvidenceBundle` and `judge(bundle, spec)`, with structured test plans and the recipe split into what is trusted and what is runtime.
4. **A rule registry.** The rules the prompts state, the checks enforce and the rubrics score come from one registry, so they can't drift apart. Prompts are generated from it, and outputs are schema-validated when submitted.
5. **Evals from recordings.** The recorded steps of real runs (inputs by sha256, outputs as blobs) become the replay dataset. A prompt or rubric change is gated on it.
6. **The Console as a projection.** It reads the step table as well as the events: live sessions, what a resume will follow, and the cost per step.

## Consequences
- A controller crash costs the time to notice it and run `factory resume`, not the sessions in flight. A session left running is bounded by its own cap, as before.
- A run's database is the run. Copying `runs/<run_id>/` copies it, and `factory export-events` gives the old JSONL for tools that want it.
- Session titles are now part of a step's identity. A title that includes something nondeterministic (a timestamp, a random id) would never reattach. It would supersede, which is safe and costs a session.
- Uploads are hashed into the inputs. An input artefact regenerated with different bytes (a fresh `produced_at`) supersedes rather than reattaches.
- A session left running by a crash whose run is never resumed is followed by nobody and charged to nobody until the run is resumed or closed. This is the same exposure as before, minus the interrupt.
- Any stopped, failed or paused run can be resumed, not only one that reached implementation or stopped idle at Gate 2. A decision a dead controller had not acted on is carried out. A `--budget` given while a gate is pending simply raises the cap.
- A replayed step that runs a session again under the same round name, because its inputs drifted (a different skill file or preview mode after a controller upgrade mid-step), would write different bytes under a name already stored. The store refuses that (`ArtifactConflict`), and the resume fails loudly instead of mixing two versions. Finished steps are never re-run, so this only reaches a step that was interrupted.
- A step that runs again repeats its own informational events, such as a `work_item.stage` it re-enters or an artefact it stores again with the same bytes. The state is unchanged, because those transitions are idempotent. Gates, sessions and change requests are never duplicated.
