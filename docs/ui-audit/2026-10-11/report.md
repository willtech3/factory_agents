# Factory Console: detailed UI and interaction audit

2026-10-11 · Combined UX, visual, behavior, information-disclosure, and accessibility review.

## Verdict

The Console has strong underlying controls: decisions bind to artifacts, gate pages explain consequences, budgets are explicit, and browser evidence distinguishes an application failure from a verification failure. Its presentation nevertheless makes people assemble an answer from too many competing surfaces. The most consequential defect was a stopped run still showing a working agent. The biggest usability problem was giving idle roles, pipeline decoration, raw history, and the next decision similar visual priority.

The implemented direction puts state, recorded cause, active session identity, and the next action first. It preserves the crew artwork while removing pills and colored accent bars, following the user's explicit direction. The result is a neutral, typographic interface with progressive disclosure and a reusable [design system](../../console-design-system.md).

This is an extensive, evidence-backed audit and an implemented foundation. It is not a claim that every possible provider state or accessibility requirement has been verified. The remaining work is explicit below.

## Method and evidence boundaries

- Read the repository's Console, pipeline, human-gate, and design guidance; traced the rendered UI through `app.js`, `app.css`, `app.py`, `summary.py`, and onboarding/environment code.
- Captured the current UI in the Codex in-app browser, saved the actual screenshots, and inspected the saved images. Baseline captures 01–19 precede the redesign.
- The live local Console on port 8765 contained **no run logs or saved outcomes**, one failed local app, workspace environments, and real platform-session history. No historical memory was used as audit evidence.
- Run-state coverage used the existing test fixture driver, real local state replay, and real local gate queue in a separate temporary store. Every example is labeled **[UI fixture]**. These are not real agent results.
- No paid run, provider mutation, environment archive, publication, or real gate approval was submitted. Run creation, recovery execution, and final publication outcomes are source-reviewed or fixture-tested where noted.
- Desktop baseline: 1280 × 720. Phone baseline: 390 × 844. Final checks also cover dark theme and keyboard interactions. DOM observations supplement screenshots; screenshots alone cannot establish accessibility compliance.
- The preview script is `orchestrator/tests/support/preview_console.py`. It deliberately blocks paid work and provider writes. Browser-captured fixture evidence demonstrates UI behavior, not deployed feature correctness.

## Flow coverage

| Step | Screen / action | Baseline health | Source |
|---|---|---|---|
| 01 | Empty run history | Clear CTA; local scope unclear | Live Console |
| 02 | Apps and workspace environments | Readiness obscured by storage provenance | Live Console |
| 03 | Failed app / recovery | Cause present; recovery choices fragmented | Live Console |
| 04 | Start a run | Clear sections; enabled action with no outcome | Live Console; no submission |
| 05 | Empty outcomes | Understandable; duplicate primary CTA | Live Console |
| 06 | Outcome form | Good labels; long and spacious | Live Console; no save |
| 07 | Settings | Readiness clear; session diagnostics dominate | Live Console |
| 08 | Populated run history | Dense; weak scan order | Fixture |
| 09 | Active run | Competing hierarchy; researcher omitted from roster | Fixture |
| 10 | Active run on phone | Essential work/action pushed below decoration | Fixture |
| 11 | Board | Narrow cards and large empty matrix; stale activity claim | Fixture |
| 12 | Gate 1 plan review | Good decision framing; substantial reading burden | Fixture |
| 13 | Approval confirmation / cancel | Clear consequence; focus and subset details needed repair | Fixture; not submitted |
| 14 | Gate 2 contract review | Useful brief; approval-leading copy and dense metadata | Fixture |
| 15 | Failed run | Cause repeated; recovery late | Fixture |
| 16 | Stopped run | Contradictory “stopped” and working-agent claims | Fixture |
| 17 | Completed run with open PRs | Good PR evidence; “Done” overstates the endpoint | Fixture |
| 18 | Blocking question | Good rationale; default wording conflicts with blocking | Fixture |
| 19 | Add an app | Charges disclosed; large setup form | Live Console; no submission |
| 20 | Budget pause and decision | Inspected after redesign; amounts and consequences retained | Isolated fixture |
| 21 | Standalone gate document | Neutral styling verified; long raw-document section remains | Source review and post-change fixture capture |

