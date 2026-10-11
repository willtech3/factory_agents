# Factory application and agent audit — October 10, 2026

Pulled `main` through `8f579ea`, then incorporated the reviewed and repaired onboarding PR #14 (`6fa432e`). The audit covered the pipeline, session driver, event state, approval queue, integration boundary, prior platform sessions, and the Console. Tests and form submissions used temporary data; no paid Factory sessions were launched or live approvals changed.

## Findings that survived refutation

| # | Priority | Finding and attempted refutation | Resolution and evidence |
|---|---|---|---|
| 1 | P1 | Concurrent approval-queue writes lose records. Atomic rename prevents partial JSON but does not protect read/modify/write. Four processes retained only 48 of 80 questions in the reproduction. | Lock a stable per-file inode across read/modify/write for gates, questions, answers and decisions. Multiprocess regression retains all 80. |
| 2 | P1 | The CLI/file queue saves invalid decisions before validating them; validation on polling then crashes the consumer. Console-side validation does not cover CLI callers. | Validate before persistence. An empty request-changes decision is refused without poisoning the queue. |
| 3 | P1 | Stream recovery reads only the first history page. Six inspected real sessions contained 105–442 events; their terminal events were beyond page one at 100/page. Streaming does not guarantee replay of missed history. | Drain every history page in chronological order. Regression recovers the grader outcome and terminal event from page two. |
| 4 | P1 | A clean stream EOF is treated as session completion. Transport closure is not evidence that an agent finished. | Reconnect with bounded retries until a terminal event is observed; retain active ownership if recovery fails. |
| 5 | P1 | A tool event is marked handled before its result is delivered. A network error then permanently skips the response; simply replaying the handler could repeat an external side effect. | Cache the handler result, mark events handled only after delivery, and reconcile server acknowledgements before replay. Tests cover both failed delivery and a lost response after successful delivery. |
| 6 | P1 | Accepting output after a grader iteration limit records its cost twice; output-validation failures can omit the spend entirely. Artifact preservation is valid; counting the same returned session twice is not. Four $1.20 writers and a $2 judge became nine sessions/$11.60 instead of five/$6.80. | Record session completion immediately when a result returns, before parsing, and only once per returned result while retaining both truncated and accepted artifact sets. |
| 7 | P1 | Interrupted/failed outcomes and terminated sessions can supply output to the next stage. A real inspected session reported an interrupted grader outcome followed by an end-turn idle event. Idle alone therefore does not refute the risk. | Persist unfinished evidence and its spend, then stop advancement. Tests cover interruption, failure and termination. |
| 8 | P2 | Nonblocking/defaulted and expired questions remain open after the agent receives its default. They are historical context, not outstanding decisions. | Emit an explicit default-answer event for both cases. The event-derived pending set clears. |
| 9 | P2 | Re-decomposition retains removed pending work items. Keeping completed work is intentional; keeping superseded pending work is not. | Replace obsolete unshipped items while preserving open-PR/merged items. Regression replays an old-to-new plan. |
| 10 | P2 | Nested probe/evidence links return 404 because the artifact route accepts only one filename segment. Probe output is intentionally nested. | Serve nested paths within the run's artifact root; traversal and symlink/range regressions remain covered. |
| 11 | P1 | Integration trusts agent-authored PR IDs, accepts an unintended target branch, can commit unrelated staged files, and does not reject a branch changed after judgement. Prompt instructions are not enforcement. | Use the forge receipt as authority, bind the target, commit only record paths, and reject moved heads except an identical prior record commit. Local bare-repository and fake-forge regressions cover both failure and success, including idempotent record commits. |
| 12 | P2 | Start resets every saved outcome's budget to $60. The saved default should survive selecting the outcome. | Load its saved budget. Browser save-to-start check preserves $87. |
| 13 | P2 | Session-level limit gates display a run-budget input that does not control that decision. Run-level budget gates do need it. | Show the amount field only for a real run-budget pause; distinguish session-limit wording and keep confirmation consequences. |
| 14 | P2 | Outcome-form labels are adjacent text rather than associated labels. Visual proximity does not make the controls accessible. | Associate labels; verify controls by accessible name in the browser. Optional field groups remain keyboard-operable disclosures. |
| 15 | P2 | Clock labels slice UTC strings but present them as ordinary local times. Relative ages being correct does not fix absolute labels. | Format local times with the timezone shown. |
| 16 | Improvement | Examples, demo actions, repeated tutorials, speculative future-work cards, placeholder Ops information, an unused owner-role field, and a misleading parallelism control consume attention. Some advanced controls remain useful, but need not lead the flow. | Remove the example/demo entry actions, empty placeholders and repeated explanations. Collapse repository changes, optional form fields and review settings. Remove the parallel-items knob: implementation runs sequentially. Keep scope/budget controls and explicit decision confirmation. |
| 17 | P2 | Editing an outcome rebuilds metric rows and drops recorded measurements; hidden/optional data should not be destroyed by a routine edit. | Preserve existing metric metadata, owner role and additional repository entries while editing displayed fields. |
| 18 | P2 | Settings links every workspace session to a local run page, including onboarding IDs and runs from other checkouts. Those records do not necessarily exist locally. | Emit a local link only when its run/app record exists, route onboarding records to Apps, and retain the platform trace link for remote-only sessions. |
| 19 | P2 | Claude PR #14's new diagnostic renderer crashes on list/dict values in status or phase fields. Agent-authored diagnostic JSON is not schema-guaranteed. | Added type checks and regression cases directly to that PR before merging it. |

