# ADR-0001: Orchestrator-driven stage sessions, not one coordinator session per run

**Status:** accepted · **Date:** 2026-10-09

## Context
Managed Agents offers a `multiagent` coordinator that can roster other agents and run them as threads in one session, sharing a filesystem. The whole factory could in principle be one coordinator session with the decomposer, writers, judges, implementers, and reviewers as roster members.

## Decision
Run **one session per stage per work item**, driven by our own deterministic orchestrator, and use coordinator rosters only *within* a stage (researchers under the decomposer and writer; `self` copies under the implementer and reviewer).

## Reasons
1. **Human gates park the run for hours or days.** A session waiting on a custom tool result for two days is a poor fit; a session boundary with orchestrator state is natural.
2. **Judges must provably not see the author's context.** Threads in one session share a filesystem and the coordinator's messages; separate sessions share only the files we choose to upload.
3. **Per-stage agents are versioned and budgeted independently.** A reviewer prompt change should not bump the decomposer's version; a runaway implementer should hit its own cap.
4. **Evals need stage isolation.** Replaying a frozen decomposition into the spec stage is trivial with stage sessions and awkward inside one session.
5. **Platform limits:** one level of delegation and 25 concurrent threads would be hit by a four-spec run with two reviewer passes each.
6. **Evidence:** coordination research finds designated coordinators add little and shared artifacts beat chat; Cognition reports managers become "overly prescriptive" without codebase context ([academic-2026.md §4](../research/academic-2026.md); [practitioners.md §2](../research/practitioners.md)).

## Consequences
- We own a state machine, an event log, and artifact movement (`orchestrator/factory/`). This is the 12-Factor "own your control flow" position.
- More sessions means more fixed per-session overhead (sandbox provisioning, repo clone). Repository caching on the platform mitigates clone time.
- The Console shows many short sessions per run rather than one long one; `metadata.run_id` ties them together.
