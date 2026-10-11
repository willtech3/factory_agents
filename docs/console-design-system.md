# Factory Console design system

Status: implemented foundation, 2026-10-11. Applies to the web Console. The pipeline's approval rules remain in [design-principles.md](design-principles.md). Visual examples are available at `#/design-system` in the Console; the [UI audit](ui-audit/2026-10-11/report.md) explains the evidence and remaining work.

## Product promise

A person should be able to glance at a run and answer four questions: **What is happening? Why did it change? Who is doing it? Do I need to act?** They should be able to inspect the evidence without having to read it all to get those answers.

Factory is a control room for delegated work. Its personality comes from clear language, the small crew figures, and dependable behavior. Avoid making the interface look busy to reassure people that work is happening. Movement, success language, and “running” are claims that need evidence.

**Visual direction confirmed by the user:** no pills and no colored accent bars. Use plain text status, typographic weight, spacing, and neutral borders. Reserve color for meaningful errors, links/focus, and the crew artwork. Do not reintroduce status capsules or colored panel edges.

## Information architecture

| Surface | Primary job | Default content | On demand |
|---|---|---|---|
| Runs | Find the run requiring attention; understand what came out | Outcome title, meaningful status, one summary, current work, spend | Open the run for IDs, full history, files |
| Board | Understand who owes the next action | Populated responsibility lanes, decisions, reviews, open sessions, stopped controllers | Decision details and run evidence |
| Run | Explain the present and the next action | State, cause/last recorded change, active roles, known cost, next action, open sessions, four recent events | Earlier activity, all roles, settings, event log, files |
| Gate | Make an informed decision | Consequences, proposed changes, remaining issues, scope, decision controls | Technical contracts, raw files, fingerprints |
| Start | Choose the work, repository, execution context, and budget | Required inputs, prerequisites, cost and approval consequences | Review tuning and repository alternatives |
| Outcomes | Create a reusable brief | Desired behavior, measurable target, repository, owner | Measurement mechanics, constraints, branch/scope |
| Apps | Establish whether a setup can be used and what is needed | Setup status and next step; environment readiness, activity, lifecycle | Setup recipe, infrastructure IDs, source provenance |
| Settings | Resolve readiness and connection problems | Service readiness and actionable gaps | Agent registry, workspace sessions, paths, logs |

Navigation vocabulary stays stable: **Runs**, **Board**, **Start a run**, **Outcomes**, **Apps**, **Settings**. A run is one attempt at an outcome. An app is a repository plus a test setup. A contract is the specification approved before implementation. Introduce these terms in context; avoid adding a new synonym to every screen.

## Three disclosure levels

| Level | Reveal trigger | Appropriate content | Never put here |
|---|---|---|---|
| Essential | Immediately visible | Current status; blocking reason; next action; charge/budget; required limitations; consequence of approving | Long session IDs, raw JSON, repeated metadata |
| Supporting | A named disclosure, link, or selection | Full explanation, prior events, all agent roles, contract internals, environment management, session history | An undisclosed charge or condition needed to make the decision |
| Diagnostic | Explicit evidence/diagnostics request | Exact timestamps, event kinds, fingerprints, raw logs, policy and environment identifiers | Unlabeled secrets or invented state |

A tooltip is a **supplementary definition**, not a fourth information level. It must work on focus and click/touch as well as hover, remain readable while hovered, and dismiss with Escape. Use `hint(label, text)`. Keep text to roughly two sentences. Do not put buttons or links inside a tooltip. Never use a native `title` as the sole place for a blocker, a disabled reason, a charge, an approval consequence, or identity needed to distinguish concurrent agents.

Use native `<details>` through `disclosure(key, label, body)`. The label tells people what is inside, preferably with a count. Prefer “Earlier activity (12)” to “More,” “Advanced,” or “Show all.” Stable keys preserve expanded state during polling. Do not put an essential warning inside a closed disclosure. `longText()` shows an honest excerpt with a way to recover the full text; it must not permanently cut off an agent's explanation.

## Run information contract

`console-model.js` is the pure presentation layer for run state. It does not start work, rewrite events, poll providers, or decide approvals.