## Detailed findings and disposition

Priority: **P1** misleading operational state or loss of a user's work/decision context; **P2** material friction, overload, or discoverability; **P3** consistency/polish. “Implemented” means changed in this branch; the validation record below distinguishes automated checks from browser observations.

| ID | Priority | Evidence | Finding and user consequence | Disposition |
|---|---|---|---|---|
| F01 | P1 | 09, 16; `crew_roster`, `board` | A stopped controller still produced working-agent cards and animation. A person could wait for work that was not advancing. | Implemented: board uses run liveness; run detail suppresses unconfirmed active sessions. |
| F02 | P1 | 09; roster role list | Researcher appears in the header but is absent from the roster and excluded from the story. The UI cannot answer who is working. | Implemented: researcher participates in session and history presentation. |
| F03 | P1 | Source trace; parallel-session regression | One role can have several sessions, but the roster's last-started session may already be finished. A link could inspect the wrong session. | Implemented: one detail row/card per open session; roster points to a still-open session. |
| F04 | P2 | 09 | Stage names and start events require the user to infer why the run changed. | Implemented: current state plus recorded cause/latest change. Unknown causality remains explicitly unknown. |
| F05 | P1 | 16 | A controller's unexplained stop has no recorded timestamp or reason; borrowing the previous event's time would create false precision. | Implemented: stopped-state explanation does not invent a stop time. |
| F06 | P2 | 17 | “Done” can mean that PRs were merely opened. Users can confuse run completion with delivery. | Implemented: “Ready for review,” “Design approved,” and delivery evidence remain distinct. |
| F07 | P2 | 18 | “Paused until you answer” beside “If nobody answers: …” makes a blocking question appear optional. | Implemented: “Suggested answer” for blocking questions; default behavior only for nonblocking questions. |
| F08 | P1 | Source trace: `refreshWhenChanged` | Guarding only while an input is focused lets a poll replace a draft after blur. | Implemented for run questions: stable IDs and draft restoration; app feedback participates in dirty-state preservation. |
| F09 | P2 | Source trace: full-page renders | Polling collapses open details and can replace the focused control. | Implemented: keyed disclosure state and identified focus survive same-route renders. Gate review already updates narrowly. |
| F10 | P1 | Source trace; stale UI observed navigating | A response from an old route can render after navigation. | Implemented: route-bound requests discard obsolete responses; route entry stops the old poll. |
| F11 | P1 | Source trace: swallowed poll errors | Connection failure leaves reassuring old state with no stale indication. | Implemented on run/history/apps refresh: visible reconnect notice. Settings and gate polling still need finer per-panel freshness. |
| F12 | P2 | 01–19; `index.html` | The whole main area is a polite live region. Replacing it can announce an entire page repeatedly. | Implemented: dedicated concise announcer; meaningful run changes announce once. Full screen-reader pass remains open. |
| F13 | P2 | 09, source event rendering | The story formats local time while raw events slice UTC text without a zone. | Implemented: visible event times use the same local formatter with zone. |
| F14 | P2 | 09 | Status, mode, spend, live duration, and total duration are all equal capsules in the heading. | Implemented: plain type with separate state, cause, cost, and timing hierarchy. |
| F15 | P2 | 09 | Three fixed columns leave the narrative too narrow at common desktop widths. | Implemented: two useful columns; action panel appears only when it serves a task. |
| F16 | P1 | 10 | Seven stations and global chrome consume the phone viewport before the user reaches the action or agent identity. | Implemented: compact shell, current-stage summary, immediate active-role sentence, action before supporting activity. |
| F17 | P2 | 09, 15 | Every idle role gets a large illustrated row; old roles can still say “Waits for the outcome” after the run progressed. | Implemented: open sessions first; all roles folded; absent history says “No session recorded.” |
| F18 | P2 | 15 | Failure clears the visual pipeline rather than preserving the last meaningful phase. | Implemented: retain the last recorded pipeline stage with an explicit halted interpretation. |
| F19 | P2 | 09 | Work items, events, and files share one disclosure. Useful work status requires exposing unrelated diagnostics. | Implemented: work items visible; separate named history/settings/files disclosures. |
| F20 | P2 | 09, 11 | Agent summaries are cut mid-sentence without an inline path to recover the rest. | Implemented: recoverable excerpts with “Read the full explanation.” |
| F21 | P2 | 18; `askBlock` | Multiple questions share `id=q` and an indistinguishable “Your answer” label. | Implemented: unique question/answer IDs and question-specific accessible names. |
| F22 | P3 | 09–11 | Sleeping figures, blinking eyes, a moving belt, and pulsing calls all compete for attention. | Implemented: no pulsing requests or conveyor; limited agent animation and reduced-motion support. |
| F23 | P2 | 11 | A 20-cell responsibility-by-stage matrix creates thin text columns and mostly empty space. | Implemented: populated responsibility lanes and immediate open-session summary. Large workloads still need filtering. |
| F24 | P3 | 08, 11 | Sidebar count, run count, and board count have different units. | Implemented: accessible sidebar count names decisions/reviews; headers name their units. A dedicated attention inbox remains an option. |
| F25 | P2 | 01, 07 | “No runs yet” appears while platform sessions show prior run IDs. The page does not make its local-record scope clear. | Partly implemented: empty-state copy scopes records to this Console. Cross-Console run discovery is remaining product work. |
| F26 | P2 | 08 | No search; IDs and tiny stage bars consume a column without helping scan titles/results. | Implemented: search and readable history rows; IDs searchable and available in details. |
| F27 | P2 | 08; responsive source | Run tables require lateral reading/scrolling on narrow screens. | Implemented: adaptive run rows. Contract/metric tables still use bounded horizontal scrolling. |
| F28 | P2 | 09 | The cost-excludes-live-sessions condition is partly hidden in a title tooltip. | Implemented: exclusion is visible beside spend; tooltip only explains the accounting convention. |
| F29 | P2 | 04 | “Start the run” is enabled despite no outcome selected. The primary action can only fail. | Implemented: visible prerequisites and disabled action until required data exists. Server validation remains authoritative. |
| F30 | P2 | 04 | “Design only” can sound free or simulated unless paid planning sessions are explained. | Implemented: explicit paid-design wording and stop-before-implementation consequence. |
| F31 | P2 | 02 | “Onboarded elsewhere” is presented as the environment's state. It says little about use readiness. | Implemented: management provenance moved into disclosure; readiness, activity, lifecycle are named separately. |
| F32 | P2 | 02 | Missing verification and no active session are not explicit facts in a common scan. | Implemented: explicit “No verification recorded” and “No active session observed”; avoid unconditional “ready” or “idle.” |
| F33 | P2 | 03 | Error, retry, feedback, and generic agent status are separate cards; the same failure is repeatedly named. | Implemented: one recovery card contains the cause and retry, with guidance as a supporting disclosure. |
| F34 | P2 | 07 | Dozens of provider session rows dominate Settings even when connections are healthy. | Implemented: workspace sessions folded beneath readiness; IDs remain diagnostic content. |
| F35 | P2 | 14 | “Approving is the expected call” nudges agreement before the user has considered remaining issues. | Implemented: neutral instruction to review criteria and remaining issues. |
| F36 | P2 | 12–14 | Request changes is disabled; its prerequisite lives mostly in textarea placeholder text. | Implemented: persistent adjacent explanation. |
| F37 | P1 | Source: `buildDecide` | Confirmation captures comments but previously read selected contracts only at final submit; selection could change after review. | Implemented: confirmation snapshots the subset and comments together; hash binding preserved. |
| F38 | P2 | 13; source | Cancelling confirmation does not restore the original action focus; a delayed focus can fire after navigation. | Implemented: restore trigger focus; delayed confirmation focus checks that the control is still connected. |
| F39 | P2 | 12, 14; responsive source | On a phone, gate decision controls can sit after a very long review document. | Implemented: a keyboard-accessible “Go to decision” control scrolls and focuses the decision panel without changing the route. Long-document assistive-technology verification remains open. |
| F40 | P2 | 06, 19 | Setup forms are long; repository, owner, and metrics can become detached from the summary at phone widths. | Remaining: verify populated/error states across each form; tighten field groups and sticky summaries without hiding required content. |
| F41 | P3 | 01–19; explicit user direction | Capsule statuses and colored panel edges create a generic decorated-dashboard feel. | Implemented: no status pills or colored accent bars; neutral surfaces and typography throughout the Console. |
| F42 | P2 | Source: repeated dimensions/colors | A palette existed but spacing, disclosure, state copy, and refresh rules were scattered. | Implemented: tokens, shared primitives, pure state presentation, contributor guide, in-app examples, and README links. |
| F43 | P2 | Source: render keys | Relative time labels can freeze when event data does not change. | Implemented: minute-scale presentation keys refresh age labels without announcing unchanged state. |
| F44 | P2 | 09; provider boundary | Open session events are not proof that an agent is producing tokens right now. | Mitigated: “Session open” and explicit evidence limits. Remaining: provider heartbeat/freshness data and causal correlation IDs. |
| F45 | P2 | Source: app evidence and gate tables | Complex table reading order, media accessibility, and full assistive-technology behavior are not established by screenshots. | Remaining: VoiceOver/NVDA and keyboard pass on real populated evidence; add table semantics where testing identifies ambiguity. |
| F46 | P3 | 06, 12, 19 | Containers and `.card + .card` margins sometimes compound, creating uneven gaps. | Implemented for shared stack/grid layouts; continue migrating isolated inline layout rules. |
| F47 | P2 | Source: complete/failed run polling | A parked page stops observing changes even though another actor can resume it or merge/deploy its PRs. | Implemented: slower polling continues for terminal pages to pick up new evidence. |
| F48 | P2 | Source: error routing | A failed route offers only a way back, not a retry of the requested page. | Implemented: same-route retry, busy semantics, and delayed loading feedback. |
| F49 | P3 | `artifact_pages.py`; 32 | Standalone review documents maintain a separate palette and retained pills/colored summary edges after the main Console changed. | Implemented: neutral summaries and plain status labels in the self-contained renderer; rendering/contrast tests passed. |
| F50 | P2 | 32 and full standalone DOM | The Gate 1 reading page repeats its structured content in a full raw document, producing a very long reading surface. | Remaining: fold the raw document behind a named disclosure while preserving anchors and portable-file behavior. |

