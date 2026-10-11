# ADR-0011: No design change requests in the target repository

**Status:** accepted · **Date:** 2026-10-11 · Supersedes the `forge` gate surface of [ADR-0007](0007-pluggable-gate-surfaces.md) and the design MR of [ADR-0003](0003-gitlab-integration.md).

## Context
With `FACTORY_GATE_SURFACE=forge`, Gates 1 and 2 committed the decomposition, specs and verdict under `.factory/runs/<run_id>/` on a `factory/<run_id>/design` branch and opened a draft `[factory design]` change request so the owner could comment inline. It was also the default whenever `LINEAR_API_KEY` was unset, so a plain `factory run` from a terminal opened one without being asked.

The first live use (evergreen_chat PR 118, run `run_1bf2f7742b`) showed the cost: a draft PR that holds no application code sits in the target repository next to the real one (PR 119), and nothing ever closes it. The decision never came from it either: approvals are read from the file queue or the tracker and bound to artifact hashes in the event log ([ADR-0005](0005-gates-bind-to-artifact-hashes.md)). Since [ADR-0010](0010-external-integration-records.md) the implementation PR makes no record commit, which left the design CR as the only thing the factory wrote to a target repository for its own bookkeeping.

## Decision
1. **The target repository sees nothing from the factory before Gate 3.** No branch, commit or change request is created for Gates 1 and 2. The only change requests are the code PRs (or MRs) the integrator opens.
2. **The `forge` gate surface is removed.** `FACTORY_GATE_SURFACE` accepts `tracker`, `file` and `web`; the default is `tracker` when `LINEAR_API_KEY` is set, else `web`. A leftover `forge` value is an unknown surface and reported as a configuration gap.
3. **Gates are read on the artifact pages and the Factory Console, and decided on the Console, the CLI or the tracker.** Inline review comments on the specs are replaced by the notes the Console carries with Request changes and by tracker comments.
4. **The forge protocol loses what only the design CR used**: `create_branch`, `file_url`, `change_request_comments`, `change_request_discussions` and `render_feedback_md`. The integrator's `open_change_request` tool drops `design_cr_id`, and the PR template drops its design CR link.

## Consequences
- Design-only runs never bind a forge.
- Inline, line-anchored comments on a spec are no longer available; the Console's per-item notes and tracker comments are the feedback channels.
- `agents/integrator.md` changed, so the integrator must be re-applied (`ant apply`) and `claude-lock.json` committed.
- Design PRs opened before this change (evergreen_chat PR 118) are not closed automatically.