1. **Current state:** name the actual phase or active role. “Reviewing the changes” is more useful than an unexplained state-machine name. Keep coarse lifecycle status separate.
2. **Cause:** show a recorded reason or a constrained relationship supported by events. A matching adjacent Gate 2 approval can explain entry into implementation. An arbitrary previous approval cannot. When a session starts without a recorded cause, label it **Latest recorded change**, not an inferred reason.
3. **Time:** show when the evidence was recorded. A stopped controller with no exit event has no trustworthy stop timestamp; do not borrow the last agent's start time.
4. **Agent identity:** one row per open session, including researchers and parallel sessions of the same role. Include role, task, associated work item when available, start time, and an individual session link.
5. **Next action:** say whether action is needed, who owes it, and what it starts. Blocking questions and budget decisions precede history in reading order.
6. **Cost:** show recorded spend against the cap; explicitly state that open sessions are excluded. An estimate is not a final invoice. Duration and recorded spend are different measurements.

Open-session events are not live provider heartbeats. Use “Session open” on the detail view. An orphaned session on a stopped or terminal run cannot be presented as confirmed active work. A resumed controller clears orphaned sessions from the previous attempt. Do not call an interrupted session successful merely because it no longer runs.

**State vocabulary:**

| State | Meaning | Tone | User implication |
|---|---|---|---|
| Running / Session open | Work is recorded as in progress | Plain text | Observe, or inspect session |
| Needs you | An explicit gate/question awaits a person | Emphasized text | Review or answer |
| Decision recorded | The decision is stored but not yet applied | Neutral/info | Wait for the controller; stop claims if it is absent |
| Paused at budget | The next work requires a budget decision | Emphasized text | Review amount and consequence |
| Stopped | The controller is absent without a failure record | Failure | Inspect diagnostics; do not imply self-recovery |
| Failed | A failure was recorded | Failure | Read the cause and supported recovery |
| Ready for review | The run finished with open pull requests | Plain text; explicit remaining action | Code review still required |
| Design approved | A design-only run completed | Plain text | No implementation claim |
| Verified in dev | Recorded deployment checks passed | Plain text | Still show commit, coverage, and limitations |
| Unknown / not recorded | Evidence unavailable | Neutral | Do not substitute “ready,” zero, or “failed” |

Completed does not mean merged, deployed, or verified. A passing browser check does not prove every criterion; retain its coverage, commit, environment, and limitations. Earlier evidence must remain marked as earlier evidence.

## Environment state is three facts

Always separate:

- **Readiness:** verification evidence and its age; use “No verification recorded” when absent.
- **Activity:** sessions observed at the last workspace check; “No active session observed” is narrower than an absolute idle guarantee.
- **Lifecycle:** current setup, available environment, replaced, or archived when the provider supplies that fact.

Storage provenance (“managed by another Console”) belongs in supporting management details. It is not a readiness state. Do not equate a provider environment's existence with a usable local setup. Keep archive actions secondary, with a description and confirmation. Never imply a successful archive before the returned provider state confirms it.

## Visual foundations

Source of truth: `orchestrator/factory/web/static/tokens.css`. Existing component rules live in `app.css`. Do not introduce a second palette inside individual components.

| Foundation | Rule |
|---|---|
| Reading font | Atkinson Hyperlegible; system sans fallback |
| Heading font | Bricolage Grotesque; same fallback chain |
| Code font | JetBrains Mono; use only for code, exact IDs, paths, hashes |
| Body | 15 px, line-height 1.5 |
| Supporting text | 13 px; 12 px reserved for compact labels/time metadata |
| Page title | 26 px, 24 px on phones |
| State headline | 24 px, 21 px on phones |
| Section heading | 17–18 px |
| Reading width | About 72 characters; avoid narrow three-column narratives |
| Spacing scale | 4, 8, 12, 16, 24, 32, 48 px |
| Card radius | 8 px |
| Control radius | 6 px |
| Controls | 40 px minimum; disclosure rows 44 px; small controls 32 px, with adequate spacing |
| Borders | One quiet neutral border per surface; avoid cards nested in cards by default |
| Focus | Visible blue ring in light and dark themes; keep focus separate from selection |
| Motion | Brief, supplementary activity motion; no endlessly pulsing human requests; honor reduced motion |

Semantic tokens remain available so the code expresses meaning, but status text resolves to neutral colors in both themes. `--call` is human-action text, not an orange stripe. Status labels have no background, border, or capsule radius. Run summaries and decision panels use an ordinary neutral border. Navigation and filter selection use weight, neutral fill, or a neutral underline. Color swatches are reference material, not a license to decorate production cards. Error text and focus indicators remain distinguishable.

Use whitespace to group related facts, not to stretch a simple decision across the page. Container layouts own their gaps. A child card must not add a second sibling margin inside a grid or stack.

## Layout and component recipes

### Run overview