## What was implemented

The state presentation model is independent of rendering and has focused tests for truthful causality, missing evidence, role changes, parallel sessions, stopped/terminal sessions, resumes, and completion semantics. The backend board and roster now preserve individual live sessions and agree with controller state.

Run pages, history, the board, start prerequisites, app recovery, environment facts, and Settings disclosure have been updated. Shared helpers now cover recoverable excerpts, native disclosures, supplementary help, stable page refresh, and reconnect messages. The full event log and file list render on request to keep the overview light.

The design system is part of the repository and accessible from Settings/footer. It includes actual working components rather than screenshots of a separate design. The frontend retains its existing vanilla-JavaScript/no-build architecture.

## Remaining design work, in priority order

1. **Reliable freshness and explicit causal evidence.** Add controller heartbeat age, provider-session observation time, and a causal event reference for transitions. Acceptance: distinguish “open session,” “recently observed running,” and “no fresh observation” without guessing; link a state change to the event that caused it.
2. **Mobile gate validation.** The jump to decision is implemented. Verify long documents with assistive technology and add a compact selected-contract summary where it helps. Acceptance: no hidden text, obscured focus, or bypassed confirmation.
3. **Unified run discovery across Consoles.** The local store and provider workspace have different history. Acceptance: distinguish missing local record from no historical work, and recover/import evidence without pretending a provider session alone reconstructs a run.
4. **Complete long-form and media accessibility verification.** Exercise real populated app verification, credentials, rejected reports, media controls, table headers, zoom, and screen-reader announcements. Acceptance: evidence-backed accessibility checks, not screenshot-based compliance claims.
5. **Scale and information limits.** Add board filters/pagination and event-log pagination for hundreds of tasks/thousands of events; pin actionable errors instead of letting history dominate. Acceptance: stable focus and bounded render work with representative large datasets.
6. **Finish legacy component migration.** Replace remaining inline styles and bespoke detail patterns as those surfaces are touched. Preserve approval semantics and evidence provenance.

