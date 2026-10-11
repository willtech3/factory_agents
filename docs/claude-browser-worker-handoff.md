# Claude in Chrome browser worker: handoff

Branch `feature/claude-browser-worker-retry`, based on PR10 (`f1ee8b8`), in the worktree `/Users/will/Desktop/factory-browser-worker`. Reviewed by the parent Codex agent; all 236 tests independently passed. The adapter is integrated into the visual-evidence feature branch for PR10 review; no PR was merged or deployed.

## What it is

`factory:browser` checks used to wait for an attached worker that did not exist. This adds the worker side: a small CLI that lets an **interactive Claude Code session with Claude in Chrome connected** pick up a controller request, drive Chrome, and hand back evidence in a form the controller accepts. The controller (`visual.py`) still owns request identity and the final verdict. The adapter adds stricter refusals in front of it and never decides that a step passed.

**Supported mode:** interactive attach only. Unattended capacity is not claimed. Chrome integration in Claude Code needs a claude.ai login (it stays off with an API key, per https://code.claude.com/docs/en/chrome), and on this machine the standalone CLI reports `{"loggedIn": false, "authMethod": "none"}` (`claude auth status`, Claude Code 2.1.296). The desktop app's own session is authenticated and drove Chrome for the smoke tests below. Nothing logged in, copied credentials, or changed settings, permissions, budgets, or model definitions. `claude -p --chrome` is not documented, and no launcher was built for it.

## Files

| File | Change |
|---|---|
| `orchestrator/factory/browser_worker.py` | New. List, claim, brief, submit, block, doctor, and smoke; also the `factory browser` Typer sub-app. |
| `orchestrator/tests/test_browser_worker.py` | New. 27 protocol and failure tests. |
| `orchestrator/factory/visual.py` | `REQUEST_TTL = 900` constant replaces the literal in `pending()`; no behaviour change. |
| `orchestrator/factory/cli.py` | Registers `factory browser ...` and adds one line to the module docstring. |
| `docs/05-platform-and-deployment.md` | One paragraph on the attached worker under "Visual browser verification". |
| `docs/claude-browser-worker-handoff.md` | This file. |

`orchestrator/shell-shim/` (a symlink `sh -> /opt/homebrew/bin/bash`) is untracked output of the **existing** test suite (`implement.py:84`), created by the baseline run before any change here. It probably belongs in `.gitignore`. It was left in place.

## The attach flow

From a Claude Code session with Claude in Chrome connected (`--runs-dir` must match the controller's `FACTORY_RUNS_DIR`):

```bash
orchestrator/.venv/bin/factory browser doctor                    # interactive vs unattended, from `claude auth status`
orchestrator/.venv/bin/factory browser list --runs-dir <runs>    # open requests, JSON lines
orchestrator/.venv/bin/factory browser claim <id> --runs-dir <runs>
#   -> claim.json written with O_EXCL; prints a brief (steps, persona, viewport, rules) and a report draft
#   the session drives Chrome: new tab, resize, measure innerWidth/innerHeight, screenshot each step with save_to_disk
orchestrator/.venv/bin/factory browser submit <id> --draft report.json --runs-dir <runs>
orchestrator/.venv/bin/factory browser block <id> --reason "Claude in Chrome is not connected" --runs-dir <runs>
```

`submit` publishes in this order. It checks the request is still open and the checkout is clean at the requested commit. It parses the draft, with malformed input refused rather than raising. It copies the media into a private staging directory and runs `visual.collect` there, refusing if the controller's status would differ. It re-checks the request and checkout. It copies the media into the evidence directory. Finally it links `result.json` into place, which is atomic and never overwrites a report. The controller stops waiting when `result.json` appears, so the report always lands last.

Refusals (each has a test):

- The request is closed, expired (`REQUEST_TTL`), already submitted, or targets a non-loopback app.
- The checkout is not exactly `head_sha` at claim **and** at submit: a moved HEAD, tracked edits, or untracked files. Ignored build output is allowed.
- The draft's `request_id` or `head_sha` differ from the request, or the step count differs.
- The overall status doesn't follow from the steps: any failed step means `failed`, all passed means `passed`, otherwise `blocked`.
- A passed step lacks its own screenshot (`media[].step`).
- A step is marked passed but `viewport_observed` differs from the requested viewport (`VIEWPORT_TOLERANCE = 0`).
- Media is older than the claim, is a symlink, is not at an absolute path, or isn't PNG/JPEG/WebP/MP4/WebM/GIF by signature.
- The draft or its media entries are malformed. The CLI exits 1 with `refused: ...`; there is no traceback.

`block` is allowed on a dirty or moved checkout and on a non-loopback app, because a blocked report claims nothing about the app.

## Tests

```bash
cd orchestrator && .venv/bin/pytest -q                        # 236 passed (209 before + 27 new)
cd orchestrator && .venv/bin/pytest -q tests/test_browser_worker.py tests/test_visual.py   # 39 passed
```

The new tests cover:

- The claim → submit → `wait_for_browser` → `collect` → `checks.visual_passed` round trip.
- One claim per request; no submit without a claim.
- Dirty-source regression: a tracked edit or an untracked file is refused at claim and at submit, and ignored `build/` output is not.
- A moved HEAD makes the evidence stale.
- Closed, expired, and remote requests are refused.
- The status must follow the steps, and a truthful `failed` report is published.
- Each passed step needs its own screenshot, taken at the requested viewport; a wrong-viewport `blocked` report is accepted.
- The two observed near misses can't pass: 1012×700 for a 1024×700 request, and 406×844 for 390×844. Both are accepted as `blocked`.
- The brief names the request, commit, steps, outer-size rule, Save-dialog rule, and the submit and block commands.
- Stale, fake, and symlinked media are refused, and nothing is published.
- Ten malformed draft shapes give controlled refusals, including the CLI with bad JSON and a non-object media entry.
- `block` produces a blocked verdict and a failing gate.
- `list` and `locate`, including path rejection.
- `doctor` without a login.

Ruff on the new files leaves only the repository's existing idioms (`timezone.utc`, and `typer.Option` defaults as in `cli.py`).

## Real Chrome evidence (scratch only)

Every run used `factory browser smoke`. It commits a harmless page to a scratch git repo and serves it on `127.0.0.1` with a random port. It issues one request through the real `LocalVerifier.run_e2e` path, which calls `new_request`, then `wait_for_browser`, then `collect`, and then applies `checks.visual_passed`. All of this happens under the session scratchpad:

`/private/tmp/claude-501/-Users-will-Desktop-factory-browser-worker/40b3df31-23d0-46fb-9bd3-fe7a496651c4/scratchpad/` (written `$S` below; it is session scratch and may not persist)

No real run store was read or written, and no lifecycle events were created. Run `run_0d83a37114` on localhost:8765 was not touched: it was not listed, claimed, approved, resumed, or stopped.

### Smoke 1: truthful **blocked** (requested 1024×700)

- Request `8851831795d24379bdbd3caa672fa075`, commit `082a0d47dd29bafb45a5ef9d948439cd4ed227cc`, app `http://127.0.0.1:64231/`. Chrome tab 967134399 was in a new Claude in Chrome tab group on "Browser 2".
- **Viewport observation:** `resize_window` 1024×700 reported success, but `innerWidth/innerHeight` stayed **1742×1427**. A retry at 1024×790 gave the same result. `document.visibilityState` was `hidden`, `hasFocus()` was false, and `outerWidth/outerHeight` read 0×0 and then 602×493. The steps were performed and looked correct, but only at the wrong viewport.
- A deliberately dishonest `passed` draft was **refused**: "a step can only pass at the requested 1024x700 viewport; viewport_observed is {'width': 1742, 'height': 1427}". Nothing was published.
- A truthful `blocked` report was submitted. The controller recorded `blocked`, `visual_e2e` failed, and the harness exited 1.
- Screenshots as saved by Claude in Chrome (`save_to_disk`):
  - `/Users/will/.claude/projects/-Users-will-Desktop-factory-browser-worker/40b3df31-23d0-46fb-9bd3-fe7a496651c4/tool-results/mcp-claude-in-chrome-blob-1791663278844-ljd4ii.jpg` (step 1)
  - `.../tool-results/mcp-claude-in-chrome-blob-1791663287821-wy3y8g.jpg` (step 2)
  - `.../tool-results/mcp-claude-in-chrome-blob-1791663287821-53bc6g.png` (step 3, zoom, 696×231)
- Published evidence: `$S/smoke1/runs/smoke_aa85bf52/verify/visual/8851831795d24379bdbd3caa672fa075/`
  - `01-screenshot.jpg`, sha256 `eb282474…`
  - `02-screenshot.jpg`, sha256 `ba994825…`
  - `03-screenshot.png`, sha256 `c818552f…`
  - `result.json`, sha256 `6e75e5f49debe9e70c2de85ddc018b584bccb1cc7ec48a6d93310e6af2ab43b2`
  - This directory is immutable. Its hash was unchanged after smoke 2.
- **GIF, diagnostic only:** `gif_creator` export reported "Downloaded factory-88518317.gif (31KB)", but no file appeared, because Chrome had opened a **native Save dialog** in its window. The parent Codex agent clicked Save in Chrome; no setting or permission was changed. The file is `/Users/will/Downloads/factory-88518317.gif`: 31,516 bytes, GIF89a, sha256 `37d98048bc074d84fbb5b1995b26d86ede26245c71c0d581903ed8c4f0237ef3`. It is **not** part of the request's evidence. The published `demo_note` says no recording was attached, which was true at submission.

### Smoke 2: **passed**, transport only (requested 784×653 = the observed viewport)

- This was run after the parent Codex agent brought the tab to the front. The window measured `outerWidth/outerHeight` 1024×790, and the page measured `innerWidth/innerHeight` **784×653**, visible and focused. The cause of the 240 px width difference was not investigated. The window was **not** resized.
- A fresh request `bec2b13c3eaa4f7fb5eae35ed873ad55` was issued (`smoke --viewport 784x653`): commit `3baf4eb8431c34152a405d18999aef84f8638b27`, app `http://127.0.0.1:64642/`, same tab. The viewport was re-measured at 784×653 before and after the click. All three steps were observed visually, and no GIF was made, because export would raise the Save dialog again.
- Controller verdict: exit 0, `passed`, steps passed/passed/passed, runner "Claude in Chrome (attached Claude Code session)". `visual_e2e`: "All required browser checks have current visual evidence".
- Screenshots as saved:
  - `.../tool-results/mcp-claude-in-chrome-blob-1791663534094-2q1te2.jpg` (step 1, 784×653)
  - `.../tool-results/mcp-claude-in-chrome-blob-1791663538548-ecmrgu.jpg` (step 2, 784×653)
  - `.../tool-results/mcp-claude-in-chrome-blob-1791663538548-fltpl3.png` (step 3, zoom 784×280)
- Published evidence: `$S/smoke2/runs/smoke_9bb82270/verify/visual/bec2b13c3eaa4f7fb5eae35ed873ad55/`
  - `01-screenshot.jpg`, sha256 `cd2f7e3f3044b976…`
  - `02-screenshot.jpg`, sha256 `5954df40c66d8b60…`
  - `03-screenshot.png`, sha256 `0d60bcfde2b8fb8d…`
  - `result.json`, sha256 `af0b00415552d3f618e95f80350e2336c213b4849cc07036fdb80f176eea4f3e`
- **What it proves:** the transport works with a real Chrome session, real screenshots, the exact request and commit, an atomic publish, and the controller's pass. **What it does not prove:** that Claude in Chrome can set a requested desktop or mobile viewport. Smoke 2's viewport was chosen to match the window.

The Chrome tab 967134399 was left open for inspection.

### Smoke 3: truthful **blocked** (requested 390×844, a fresh request)

This ran after the parent Codex agent moved the tab group into a dedicated, visible, normal Chrome window. The window had a vertical tab sidebar, and nothing about Chrome's settings, permissions, or emulation changed.

- Calibration before the request: outer 634×844 gave inner 406×707, a difference of 228×137. `resize_window` to outer 618×981, and then to 600×981, was **clamped to outer 626×981**, which gave inner **406×844**. 390 was not reachable.
- Request `fe6d92a205d543efb1e44adea46d758d`, commit `f4bd710492b0cb3a4475c8d03c306c9e45a954ce`, app `http://127.0.0.1:65230/`.
- Measured before and after each step: step 1 406×843 → 406×844 (1 px height settle on load), step 2 406×844 → 406×844, step 3 406×844 → 406×844. The steps looked right; the heading wrapped onto two lines at this width.
- Controller: `blocked`, steps blocked/blocked/blocked, `visual_e2e` failed, and the harness exited 1.
- Screenshots as saved:
  - `.../tool-results/mcp-claude-in-chrome-blob-1791663960854-bl7t8m.jpg` (406×843)
  - `.../tool-results/mcp-claude-in-chrome-blob-1791663968798-eh0f7r.jpg` (406×844)
  - `.../tool-results/mcp-claude-in-chrome-blob-1791663968798-ve6mqr.png` (zoom 406×360)
- Evidence: `$S/smoke3-390/runs/smoke_c81fd5e7/verify/visual/fe6d92a205d543efb1e44adea46d758d/`
  - `01-screenshot.jpg`, sha256 `304e4f53…`
  - `02-screenshot.jpg`, sha256 `b8520687…`
  - `03-screenshot.png`, sha256 `8827e72c…`
  - `result.json`, sha256 `96e477c421e3aeb6d554d844e7b0c06975f6f1e44c715cc214db91e38b659ccf`
  - This directory is immutable.

### Smoke 4: **passed** at the requested 1024×700 (a fresh request, with a recording)

- Calibration: outer 1252×837 gave inner 1012×700, because the difference was 240 at this width. Correcting to outer **1264×837** gave inner **1024×700** exactly.
- Request `4d009b66afeb400fb9a2af398817f359`, commit `2f6395f8f0d4194883e9f5a49b8fd97301c8ef7b`, app `http://127.0.0.1:65260/`, in the same dedicated window.
- Measured before and after each step: 1024×700 every time, with outer 1264×837 and the page visible.
- Controller: exit 0, `passed`, steps passed/passed/passed, and `visual_e2e` "All required browser checks have current visual evidence".
- Screenshots as saved:
  - `.../tool-results/mcp-claude-in-chrome-blob-1791664002271-7fah3d.jpg` (1024×700)
  - `.../tool-results/mcp-claude-in-chrome-blob-1791664009433-79kpya.jpg` (1024×700)
  - `.../tool-results/mcp-claude-in-chrome-blob-1791664009433-zbl6xj.png` (zoom 1024×280)
- Recording: `gif_creator` export of `factory-4d009b66.gif` raised Chrome's native Save dialog. The parent Codex agent confirmed the download ("44.1KB Done"). The file is `/Users/will/Downloads/factory-4d009b66.gif`: 45,142 bytes, GIF89a, sha256 `a8259dc2ba932bd171f2b0e715537c39e98899e33c9222fdce9b19a81f5f5d0c`. It was submitted as `04-demo.gif`.
- Evidence: `$S/smoke4-1024/runs/smoke_8b2a32af/verify/visual/4d009b66afeb400fb9a2af398817f359/`
  - `01-screenshot.jpg`, sha256 `dcc7bbb9…`
  - `02-screenshot.jpg`, sha256 `d0ba374a…`
  - `03-screenshot.png`, sha256 `c4823765…`
  - `04-demo.gif`, sha256 `a8259dc2…`
  - `result.json`, sha256 `df424aceb6de98a9e4dc4e30e97fc91033fb3f60952e2dc149af7db4d2fe4ca8`
- This is the first check where a **requested desktop viewport** was set by `resize_window` and verified by measurement. It applies to this window layout (visible, normal, 240 px sidebar); it is not a general guarantee.

After smoke 4, browser actions were paused until the parent Codex agent handed the browser back.

### Smoke 5: **passed** at a 412×915 responsive-phone viewport (a fresh request)

This check is separate from smoke 3. It does **not** replace the 390×844 evidence, which stays `blocked`. The parent Codex agent's attempt to widen the native sidebar didn't visibly change it.

- Calibration: outer 640×1052 gave inner 406×915. The content area seems to keep a 406 px floor while the sidebar narrows: the difference was 234 here and 220–228 at outer 626–634. Outer **652×1052** gave inner **412×915** exactly, with a difference of 240×137.
- Request `43b25eecb6f541cb860ef8c2fe717fab`, commit `ee4347354269bec141a254210d9b9a636766f266`, app `http://127.0.0.1:65444/`.
- Measured before and after each step:
  - Step 1: **412×922 immediately after navigation**, a transient while the page settled. The screenshot was taken at 412×915, and 412×915 after the step.
  - Steps 2 and 3: 412×915 before and after.
  - The heading wrapped onto two lines at this width.
- Controller: exit 0, `passed`, steps passed/passed/passed, `visual_e2e` passed. No recording; the desktop check carries the demo.
- Screenshots as saved:
  - `.../tool-results/mcp-claude-in-chrome-blob-1791664236665-wigpbi.jpg` (412×915)
  - `.../tool-results/mcp-claude-in-chrome-blob-1791664246358-oalode.jpg` (412×915)
  - `.../tool-results/mcp-claude-in-chrome-blob-1791664246358-tr7eau.png` (zoom 412×360)
- Evidence: `$S/smoke5-412/runs/smoke_9122cfeb/verify/visual/43b25eecb6f541cb860ef8c2fe717fab/`
  - `01-screenshot.jpg`, sha256 `d22dc67c…`
  - `02-screenshot.jpg`, sha256 `3d592fb6…`
  - `03-screenshot.png`, sha256 `209bc6cf…`
  - `result.json`, sha256 `d8c7180d1999c7a8aa48905c30dc815e1ff871b19e7d815cc3ce217a0a995590`
- **What it is:** a narrow CSS viewport in a desktop Chrome window. **What it is not:** a phone. There is no touch input, no mobile user agent, and no device pixel ratio other than 1.

### Evergreen baseline visit: **not performed**

The parent Codex agent requested a read-only visit to `http://127.0.0.1:3390/` in the same 412×915 window: open "[Synthetic] Factory baseline walkthrough", inspect the thread and its markdown, and reopen it. The first navigation was **denied by the Claude Code session's permission classifier** ("Browser Navigate Exfil"). It was not retried, and no other route was tried: no in-page redirect, no other browser tool. No baseline screenshots exist from this session. Nothing was submitted to the factory run, and no Evergreen files were read or edited. To do the visit, the user can authorize that navigation directly in this session, or do it from a session whose permissions allow it.

Browser actions are paused after smoke 5. Tab 967134399 is still on the smoke-5 fixture page; that fixture server has since stopped. The dedicated window was left at outer 652×1052.

## Limitations and next steps

1. **Desktop viewport control works in a visible, normal window** (smoke 4). It does not work while the window is hidden (smoke 1). `resize_window` sets outer bounds, and the outer-minus-inner difference changed with width (228 at outer 634, 240 at outer 1252 and 1264), so the brief now says to measure, correct, and re-measure.
2. **Phone widths below about 406 px are not reached.** In smoke 3, Chrome clamped this window at outer width 626, which left the page at least 406 px wide, so the 390×844 check is truthfully `blocked`. The parent Codex agent's attempt to widen the native sidebar didn't visibly change its width. A 412×915 responsive viewport **was** reached (smoke 5), but it is a narrow desktop window, not phone emulation: no touch and no mobile user agent. There is no device emulation, by policy. Any spec that requires 390 px, touch, or a mobile user agent stays a coverage gap with this worker.
3. **The Evergreen baseline visit was not performed**, because of the permission denial described above.
4. **A demo GIF needs a person** in this Chrome profile, because of the native Save dialog. The parent Codex agent confirmed the download for smoke 4. Screenshots need no download. Do not change Chrome's download setting from an agent; the brief says to tell the person and submit without the recording if it doesn't arrive in time.
5. **No unattended worker.** It would need the standalone CLI logged in to claude.ai by the user, plus a reviewed, narrowly scoped `claude -p --chrome` launcher. Neither exists here.
6. **Claims have no release.** A crashed session leaves `claim.json` behind; the controller still times out to `blocked`, and the claimer can `block`. The Console shows "waiting" without the claimed state.
7. **Loopback only.** Platform preview URLs are refused for driving; `block` still works for them.
8. The adapter validates provenance, freshness, and consistency, not whether the worker's observations are true. An independent visual reviewer still looks at the screenshots, as `docs/05` already says.