Order: title → state/cause/next → compact progress → action and open sessions → recent activity → work items → delivery/evidence → diagnostics. On wide screens, the action card sits beside agent/activity content. In source order, actions come first so narrow layouts and keyboard reading retain priority. At phone widths show the current pipeline stage; do not turn seven decorative stations into a page of scroll.

### History row

Outcome title and state share a heading. One sentence explains the result/blocker. A quiet metadata line gives mode, phase, and activity. Spend aligns on the right on desktop and wraps below on phones. The title is a real link; don't make a mouse-only row the only navigation. Search includes title, headline, and run ID. A filtered empty state offers one clear reset.

### Board lane

Render only populated responsibility lanes. Each card names the run and the action. Do not force a five-column-by-four-role grid at widths where a decision becomes a 150 px text column. Keep decision counts distinct from counts of runs. Grouping by responsibility is presentation only; it cannot change who has authority to decide.

### Recovery

Put the cause next to the recovery action. Distinguish retrying the same request from retrying with new guidance. Preserve the previous evidence and show the cost before the action. If the current API cannot resume a state, offer diagnostics instead of an apparently working button. Do not put three competing “Failed” panels above the first useful action.

### Forms and approvals

Required inputs stay visible. Optional controls explain their purpose before expansion. Associate labels and errors with controls; focus the first invalid input. A disabled primary action needs an adjacent explanation. Clearing or blurring a field must not erase a draft on refresh.

Before confirming an approval show its consequence. Bind confirmation to the selected subset and comment reviewed at that moment. Cancellation restores focus to the triggering choice. A two-step confirmation must not unexpectedly re-arm due to a background render. Keep the artifact-hash binding on the server.

### Empty, loading, stale, and error

- Empty: explain what is absent and one useful next step. Scope the claim (“on this Console”) when history is local.
- Loading: retain context, mark content busy, and show a small loading message if the request takes more than a moment.
- Refresh: update quietly, retain open disclosures, preserve identified drafts and focus, and announce only a meaningful state change.
- Stale: show that the displayed state is from the last successful read; retry visibly. Absence of fresh data is not evidence of success.
- Error: state what did not load and offer “Try again” on the same route. Avoid a dead-end generic error.

## Implementation map

| File / helper | Responsibility |
|---|---|
| `tokens.css` | Semantic palette, type, spacing, radius, focus tokens |
| `console-model.js` | Evidence-derived session identity, phase, state narrative |
| `app.js` → `card()` | Standard surface and heading |
| `app.js` → `disclosure()` | Stable, native, labeled progressive disclosure |
| `app.js` → `longText()` | Recoverable excerpts |
| `app.js` → `hint()` | Hover/focus/click supplementary definition |
| `app.js` → `showPage()` | Preserve keyed disclosures, drafts, and focus on refresh |
| `app.js` → `connectionNotice()` | Shared loading/stale/recovery feedback |
| `app.js` → `environmentFacts()` | Readiness, observed activity, lifecycle |
| `surfaces/artifact_pages.py` | Portable gate documents mirror neutral labels/borders with embedded CSS |
| `app.py` → `crew_roster()` / `board()` | Session identity and suppression of false board activity |
| `tests/test_console_model.cjs` | Causal wording, roles, stale sessions, completion semantics |
| `tests/support/preview_console.py` | Isolated UI scenarios with provider writes blocked |

The frontend remains vanilla JavaScript with no build step. `console-model.js` works in the browser and Node tests. Do not add a framework or UI dependency just to add a card. Prefer replacing a repeated pattern with a shared helper over adding another inline style or status dictionary.

## Review checklist and validation

Before shipping a user-facing change, inspect light and dark themes, desktop and 390 px phone layouts, keyboard navigation, empty/error states, long content, and live refresh during an editable task. Verify the question “Could this copy claim more than the evidence proves?” Test state logic when it changes; do not write snapshot tests for every margin.

```sh
node --test orchestrator/tests/test_console_model.cjs
PYTHONPATH=orchestrator python -m pytest orchestrator/tests -q -m 'not network'
python orchestrator/tests/support/preview_console.py --port 8767
```

The preview uses temporary, explicitly labeled test fixtures. It blocks paid runs, provider writes, and provider discovery; only local fixture answers and decisions are accepted. It is a design review tool, not proof that an agent or deployed application worked. The script prints a manifest and a temporary directory. Creating a file named `offline` there makes its API return 503 for testing reconnect behavior; remove that file to restore it.

Remaining work is tracked in the audit rather than hidden behind a “complete” label. In particular, explicit controller-to-session causality, true heartbeat freshness, cross-Console run discovery, and complete assistive-technology verification need additional work beyond presentation.