## Baseline screenshot walkthrough

These are the exact screenshots inspected before changes. The fixture labels are deliberately visible. The date/time and current account state are a point-in-time observation.

### 01. Empty run history

The primary action is clear. The empty claim lacks its local-record scope; the page leaves much of the viewport idle. F25.

![Empty run history — baseline](01-runs.png)

### 02. Apps and environments

Storage provenance takes precedence over operational readiness. Environment names dominate the row. F31–F32.

![Apps and environments — baseline](02-apps.png)

### 03. App failure

The failed state is repeated in the header, stepper, notice, and side card. Cause and recovery choices are separated. F33.

![App failure — baseline](03-app-failure.png)

### 04. Start a run

Required setup is incomplete while Start remains enabled. Charges and mode consequences need consistent placement. F29–F30.

![Start a run — baseline](04-start.png)

### 05. Empty outcomes

The brief concept is understandable; a second identical CTA provides little extra value.

![Empty outcomes — baseline](05-outcomes.png)

### 06. Outcome form

Labels and optional disclosures are good foundations. Large field/card spacing increases the distance to later required fields. F40, F46.

![Outcome form — baseline](06-outcome-form.png)

### 07. Settings

Readiness is immediately useful. The provider-session table then exposes a great deal of diagnostic detail by default. F34.

