# Verification record

2026-10-11. Scope: the local worktree's UI changes and isolated Console fixtures. No deployment, paid run, real gate decision, or provider mutation was performed.

## Automated checks

- Full non-network Python suite: **473 passed, 1 skipped, 1 network test deselected**.
- Pure JavaScript state-presentation suite: **14 passed**. It covers concurrent same-role sessions, researcher visibility, completion versus delivery, stopped/terminal activity, resumes, blocking versus optional questions, recorded versus applied decisions, causal matching, unknown evidence, role changes, and stale activity.
- After neutralizing standalone gate documents: **14 page-renderer and experience tests passed**. This includes text contrast in both themes and HTML rendering.
- JavaScript syntax checks passed for both browser scripts.
- `git diff --check` passed.
- The existing contrast regression now reads `tokens.css` and resolves semantic aliases; its 4.5:1 assertions were retained.

## Browser checks

| Check | Observed result |
|---|---|
| Current run, desktop | State, latest recorded change, active roles, spend, and next action precede history; both open sessions visible. |
| Current run, 390 × 844 | Document width equals viewport width (390 px); no page-level horizontal overflow; active-role sentence visible before details. |
| Stopped run | Zero active session rows; cause explicitly says no stop reason was recorded. No working illustration is presented as current activity. |
| Board | Both implementer and researcher appear; stopped controller is an Ops card, not an agent card. |
| Search | Typing “retry scheduler” narrows to the intended fixture and preserves search focus. |
| Tooltip | Click/focus reveals supplementary help; Escape dismisses it. Native hover behavior is implemented; pointer-hover timing was source-reviewed rather than separately automated. |
| Keyboard disclosure | Enter expands the named disclosure in the working design guide. |
| Skip link | Enter moves focus to `main` without accidentally changing the app route. |
| Background update during draft | Typed an answer, blurred it, opened the role disclosure, then appended a real event to the fixture store. The new activity appeared while the answer, expanded state, and keyboard focus remained. See screenshot 28. |
| Offline / reconnect | The preview API returned 503 using its local offline switch. The UI retained content and showed a stale-state notice. Removing the switch restored updates; the draft remained. See screenshot 29. |
| Gate navigation on phone | “Go to decision” moved focus to `gate-decision`. Document width stayed 390 px. See screenshot 30. |
| Confirmation | Approve revealed the second step. Cancel restored focus to Approve. No final approval was submitted. |
| Start prerequisites | With no outcome, Start was disabled and “Choose or create an outcome to continue” was visible. |
| Dark theme | Neutral status styling and readable controls verified in the run view; theme restored to light for handoff. |
| App recovery | Cause and retry share one panel; guidance and charged-session details are disclosed on demand. |
| Standalone document | Neutral summary border and plain status labels verified in the actual reading page. |
| Browser errors | No error entries returned in the final browser-log check. |

## Evidence

- 01–19: baseline captures, live Console or explicitly labeled run fixtures as documented in the report.
- 20–27: updated overview, phone layout, history, board, guide, dark theme, budget, and recovery.
- 28: retained draft and focus after an event refresh.
- 29: stale-state message during controlled API failure.
- 30: focused mobile decision panel.
- 31: stopped run with no active-agent claim.
- 32: standalone reading page after styling changes.

The preview was restarted with fresh fixture IDs after updating the standalone renderer. Screenshots from previous fixture instances remain valid historical captures; they are not claims about a current real agent run.

## Limits

- Full VoiceOver/NVDA testing, zoom/forced-color modes, every keyboard path, and every complex media/table combination were not completed. No full WCAG compliance claim is made.
- No paid hosted-agent or deployed-application E2E run was performed. The existing automated suite and local UI scenarios establish the listed behavior only.
- Live sessions, ready/limited app setup, credential entry, real publishing, and deployment-failure variants were source-reviewed where unavailable; not all were captured with real provider data.
- The UI can explain recorded reasons and label unknown causes. It cannot recover causal evidence or heartbeats that the controller/provider does not supply.
- Larger boards and very long histories still need representative performance testing and additional filtering/pagination.
