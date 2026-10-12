# ADR-0007: Pluggable gate surfaces, tracker as the default board

**Status:** accepted · **Date:** 2026-10-10 · The `forge` surface was removed by [ADR-0011](0011-no-design-change-requests.md); the default without Linear is now `web`.

## Context
Gates 1 and 2 were bound to one surface: a draft merge request on GitLab holding the decomposition and specs as files, with inline comments as feedback and the CLI's file queue as the only place a decision was actually read from ([ADR-0003](0003-gitlab-integration.md)). That assumed GitLab, assumed every owner is comfortable reviewing JSON on a merge request, and gave the run no board: nobody could see at a glance which work items were where. The user wants to run the factory with a personal GitHub account and a personal Linear workspace first, and later at work with Jira and self-hosted GitLab, so both the forge and the surface a human decides on must be pluggable.

The existing protocols already allowed this: `ApprovalQueue` and `Notifier` in `gates.py` were defined for exactly the "Slack, Console, GitLab" fan-out, but only the in-memory and file-backed implementations existed.

## Decision
1. **Three gate surfaces behind the existing protocols**, selected with `FACTORY_GATE_SURFACE=forge|tracker|file` (default `tracker` when `LINEAR_API_KEY` is set, else `forge`):
   - **tracker** (`surfaces/tracker.py`): a run is a tracker project plus a run issue; every work item is a child issue whose state mirrors `WorkItemStage`; a gate is a comment on the run issue (or the work item's issue for `DEPLOY_FAILURE`) carrying the summary, artifact links, and the artifact sha256 list, plus the label `factory:gate-open`; the decision is read from a `factory:approve|request-changes|reject|approve-subset` label and the newest human comment, with the hashes copied from the gate comment so [ADR-0005](0005-gates-bind-to-artifact-hashes.md) applies unchanged; blocking questions are `factory:question` comments answered in their thread; comments render to `feedback.md`. `LinearTracker` implements the tracker-neutral `Tracker` protocol over Linear's GraphQL API; Jira is not implemented (its REST mapping is under Consequences).
   - **forge**: the design change request as before (now `runner._design_change_request`, forge-neutral through [ADR-0008](0008-pluggable-forge-github-default.md)). Kept for teams that want inline comments on the spec files.
   - **web** (first named `file`; the `file` name was removed on 2026-10-11): the CLI's file queue and local pages, plus the Factory Console (`factory web`, [08-factory-console.md](../08-factory-console.md)), which reads the same queue and writes decisions to it.
   The CLI's file queue is always composed in (`CompositeQueue`), so `factory decide` works whichever surface is configured; the first surface with a decision wins.
2. **Artifact pages for reading, separate from deciding** (`surfaces/artifact_pages.py`): one self-contained HTML page per gate, judge summary first, goal-coverage matrix, per-spec sections, open questions, and the hash list; published by a `PagePublisher` (`LocalHtmlPublisher` to `runs/<run_id>/pages/`; `ClaudeArtifactPublisher` is a documented stub because claude.ai artifacts have no publish API). Gate artifact links point at page anchors unless a design change request exists, whose file links then win.
3. **The approved design remains in Factory’s artifact store.** The implementation PR links to the specification and judge verdicts. Integration leaves the reviewed branch unchanged. Historical runs may contain committed records; new integration steps create no record commit.
4. **Surface roles**, documented in [03-human-in-the-loop-ux.md](../03-human-in-the-loop-ux.md): the Factory Console or the CLI as the workbench for the first stages, artifact pages for reading, the tracker as board and record, Slack for notifications (later), the forge from Gate 3 onward.

## Why not the alternatives
- **Tracker only, no pages.** Linear and Jira comments are fine for a summary and a decision but poor for reading a 400-line spec with tables. Pages are cheap and keep the tracker comment short.
- **Reading and deciding both on the page.** A page would need a backend to record a decision, which is the Factory Console: deferred on purpose (roadmap). (Since built: `factory web` reads the gates and records decisions to the file queue; see decision 1.) The tracker already has auth, audit, notifications, and a mobile app.
- **Decisions by comment text only, no labels.** Labels are what a board filters on, survive comment edits, and are one click from a phone; the comment carries the reasoning and the subset list.

## Consequences
- `RunContext` gains `tracker` and `pages`; `Gate` and `Question` gain `work_item_id`; `ctx.event` mirrors `work_item.stage` events onto the tracker. Nothing else in the stages changes.
- A stale label (left from a previous gate on the same issue) is cleared when a new gate opens; a decision whose gate comment no longer matches the gate's hashes raises `StaleDecision` exactly like the CLI path.
- Jira is not implemented. A `JiraTracker` is a one-file job against the `Tracker` protocol (keep the client as thin as `LinearClient`); `TrackerQueue` needs nothing else. The Jira Cloud REST v3 mapping, with `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` (basic auth) and `JIRA_PROJECT_KEY`:

  | Tracker method      | Jira                                                                                  |
  |---------------------|---------------------------------------------------------------------------------------|
  | ensure_project      | an Epic in the configured project (`POST /rest/api/3/issue`, issuetype Epic), or the  |
  |                     | project key itself when `JIRA_PROJECT_KEY` pins one project per run                    |
  | ensure_issue        | `POST /rest/api/3/issue` (issuetype Task/Story) with `parent` = the run's Epic         |
  | issue_url           | `<JIRA_URL>/browse/<key>`                                                              |
  | set_issue_stage     | `GET /rest/api/3/issue/{key}/transitions`, pick by name/category, then `POST` it      |
  |                     | (Jira status categories: new, indeterminate, done mirror STAGE_TO_STATE's types)      |
  | labels/add/remove   | the `labels` field via `PUT /rest/api/3/issue/{key}` (`update: {labels: [{add|remove}]}`) |
  | post_comment        | `POST /rest/api/3/issue/{key}/comment` (body in ADF; wrap markdown in a codeBlock or    |
  |                     | convert; the HTML marker must survive as text for `TrackerQueue` to find it)          |
  | comments            | `GET /rest/api/3/issue/{key}/comment` -> {id, body (text), author.displayName,         |
  |                     | created, parent: none (Jira has no threads; TrackerQueue falls back to "mentions id")} |
- Nothing here is verified against the live Linear API; the mapping is unit-tested with a fake client. The first real run (docs/07) is the verification.