![Settings — baseline](07-settings.png)

### 08. Populated run history

The five-column structure, identifiers, bars, and repeated timestamps reduce scan clarity. F14, F26–F27.

![Populated run history — baseline](08-run-history.png)

### 09. Active run

Three competing columns; inactive crew occupies prime space; researcher is named only in the header. F02–F04, F15–F20.

![Active run — baseline](09-active-run.png)

### 10. Active run on phone

At 390 px, the viewport is consumed by navigation, metadata, and the seven-stage strip. F16.

![Active run on phone — baseline](10-active-mobile.png)

### 11. Decision board

The sparse matrix creates very narrow decision cards and blank columns. It also claims activity for the stopped fixture. F01, F23.

![Decision board — baseline](11-board.png)

### 12. Gate 1

Strong consequence framing and evidence structure; the review is long and the decision panel needs clear input prerequisites. F36, F39.

![Gate 1 — baseline](12-plan-review.png)

### 13. Approval confirmation

A useful second step explains what approving starts. No final decision was sent. Focus and selected-subset binding were reviewed in code. F37–F38.

![Approval confirmation — baseline](13-decision-confirm.png)

### 14. Gate 2

The brief is useful, but the recommendation is overly approval-leading and there is considerable repeated metadata. F35, F39.

![Gate 2 — baseline](14-contract-review.png)

### 15. Failed run

The failure is repeated in several locations while recovery is farther down. The pipeline loses its last-stage context. F18, F33.

![Failed run — baseline](15-failed-run.png)

### 16. Stopped run

The screenshot visibly combines Stopped with the active-stage figure and a working-session link below. F01, F05.

![Stopped run — baseline](16-stopped-run.png)

### 17. Completed run

The PR table is useful evidence. Done is too broad for open PRs awaiting review. F06.

![Completed run — baseline](17-completed-run.png)

### 18. Blocking question

The rationale and suggested responses help. Blocking and default copy conflict; other content dilutes the action. F07, F21.

![Blocking question — baseline](18-question.png)

### 19. Add an app

The budget/charge consequence is visible. Populated, invalid, and narrow-screen paths need additional end-to-end coverage. F40.

![Add an app — baseline](19-add-app.png)

## Updated experience

The screenshots below reflect the neutral direction: plain statuses, no pills, no colored accent bars.

### Run overview

![Updated run overview](20-run-overview-after.png)

### Phone hierarchy

![Updated phone overview](21-run-mobile-after.png)

### Run history

![Updated history](22-history-after.png)

### Board

![Updated board](23-board-after.png)

### Working design system

![Design system](24-design-system.png)

### Dark theme

![Dark run overview](25-dark-run.png)

### Budget decision

![Budget decision](26-budget.png)

### App recovery

![App recovery](27-app-recovery.png)

### Standalone gate document

![Standalone gate review](32-standalone-review.png)

## Validation record

The final checks and limits are recorded in [validation.md](validation.md).
