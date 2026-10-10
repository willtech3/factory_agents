# ADR-0007: Pluggable gate surfaces, tracker as the default board

**Status:** accepted · **Date:** 2026-10-10

## Context
Gates 1 and 2 were bound to one surface: a draft merge request on GitLab holding the decomposition and specs as files, with inline comments as feedback and the CLI's file queue as the only place a decision was actually read from ([ADR-0003](0003-gitlab-integration.md)). That assumed GitLab, assumed every owner is comfortable reviewing JSON on a merge request, and gave the run no board: nobody could see at a glance which work items were where. The user wants to run the factory with a personal GitHub account and a personal Linear workspace first, and later at work with Jira and self-hosted GitLab, so both the forge and the surface a human decides on must be pluggable.

The existing protocols already allowed this: `ApprovalQueue` and `Notifier` in `gates.py` were defined for exactly the "Slack, Console, GitLab" fan-out, but only the in-memory and file-backed implementations existed.

## Decision
1. **Three gate surfaces behind the existing protocols**, selected with `FACTORY_GATE_SURFACE=forge|tracker|file` (default `tracker` when `LINEAR_API_KEY` is set, else `forge`):
   - **tracker** (`surfaces/tracker.py`): a run is a tracker project plus a run issue; every work item is a child issue whose state mirrors `WorkItemStage`; a gate is a comment on the run issue (or the work item's issue for `DEPLOY_FAILURE`) carrying the summary, artifact links, and the artifact sha256 list, plus the label `factory:gate-open`; the decision is read from a `factory:approve|request-changes|reject|approve-subset` label and the newest human comment, with the hashes copied from the gate comment so [ADR-0005](0005-gates-bind-to-artifact-hashes.md) applies unchanged; blocking questions are `factory:question` comments answered in their thread; comments render to `feedback.md`. `LinearTracker` implements the tracker-neutral `Tracker` protocol over Linear's GraphQL API; `jira.py` is a stub with the same methods and a REST mapping in its docstring.
   - **forge**: the design change request as before (now `runner._design_change_request`, forge-neutral through [ADR-0008](0008-pluggable-forge-github-default.md)). Kept for teams that want inline comments on the spec files.
   - **file**: the CLI's file queue and local pages only.
   The CLI's file queue is always composed in (`CompositeQueue`), so `factory decide` works whichever surface is configured; the first surface with a decision wins.
2. **Artifact pages for reading, separate from deciding** (`surfaces/artifact_pages.py`): one self-contained HTML page per gate, judge summary first, goal-coverage matrix, per-spec sections, open questions, and the hash list; published by a `PagePublisher` (`LocalHtmlPublisher` to `runs/<run_id>/pages/`; `ClaudeArtifactPublisher` is a documented stub because claude.ai artifacts have no publish API). Gate artifact links point at page anchors unless a design change request exists, whose file links then win.
3. **The approved design is recorded on the implementation branch regardless of surface.** Before the change request is opened, the orchestrator commits `spec.json`, `spec.md`, the spec judge's `verdict.json`, and the merge judge's verdict under `.factory/runs/<run_id>/` from its own checkout, with a fixed author and timestamp (deterministic sha). The reviewer at Gate 3 sees the contract next to the code on every forge and with every gate surface.
4. **Surface roles**, documented in [03-human-in-the-loop-ux.md](../03-human-in-the-loop-ux.md): Cowork or the CLI as the personal workbench, artifact pages for reading, the tracker as board and record, Slack for notifications (later), the forge from Gate 3 onward, the Factory Console deferred.

## Why not the alternatives
- **Tracker only, no pages.** Linear and Jira comments are fine for a summary and a decision but poor for reading a 400-line spec with tables. Pages are cheap and keep the tracker comment short.
- **Reading and deciding both on the page.** A page would need a backend to record a decision, which is the Factory Console: deferred on purpose (roadmap). The tracker already has auth, audit, notifications, and a mobile app.
- **Decisions by comment text only, no labels.** Labels are what a board filters on, survive comment edits, and are one click from a phone; the comment carries the reasoning and the subset list.

## Consequences
- `RunContext` gains `tracker` and `pages`; `Gate` and `Question` gain `work_item_id`; `ctx.event` mirrors `work_item.stage` events onto the tracker. Nothing else in the stages changes.
- A stale label (left from a previous gate on the same issue) is cleared when a new gate opens; a decision whose gate comment no longer matches the gate's hashes raises `StaleDecision` exactly like the CLI path.
- Jira is a stub: filling it is a one-file job with the mapping in `jira.py`'s docstring; `TrackerQueue` needs nothing else.
- Nothing here is verified against the live Linear API; the mapping is unit-tested with a fake client. The first real run (docs/07) is the verification.