The focused reproductions initially failed in nine cases. The final full suite passed **403 tests with one skip**. JavaScript syntax and diff-whitespace checks passed. The touched pipeline/queue modules and audit regression module passed Ruff after cleanup. The browser edit/save check also preserved the chosen $87 budget, recorded measurement value/date, owner metadata and a second repository entry.

## Prior-run observability review

Read 50 recent platform sessions across five run IDs and one onboarding ID, totaling $93.90 in this bounded sample. This is not an all-time spend total: the oldest run is only partially represented by the 50-session listing. Inspected complete paginated event histories for six sessions (1,238 events), including a decomposer, spec judge, implementer and merge judge. Terminal/outcome information was beyond the first page in every inspected history. An interrupted implementation session had an interrupted outcome despite an end-turn idle event, supporting the explicit failure check.

The local checkout held two run logs (one live and one demo), not the entire workspace's history. Missing local records are therefore not proof of lost platform sessions. The pending Claude run-history work owns the history redesign; this audit fixes broken links and the underlying accounting/recovery defects independently.

Raw session summaries, trace counts, screenshots and the expanded local report are kept in ignored `runs/_audit/2026-10-10/`, rather than published in this PR.

## Claims rejected or narrowed

- Event-log sequence collisions from the earlier postmortem: already repaired with locking in `RunStore`; the distinct file-queue race still existed.
- Empty/stale spec Markdown and reviewer push instructions from the earlier postmortem: current code already carries Markdown and uses read-only git instructions in the review/integration paths. Do not repeat those old findings as current failures.
- Cloud Apps initially showed “App not found”: the already-running server predated the pulled route while static JavaScript was current. A fresh server has the route. This was a stale process, not a new route-order defect.
- Board and Runs are not interchangeable: one shows actionable ownership; the other is history. Outcomes holds reusable briefs, Apps holds reusable test setup, and Settings holds connection diagnostics. Their purposes justify keeping the destinations; their redundant explanations do not.
- Removing all advanced review/budget controls would reduce clutter but remove meaningful choices. Keep a single disclosure and meaningful controls.
- A title promising hosted evidence does not establish managed execution. Effective recorded policy is the authority; this audit did not modify an in-flight run's settings based on its title.
- Screenshots establish layout and readable states, not full accessibility compliance or end-to-end agent correctness. Those limits remain explicit.

## UI walkthrough

1. **Board and Runs:** useful ownership/history distinction; duplicate speculative cards and tutorials removed. Full history redesign remains Claude's separate work.
2. **Run detail:** inspected status, crew, story and settings. Fixed source issues behind misleading pending questions, spend, times and evidence links.
3. **Start:** examples/demo actions removed; selected repository compact by default; useful review choices behind one disclosure; saved budget verified through the form flow.
4. **Outcome form:** associated labels, fewer default-visible fields, repository visibility shown only for creation; save and edit flows retained.
5. **Apps:** shorter purpose statement and no repeated onboarding tutorial on every detail/list page. PR #14 adds meaningful failure evidence instead of generic failure prose.
6. **Session-limit gate:** no fake run-budget input or empty artifact-fingerprint panel; confirmation explains the selected action.
7. **Responsive pass:** inspected desktop and 390px layouts. Start page measured 390px content width and 390px scroll width, with no horizontal page overflow.

## Coordination and remaining delivery

- Claude Desktop was the only active Claude app. The “Non-web app onboarding issues” work became PR #14; it was reviewed, fixed and merged at `6fa432e`.
- “Factory runs display design” was waiting on a Claude permission request and had not produced a PR at the last check. Its approval was left to the user.
- A thread follow-up checks every ten minutes for that specific remaining PR, reviews/fixes/tests it, merges only the reviewed passing head, and pauses after verification. It stays quiet while nothing changes.
