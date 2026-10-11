# Post-mortem: a 71-line change shipped as an 8,099-line pull request (2026-10-11)

Source: [willtech3/evergreen_chat#120](https://github.com/willtech3/evergreen_chat/pull/120), the evaluation copy of [willtech3/evergreen_chat#119](https://github.com/willtech3/evergreen_chat/pull/119), run `run_1bf2f7742b`, outcome "Refresh Evergreen frontier models with hosted browser proof". The numbers below come from the pull request's file list and its description. The rules this post-mortem changed cite it by section (for example "post-mortem 2026-10-11 §2").

## Headline

The product change was 71 lines: four entries added to `src/lib/models.ts`. The pull request changed 12 files, +8,099 / −2,462. The rest was regenerated data, a browser test written for this one feature, and an evidence fixture. The spec made the browser test part of the acceptance criteria, so review went to the test. Every contested finding (F-1 to F-10) was about the test's failure handling or about report bookkeeping; none touched `models.ts`. The run took 3 implement rounds, 7 review sessions, about 4 hours, and $52.02 of its $60 cap. Its description was written for a factory operator. The one product decision, that the picker's "Max" effort label actually sends `xhigh`, was item 1 of 4 under "Decisions needed", listed twice, after a criteria table and before a cost table.

## What the pull request contained

| Part | Files | Lines (+ / −) | Kept by a maintainer? |
|---|---|---|---|
| Product change | `src/lib/models.ts` | 71 / 0 | yes |
| Repository note | `CLAUDE.md` | 1 / 1 | yes |
| Regenerated catalog data (`bun run catalog:refresh`) | `catalog-watch/openrouter-models.json`, `catalog-watch/report.md` | 5,143 / 2,461 | yes, but nothing marked it generated, so GitHub and every reviewer counted it |
| Browser test written for this feature | `verification/frontier-models/` (6 files: a 1,001-line `browser_journey.py`, a 97-line status classifier, a stream observer, a README, 592 lines of classifier tests, the journey model) | 1,860 / 0 | no: it exists so the factory can prove its work, and its helpers are "copied from the onboarding journey, adapted" |
| Model tests | `tests/models.test.ts` | 532 / 0 | mostly |
| Raw catalog "evidence" fixture | `tests/fixtures/frontier-models/catalog-evidence.json` | 492 / 0 | no: provenance of the metadata, not a product test input |

## Issues

### 1. The factory's proof code went into the app

In managed (hosted) mode the controller rejects `factory:browser` (`orchestrator/factory/hosted_verifier.py`, `UNSUPPORTED_COMMANDS`), and the spec writer, the platform contract and docs 05 and 09 told every UI spec to name "a repository-approved browser adapter command". Evergreen had one adapter, the onboarding baseline journey. A new feature needed new steps, so the implementer wrote and committed a new 1,860-line runner, copying the baseline's helpers. Each later UI change would do the same.

**Fix (split part A).** The factory owns the browser runner and ships it in the controller harness uploaded to the hosted sandbox. A change's steps are a run artifact the implementer writes under `/mnt/session/outputs/`, uploaded at verification and bound into the verification record by hash; it is never committed. Blocked-versus-failed classification and credential hygiene are tested once in `orchestrator/tests`.

### 2. Acceptance criteria described the proof, not the product

AC-13 required the journey's `result.json`, at least five screenshots and exactly one webm, and AC-14 required the journey's blocked/failed classifier to map seven fault classes. Both are properties of the evidence. Because they were criteria, the reviewers reviewed the test, and the implementer answered for it: F-1 to F-5 (demo note hard-coded, failing-step facts lost, 401/402/403 reason over-claimed), F-7 (the stream observer crashed after a 403 redirect, turning blocked into failed), F-9 (redirect cause invisible) and F-10 (media kept when the key had been on screen). AC-4 and AC-8 also required values to "derive only from fixture evidence", which turned a provenance capture into a committed 492-line fixture.

**Fix (split part B).** A criterion is behaviour a user, an operator or a caller of the app can observe. The evidence format, the browser test's classifier, redaction, the shape of `result.json` and "values derive from captured evidence" are not app criteria. Provenance captures are run artifacts, committed as a fixture only when a product test needs one. Tests stay proportionate: the ones a maintainer would keep.

### 3. Nothing measured the change's size

Work-item sizes XS to M had no line definition, so a 71-line change that picked up a 7,600-line regenerated tail and a 1,860-line test still read as size S. Nothing split the catalog refresh into its own pull request, and nothing reported the overrun.

**Fix (split part C).** Size counts changed lines including tests, excluding generated files (`linguist-generated` in `.gitattributes`, lockfiles, or a file the spec declares as regenerated by a named command). Defaults, kept in configuration: XS ≤ 50, S ≤ 150, M ≤ 400. The decomposer estimates with this definition, splits anything above M, and gives a large data refresh its own item that lands first. The spec judge rejects a spec whose estimate is above M. A merge-time check reports hand-written and generated lines against the estimate; an overrun is a flag for the owner, not another round.

### 4. The description was written for the operator, not the reviewer

`templates/cr_template.md` put the evidence bundle in the body: a 15-row criteria table, a contested-findings list, a verification section, and a per-session provenance and cost table. `agents/integrator.md` told the integrator to paste the merge judge's `summary_for_human` in word for word. The result had AC-n, F-n and DET-* ids, the words merge judge, orchestrator, recipe, journey and round, session ids and costs, the judge's first-person text ("I re-ran…", "I viewed 11 of 21 hosted PNGs"), "Decisions needed" twice, and eleven links to `127.0.0.1:8772` that only work on the owner's machine. A reviewer who knows the app but not the factory had to read past all of it to find the one product decision.

**Fix (split part D).** The description has seven short sections (what changed and why; needs your decision, product items first; see it; how it was tested; size; start reading at; one link to the run record) and a 60-line target. The controller renders it from structured facts and lints it before it opens: no factory ids or terms, no agent or session names, no costs, no first-person text, no local links except the run-record link, at most three screenshots and one demo.

### 5. The merge judge wrote for a description it never saw

The judge's residual risks said "The PR description must list this as an unconfirmed gap; I did not see a PR description." Its one `summary_for_human` field served two readers: the reviewer at the top of the pull request and the operator auditing the run.

**Fix (split part D).** `merge_readiness` splits the two: `pr_summary` is a short third-person summary with no ids, for the pull request, and `summary_for_human` keeps the detail for the run record. The judge is told it never sees the description and must not comment on it.

### 6. The evaluation banner hid what a reviewer needed

The do-not-merge banner on the evaluation copy ran to four sentences and did not say what the copy contained beyond `main` (two fix commits to the journey), which was the part a reviewer of the copy actually had to read.

**Fix (split part D).** One banner sentence, then the files and lines the copy has beyond the current default branch.

### 7. The evidence had nowhere to live but the pull request

The criteria table, contested findings, verification detail, media and costs were in the pull request because nothing else held them in one place. The Console showed the media but not the rest.

**Fix (split part F).** The integrate stage writes a run record per spec to the artifact store, holding everything removed from the description, and the Console's run page shows it inside the existing "What came out" card, with the media in "See the change".

## What it cost

3 implement rounds ($8.18, $6.41, $4.54), 7 review sessions including the security pass, 8 hosted verification sessions, and 28 completed sessions in all: $52.02 of the $60 cap over 3h 58m before integration. None of the review findings that drove rounds 2 and 3 concerned the 71 product lines.
