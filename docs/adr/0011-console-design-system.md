# ADR-0011: One design system for the Console, drawn around the line and the crew

**Status:** accepted · **Date:** 2026-10-11

## Context
The Factory Console grew screen by screen. By October 2026 it had three names for the same pipeline (the run page's "Station 3 · Plan check", the gate pages' "Gate 1: you", the Board's "Plan / Specify / Build / Review / Ship"), five colours for roles, a dozen pill variants, 101 inline style attributes in `app.js`, one generic `explain` class used 117 times for anything grey, a second stylesheet in `surfaces/artifact_pages.py` with different fonts and colours, and a run header made of five unexplained tags ("Running", "live", "$58.00 of $220.00 so far", "Implementer, Reviewer working for 30 s", "30 s"). The crew roster showed cost for a finished member and elapsed time for a working one in the same slot. Board cards cut summaries at 140 characters mid-word and showed "since 05:41 AM UTC". A user could not say at a glance why a run had just changed state or who was working on what, and every new screen was free to invent its own vocabulary and visuals. A UI audit (the design canvas "Factory Console Design System", October 2026) listed these; the owner's own verdict on the first redesign draft, built from pills and monospace labels, was that it was "AI slop".

## Decision
1. **One model of a run, in one table.** Four phases (Plan, Specify, Build, Ship) group seven stations (Outcome, Decompose, Gate 1, Specify, Gate 2, Build, Pull requests). `STATIONS` in `app.js` is the single source for their keys, names, owners and one-line descriptions. The Runs list, the run page, the Board and the gate pages draw from it. Parked and terminal states are a written status beside the line, never an eighth station.
2. **The drawn floor.** The animated pixel crew and the moving belt are the one graphic that carries state: the figures stand on the line at the current station (bobbing and tapping their tools while sessions are open, asleep when the run is parked), grey human figures mark gates a person already decided, an orange one marks the gate or question that is a person's now. The same line renders at three sizes.
3. **Status is written.** The state of a run is one sentence in the display face with a bold lead in the tone of the state (`statusSentence()`), followed by a grey meta line (`metaLine()`). The story groups events by station and names cause or effect in each entry.
4. **No pills, chips, badges, coloured bars or role colours.** A label is a word in ink; sections are paper on the floor with hairlines inside; tone inks are text only; orange means a person is needed now and nothing else; clay is the crew. Times are relative with the absolute time on hover; one slot carries one unit; prose is cut at sentences, never mid-word.
5. **Tokens and a documented component set** in `app.css` (grounds, inks, space, type, shape, layout, motion; dark mode redefines the same names), described in [10-console-design-system.md](../10-console-design-system.md), which is the contract for adding a screen. The standalone gate page keeps a copy of the inks so it stays one self-contained file.

## Why not the alternatives
- **Keep the pills and fix the wording.** The owner rejected chips and coloured bars outright; they were also the mechanism by which every screen diverged (each had its own pill variant and colour for the same meaning).
- **A typographic system of monospace uppercase labels in five colours.** Tried first; it swapped one trope for another, removed hierarchy, and was colour-coding by other means.
- **A dense instrument panel.** Rejected in favour of the drawn floor: the owner wanted the animated crew to stay, and the Console's job is a few runs read closely, not hundreds scanned.
- **A framework or a build step.** The Console is one file of vanilla JavaScript on purpose (no toolchain for a local workbench); a token layer in CSS and a few helpers give the same consistency.

## Consequences
- `app.css` is organised in twelve sections with tokens first; `.tag` no longer draws a ground, `.card.soft/.info/.call` render as plain paper, role colour rules are gone, and the belt and figures are real components (`lineStrip`, `crewFigure`, `humanFigure`).
- `app.js` gains `STATIONS`, `statusSentence`, `metaLine`, `timeEl`, `firstSentences`, `personName`; `miniSteps`, `statusTag`, `activityLine`, `modeTag` and `roleLegend` are gone. The run detail's timeline now carries `actor_id`, so the story can name the person who decided.
- The Board's "Review" column folds into Build (the server still files reviewers under `review`; the view maps it).
- The contrast tests in `tests/test_experience_review.py` keep pinning `:root` and the dark block; the status inks stay text, never fills.
- The design canvas holds the audit and the redesigned screens; this page and the doc are what the repository keeps.
