# Post-mortem: the first live runs (2026-10-10)

Source: every Managed Agents session of the day (35 sessions, $50.03), the orchestrator event logs and artefacts of runs `run_3834b69e13`, `run_83af9398ce`, `run_639b209243`, `run_4c9e37ea11`, `run_57fde1adc2` and the first stages of `run_149c48b75c`, read by six investigating agents (one per stage family, one for the orchestrator, one for cost) and synthesised by a seventh; the two highest-ranked findings were then reproduced by hand. Issues already fixed on the day (file-listing pagination, criteria-mapping key, judge re-asking owner decisions, missing spec.md upload to the judge, stale orchestrator processes, SDK timeouts, workflow-scope 404, missing platform-contract skill) are not repeated here.

## Headline

run_57fde1adc2 reached PR #1 unattended-ish in 3h51m for $33.18, but about half of that ($14-17, ~90 min) was rounds the orchestrator or the rubrics manufactured rather than defects in the code: a stale spec.md upload created a phantom blocking issue, rubric S12 forced the Gate 2 send-back, rubric I5 forced a 15-minute git-history rewrite, and a (now fixed) false deterministic failure bought a full implementer + two-reviewer round. Two latent defects will stop or corrupt the next multi-item run outright: unlocked event seq numbering under the parallel writers/reviewers (reproduced: 5 threads got seqs [1,1,1,1,2]) and an ac_test_mapping check that now passes any mapping, including fabricated test ids. The PR itself mis-reports its own verification (10 of 13 criteria "not_assessed", 5 of 17 sessions in provenance) because of local-preview and bookkeeping gaps.

## Issues, ranked by impact on reaching a mergeable pull request unattended

### 1. Gate 2 re-entry into run_specify loses state: the round counter resets (owner-approved artefacts overwritten), `_markdown` is never set so the judge gets the previous cycle's spec.md (phantom blocking issue) and implementers/reviewers get a 0-byte spec.md, and the previous verdict is dropped

**high** · orchestrator

Evidence:

- run_57fde1adc2/events.jsonl: gate_2 opened 10:33:33 with artifacts spec_wi-due-dates-overdue.json sha 36f7fcc924… and verdict.json fe27cca722…, decided request_changes 11:02:24 on those shas. Neither sha exists on disk: artifacts/specify holds r1-spec.json 18a4ee6254… and r2-spec.json f60a0883cb…; artifacts/judge holds r1-verdict.json 4f3d5bf90a… (written 11:20, cycle B) and r2-verdict 5185e02f83…. Cycle B overwrote the files the owner decided on.
- artifacts/judge/r1-verdict.json (cycle B, 11:20:43) I-1 blocking: "The JSON and the MD are two different specifications under one spec_id. Checked by string search: the JSON never mentions utc_today, test_due_dates, overdue_e2e, list_overdue or rollover-seed, and the MD never mentions today_utc, tests/test_overdue.py, e2e_overdue or list_dicts." The md was cycle A r2's (sesn_0174kkgGfVJ5DaBDHhmMR6go, utc_today×16) uploaded beside cycle B r1's json (sesn_019YUJzZ6U2xADZfMJHPE6Wm, today_utc×15). specify.py:96 `md = s.get("_markdown") or _latest_spec_md(ctx, s["work_item_id"])`; `_markdown` is assigned nowhere; specify.py:187-191 `_latest_spec_md` = `sorted(d.glob(f"{work_item_id}-r*-spec.md"))[-1]`, a filename sort, so the old cycle's `r2` beats the new cycle's `r1`.
- Same gap downstream: implementer sesn_01BVeVGShXZXmDKDndRqboLM `-rw-r--r-- 1 claude ubuntu 0 Oct 10 11:44 /mnt/session/uploads/spec.md` (also 0 bytes in sesn_0113PgU49waf9hoRSUDhyPEv 12:25 and the reviewer passes; r1-p1 findings checks_performed[0]: "Read spec.json (spec.md is empty, 0 bytes)") because implement.py:320 `"spec.md": spec.get("_markdown", "").encode()`; the merge judge gets no spec.md at all. specify.py:128 `verdict: dict[str, Any] | None = None` and :131 `for round_no in range(1, …)` restart cold on every Gate 2 loop, so cycle B r1 judge sesn_01KnfTFzhhoL9HqEFW1PxNCV had no previous_verdict.json and the 'owner decisions not recorded' finding escalated from minor (A-r1 ISS-08) to major (B-r2 R2-1), making the human re-confirm at 11:43:54 decisions made at 11:02:24.

**Root cause.** Artefacts are named `{wid}-r{round}`/`r{round}` and run_specify restarts round_no=1 and verdict=None on each Gate 2 re-entry; the writer's spec.md is in res.outputs but is never carried in the spec object, so every consumer falls back to a lexicographic 'latest' lookup in the store that the reset defeats, and implement.py reads a field nothing writes.

**Fix.** orchestrator/factory/stages/specify.py `_write_one`: `spec["_markdown"] = res.outputs["spec.md"].decode()` (strip before validation/persist) and use it in `_judge`; runner.py `implement_and_integrate` passes spec_md into run_implement_one for the implementer, reviewer and merge-judge uploads (implement.py:320, :390, mj_uploads); prefix artefacts with a gate-cycle counter (`{wid}-c{cycle}-r{round}-`, `c{cycle}-r{round}-verdict.json`) or make rounds monotonic across cycles, and have RunStore.put_artifact refuse to overwrite a different content under the same name; pass the previous cycle's final verdict and a round offset into run_specify so the first judge after a send-back sees previous_verdict.json + human_comments.md together; add an orchestrator-side assertion that no upload is empty. Test in orchestrator/tests/test_specify_parallel.py: call run_specify twice (second with prior_specs) and assert the judge's specs/<id>.md upload equals that round's writer output. Longer term: render spec.md from spec.json in the orchestrator so the writer produces one artefact.

Files: `orchestrator/factory/stages/specify.py`, `orchestrator/factory/runner.py`, `orchestrator/factory/stages/implement.py`, `orchestrator/factory/store.py`, `orchestrator/tests/test_specify_parallel.py`

Saves: One writer+judge round per Gate 2 send-back (cycle B r2: $0.99 + $2.56, ~22 min on run_57fde1adc2); the human stops reviewing a stale spec.md; implementers and reviewers stop re-deriving a 24 KB contract from JSON; provenance of what the owner approved is restored.

### 2. Event-log seq numbers collide when sessions run in parallel (reproduced); replay then rejects the run, which kills state(), resume and the Console on the first multi-item run

**high** · orchestrator

Evidence:

- orchestrator/factory/store.py:45-49: `existing = self.events(run_id); seq = existing[-1].seq + 1 if existing else 0; … with self._events_path(run_id).open("a") as f: f.write(json.dumps(ev.to_dict()) + "\n")` — read-modify-write with no lock (`grep -rn Lock|threading orchestrator/factory` finds nothing); state.py:191-192 `if ev.seq != state.last_seq + 1: raise ValueError(f"event seq {ev.seq} out of order …")`.
- Reproduced now against the real RunStore with ThreadPoolExecutor(5): `seqs from 5 threads: [1, 1, 1, 1, 2]`. Both live paths emit from worker threads: specify.py:139-140 `ThreadPoolExecutor(max_workers=min(ctx.policy.parallel_spec_writers, len(batch)))` where each _write_one emits work_item.stage at specify.py:51, and implement.py:422 `ThreadPoolExecutor(max_workers=ctx.policy.reviewer_passes)` where each pass calls persist_outputs (~8 events in a burst). run_57fde1adc2 survived by luck: reviewer p1 burst seq 71-77 at 12:25:15.3, p2 burst 78-86 at 12:25:20.4; no run today had more than one work item on the parallel-writer code. store.state() is called by run_decompose, implement_and_integrate (runner.py:268 list_cost_usd), resume() and the Console.

**Root cause.** RunStore.append derives seq by re-reading the file and appends without mutual exclusion while RunContext.event is called from worker threads (and from other processes: Console, `factory resume`).

**Fix.** orchestrator/factory/store.py: keep a per-run threading.Lock plus an fcntl.flock on events.jsonl around append, cache the last seq in memory instead of re-reading the file, and add a test that appends from N threads and asserts contiguous seqs. Do this before the next run with more than one work item.

Files: `orchestrator/factory/store.py`, `orchestrator/factory/stages/__init__.py`, `orchestrator/tests/`

Saves: Prevents a crash or an unreadable event log on the first multi-item (Switchboard-style) run; no spend change.

### 3. Rubric S12 has no honest path for user-visible behaviour that cannot be verified in one run against a deployed instance; it produced the only round-1 blocker, the Gate 2 send-back, a second blocker after the writer did what the owner asked, and finally a preview entry that cannot fail

**high** · agent brief or rubric

Evidence:

- Cycle A r1 judge sesn_01SyjddpEZarkfJc2ts9PZep (10:14:47) S12=0: "AC-10 is flagged user_visible=false, with the verification_note 'Not checkable on a deployed service because e2e cannot advance the server clock'. Its Then can be observed over HTTP…" → ISS-01, the only blocking issue. Gate 2 request_changes 11:02:24 item (2): "remove the two preview rollover entries (seed / verify). No single-run check against a live instance can observe a date rollover, and a non-zero exit from rollover-verify would be counted as a failed check. The clock-patched pytest tests plus the `rollover-local` entry…". Cycle B r1 verdict I-2 blocking: "AC-10 … is honestly flagged user_visible, but neither artifact gives it a preview check" (S12=0 again).
- Same judge agent, same decomposition, opposite call: run_4c9e37ea11/artifacts/judge/r1-verdict.json "AC-13 was considered for 0 under the 'flagged false to dodge e2e' rule. It is flagged false on testability grounds ('Exact instants need a movable clock')… " → S12=1, pass. rubrics/spec.md:83 "0: a `user_visible` criterion has no e2e entry, or a criterion that an external system or user plainly observes is flagged `false`"; agents/spec_judge.md:33 "flags a plainly observable behaviour as not `user_visible` to dodge an e2e entry, scores 0 there". Cycle B r2 verdict S12=1: "AC-10 'listing' '(for AC-10: per-request invariant only)': within one UTC day on a fresh instance, a build that stores the flag at creation passes" — a preview entry added to satisfy the anchor. checks.py e2e_coverage counts local entries; the rubric does not.

**Root cause.** The rubric and judge brief equate 'observable by a user' with 'checkable against a preview in one verification run'. Time-progression behaviour is observable but not single-run testable, so both flag values score 0 and the only exit is an owner waiver the schema cannot record.

**Fix.** rubrics/spec.md S12: add an anchor — a user_visible criterion whose deployed check is physically impossible within one verification run (time progression, external events) scores 2 with a `target: local` e2e entry plus a verification_note naming the reason and the stand-in evidence; score 0 only when the behaviour is single-run checkable and the flag is false or the preview entry is missing; align the wording with checks.e2e_coverage. agents/spec_judge.md: replace the 'dodge' sentence with the same rule. Optionally add `e2e_waiver: {reason, evidence}` to test_plan in schemas/spec.schema.json so the owner's decision lives in the artefact rather than in gate comments.

Files: `rubrics/spec.md`, `agents/spec_judge.md`, `agents/spec_writer.md`, `schemas/spec.schema.json`, `orchestrator/factory/checks.py`

Saves: Cycle A r2 writer+judge ($0.58 + $2.92, ~18 min) and the Gate 2 send-back that started cycle B ($6.47, ~41 min together with issue 1) on run_57fde1adc2; two extra writer grader iterations (~$0.35) on run_4c9e37ea11.

### 4. Rubric I5 ('each slice leaves the test plan green') is unsatisfiable against spec test plans that assert file existence, docs and e2e; the in-session grader enforced it literally, driving a 15-minute history rewrite and force-push, then a reviewer nit about the rewritten history, then a second rewrite and force-push

**high** · agent brief or rubric

Evidence:

- sesn_01BVeVGShXZXmDKDndRqboLM grader iteration 0 (11:58:06) needs_revision: "3 of 5 checkpoints leave the test plan red. pytest alone is green at every commit." with every deterministic item passing in the same explanation. 12:02:13: "the branch history has to change so that every commit leaves all 11 test-plan commands green"; 12:05:29: "The app code at the end of slice 3 is byte-identical to the previously graded head"; 12:06:03 tool_use `git push --force-with-lease` after rebuilt slices; session.usage list_cost 244 → 538 cents: the revision cost $2.94, 15 min, 61,534 output tokens, zero code change.
- rubrics/implementation.md:27-28: "each leaving the test plan green. - 0: a single squashed commit for a multi-criterion spec, or a commit that leaves tests red." The spec's test_plan.commands include `test -f tests/test_overdue.py && test -f tests/e2e_overdue.py` and `python tests/e2e_overdue.py local-all`, which cannot pass before the slice that creates those files. The rewrite then drew r1-p1 F-2 (nit): "Intermediate commit d33b244 declares `due_date: date | None = None`…" with refutation "I withdraw this if the branch is squash-merged"; the r2 implementer rebuilt and force-pushed again (12:29:33-12:35:24, f3_history_rebuild.out) and both r2 reviewer passes re-ran the full plan at every rebuilt commit; the r2 grader then flagged "the first commit is about 515 changed lines, well above the 100-200 guide".

**Root cause.** I5 is handed verbatim to the platform grader (max_iterations=4) and demands the whole spec test plan green at every intermediate commit, while spec writers are free to put file-existence/doc/e2e assertions in test_plan.commands; nothing forbids rewriting pushed history to satisfy a grader, and nothing tells reviewers the merge policy, so intermediate-commit observations become findings.

**Fix.** rubrics/implementation.md I5: 'green' = the repository's protected tests pass at each commit and the full test_plan passes at HEAD; make it a 1-vs-2 distinction that does not block the outcome (or move it to merge-judge advisory). agents/implementer.md: never rewrite pushed history to answer a grader or reviewer; append commits. agents/spec_writer.md step 6 / rubrics/spec.md S7: test_plan.commands must be runnable at any commit (no `test -f`, no docstring/README asserts; put those in static_checks). agents/reviewer.md + implement.py review description: state the merge policy (squash|merge); intermediate-commit observations are nits only under non-squash and never require a response.

Files: `rubrics/implementation.md`, `agents/implementer.md`, `agents/spec_writer.md`, `rubrics/spec.md`, `agents/reviewer.md`, `orchestrator/factory/stages/implement.py`

Saves: ~$3 and ~15 min per implementation (54% of the r1 implementer session on run_57fde1adc2), plus the r2 rewrite and the per-commit re-verification in both r2 reviewer passes; no more force-pushes of factory branches.

### 5. ac_test_mapping (rubric I1) is now vacuous: the 'passing tests' set is derived from the implementer's own report, so any mapping passes, including fabricated test ids

**high** · orchestrator

Evidence:

- orchestrator/factory/stages/implement.py:373 `passing_tests = [t for ts in checks.criteria_mapping(report).values() for t in ts]  # refined by a CI parser later`; checks.py:237 `passed = set(tests_that_passed)` … :247 `missing = [t for t in tests if t not in passed]` — the check tests the report's list against itself.
- Reproduced now with the current checks.py and run_57fde1adc2's final spec: a report mapping every AC to `tests/does_not_exist.py::nope` returns `ac_test_mapping passed=True, 'all automated criteria map to passing tests'`. The in-session grader had already warned (sesn_01BVeVGShXZXmDKDndRqboLM 11:58:06): "Several mapped test names are not real pytest node IDs, which could break the orchestrator's automated existence check." The two fixes today (242a83f, fd68eeb) widened which report keys are read, moving the check from 'always fails' (which cost ~$8.5/48 min, issue 6) to 'always passes' without adding verification.

**Root cause.** The deterministic I1 check was designed as 'orchestrator confirms each named test exists and passed on head_sha' but the orchestrator never parses its own pytest run; the passing set is a copy of the report's mapping.

**Fix.** implement.py run_implement_one: derive passing_tests from the orchestrator's own run — append `--junitxml=<tmp>/junit.xml` (or `-rA`) to each pytest command in LocalVerifier.run_commands and parse passed node ids; also run `python -m pytest --collect-only -q` and report mapped ids that are not collected as 'tests not found'. Until then, have deterministic_findings mark ac_test_mapping advisory (not blocking) so the merge judge treats it as a report defect. Add schemas/implementation_report.schema.json so the implementer can validate the report in-sandbox like every other artefact.

Files: `orchestrator/factory/stages/implement.py`, `orchestrator/factory/checks.py`, `rubrics/implementation.md`, `schemas/`, `agents/implementer.md`

Saves: Correctness only: closes a hole where a fabricated or stale criteria mapping merges as 'all automated criteria map to passing tests'.

### 6. Any failing deterministic check, including environmental ones the orchestrator could re-verify in seconds (a busy host port), takes the same path as a real blocking defect: a full implementer round plus two fresh xhigh reviewer passes over the whole diff

**high** · orchestrator

Evidence:

- run_57fde1adc2/artifacts/review/spec_wi-due-dates-overdue-r1-findings.merged.json: [('F-1','minor','open'), ('F-2','minor','open'), ('F-3','nit','open'), ('DET-ac_test_mapping','blocking','open')]; implement.py:501 `"finding_id": f"DET-{name}", "severity": "blocking"` and :432-433 `standing = [... f["severity"] in ("blocking", "major")]; if not standing and all(d.passed for d in det): break`. That single DET entry (the known, fixed mapping bug) bought implementer r2 sesn_0113PgU49waf9hoRSUDhyPEv ($3.31) + reviewers r2 sesn_01DzT6s14PgcAsuUn6DUwNRB ($2.86) and sesn_01MVuQtzgYXW8gMPXMJBpajR ($2.37) + the crash/resume gap: 12:25:32 → 13:13:08, ≈$8.54 and 48 min, 26% of the run.
- The next environmental trigger is already on disk: run_4c9e37ea11/artifacts/implement/spec_wi-due-dates-overdue-r1-test_results.json, command `python -m uvicorn app.main:app --port 8765 & pid=$!; sleep 2; python tests/e2e/overdue_e2e.py http://127.0.0.1:8765; …` → exit 2, "ERROR: [Errno 48] error while attempting to bind on address ('127.0.0.1', 8765): address already in use"; the same script passed in the implementer sandbox (sesn_01HP6jF3Q8JtLwheYzAPH9Da 09:19:26, 'exit 0 PASS') and the orchestrator's own LocalPreview e2e passed on a free port, so r1-deterministic_checks.json `test_plan_commands passed=false` would have become DET-test_plan_commands blocking and forced a round for a host-side collision. LocalVerifier.run_commands uses `subprocess.run(cmd, shell=True, ...)` with no process group, so a backgrounded server from such a command can outlive `kill $pid`.

**Root cause.** The review loop has one escalation path: deterministic failures (cheap to re-verify) and reviewer findings (need a fresh adversarial read) are merged into one list and both trigger the most expensive round shape; spec writers may bind fixed ports in test_plan.commands and the orchestrator runs them verbatim on the developer's machine without process isolation.

**Fix.** implement.py: (a) when every standing item is DET-* and no agent finding is blocking/major, run an implementer response round, re-run checks.run_all_for_merge, and go straight to the merge judge if it passes — no review round; (b) for round_no ≥ 2 upload `delta.patch` (previous head → new head) and run one reviewer pass scoped to 'adjudicate prior findings against their refutation conditions, then review delta.patch only' (Policy.reviewer_passes_followup=1 in state.py); (c) run each test_plan command with start_new_session=True and os.killpg afterwards, and tag DET findings whose stderr contains 'address already in use' as environmental. agents/spec_writer.md step 6 / rubrics/spec.md S7: commands must not bind fixed ports or assume host state; a local e2e entry starts the service on a free port itself.

Files: `orchestrator/factory/stages/implement.py`, `orchestrator/factory/state.py`, `agents/spec_writer.md`, `rubrics/spec.md`

Saves: ~$5-8 and 20-45 min per run in which a deterministic check fails; on run_57fde1adc2 the whole $8.54/47 min round would have been one re-check.

### 7. In FACTORY_PREVIEW=local the 'preview' is a loopback process on the orchestrator host that the sandbox cannot reach, so every user_visible criterion is labelled not_assessed and that table is what PR #1 shows (AC-1..AC-10), although the reviewers, the merge judge and the orchestrator all ran the 9 preview entries green

**high** · orchestrator

Evidence:

- Reviewer r1 p1 sesn_01GE8BycnTE1VnLb7QB7i3Dr checks_performed: "curl http://127.0.0.1:53217/health (the URL in preview.json, status 'ready', mode 'local') … Connection refused (curl exit 7)" and, in the same pass, "9/9 pass on the first run, 9/9 pass on the replay" against a manifest-entrypoint instance it started itself — yet AC-1..AC-10 are not_assessed because agents/reviewer.md step 4 says "a `user_visible` criterion you could not exercise against the preview is `not_assessed`, never `met`" and the orchestrator's LOCAL_PREVIEW_CLAUSE (implement.py:62) tells it to "record the \"preview\" entries as not run". r2-test_results.json records all 9 preview e2e entries exit 0 at http://127.0.0.1:54991.
- Integrator sesn_013mWCen1wijTD2zFn4HRc5b open_change_request body: "Status is the reviewer's (`findings.json` spec_conformance). The reviewer could not reach the orchestrator's local preview instance, so it marked AC-1..AC-10 `not_assessed`" (not_assessed appears 11 times in the PR body). schemas/merge_readiness.schema.json has no per-criterion table; the merge judge wrote one by hand in judge_notes.md ("The merge_readiness schema has no field for criterion conformance, so this table records where I found the evidence for each criterion"), which integrate.py never uploads to the integrator.

**Root cause.** Three gaps compound: preview.json claims a ready URL the sandbox cannot reach; the reviewer brief and clause make the label depend on where the command ran rather than what it proved; and the merge-readiness schema has no criteria table, so the integrator renders the reviewer's not_assessed column instead of the orchestrator's authoritative covers_ac results.

**Fix.** implement.py LOCAL_PREVIEW_CLAUSE + preview_info(): in local mode set status 'unreachable_from_sandbox', url null, and say the manifest-entrypoint instance the reviewer starts is the preview stand-in; agents/reviewer.md step 4: run preview entries against that instance, record target 'local-manifest', mark met/not_met on it (not_assessed only when the entrypoint fails to start). schemas/merge_readiness.schema.json: add `criteria: [{ac_id, status, evidence}]` and have agents/merge_judge.md step 4 fill it. agents/integrator.md + templates/cr_template.md: render the AC table from merge_readiness.criteria plus test_results.json e2e covers_ac, with the reviewer's status as a secondary column; drop the 'clean sandbox' and dev-deploy sentences when preview_mode is local.

Files: `orchestrator/factory/stages/implement.py`, `agents/reviewer.md`, `agents/merge_judge.md`, `agents/integrator.md`, `schemas/merge_readiness.schema.json`, `templates/cr_template.md`

Saves: No spend change; the PR stops telling the human its criteria were not verified when they were, and the judge stops hand-writing a side artefact.

### 8. Severity calibration: a reproduced concurrency defect (torn done/overdue in POST responses) was rated 'minor', and the loop only continues on blocking/major, so with the mapping bug fixed this race would have shipped as 'ready'

**medium** · agent brief or rubric

Evidence:

- run_57fde1adc2/artifacts/review/spec_wi-due-dates-overdue-r1-p1-findings.json F-1: severity 'minor', category 'concurrency': "Todo.to_dict() reads `done` twice without the store lock: once in asdict(self), again in is_overdue(). POST /todos calls it after TodoStore…"; the merge judge later reproduced it: "With the round-1 to_dict … it gives 70 torn in 20000, and test_post_response_stays_consistent_when_completed_while_it_is_built fails"; the implementer fixed it in e563e95 — only because the false DET blocker forced round 2.
- agents/reviewer.md:48: "`blocking` breaks a criterion or corrupts data; `major` is a real defect not covered by a criterion, or a security or concurrency problem; `minor` is a correctness nit with a clear fix" — by its own words this was at least major. implement.py:432 `standing = [... f["severity"] in ("blocking", "major")]` and agents/merge_judge.md 'ready: no blocking or major finding stands'; the merge judge's R2 calibration audit did not re-rate F-1.

**Root cause.** The reviewer under-rated against its own brief and nothing downstream (merge-judge calibration audit, orchestrator standing rule) catches a mis-rated real defect; the continue/stop decision rests solely on the reviewer's label.

**Fix.** agents/reviewer.md Severity: any reproduced concurrency, data-consistency or security defect is at least `major` regardless of how rarely it triggers, and a finding whose `violates` names a spec interface or criterion is `blocking`. agents/merge_judge.md step 2: explicit R2 calibration pass — a reproduced finding with category concurrency/security/data_integrity counts as standing even if labelled minor, and the judge records the re-rating. implement.py:432: include open findings with category in (concurrency, security, data_integrity) in `standing` irrespective of severity.

Files: `agents/reviewer.md`, `agents/merge_judge.md`, `orchestrator/factory/stages/implement.py`, `rubrics/review_quality.md`

Saves: Correctness: prevents merging a reproduced race as 'ready'.

### 9. Spec judge is the most expensive stage ($15.23, 30% of all spend): Opus at effort xhigh spends ~80% of its output on hidden reasoning, builds a prototype every round, re-judges all 12 criteria from scratch in round 2, and emits a 27-34 KB quote-heavy verdict through a builder script

**medium** · cost or time

Evidence:

- sessions/index.json: judge 6 sessions $15.23 (30.4% of $50.03), 81 active min, 3,564 output tokens per request (specify 2,029; review 1,404); on run_57fde1adc2 judge:writer spend is $9.66 : $2.72 and judge sessions were 51 of the 66 min the two spec cycles took, on the critical path before Gate 2. One request in sesn_01NRZtvf9sAyPJnFwXAJEqb9 emitted output_tokens=33104 whose only visible product was `which python python3; python3 --version; ...`; sesn_01PTXP2jnz8WnwmpBXwBkzEp request output_tokens=25462 produced only `find / -path /proc -prune ...`.
- Round 2 costs more than round 1 despite previous_verdict.json: r1 $2.02/708 s (sesn_01SyjddpEZarkfJc2ts9PZep) → r2 $2.92/841 s (sesn_01NRZtvf9sAyPJnFwXAJEqb9, "I'll build a scratch clone and implement the spec as literally written"); cycle B r1 $2.16 → r2 $2.56/841 s; each live judge built a full prototype ("I built the change in a scratch clone of 6dec694 … All 11 commands exit 0 (29 tests)"). agents/spec_judge.md: `effort: xhigh`, "Quote the spec text that justifies each score" → per-criterion evidence of 600-1,000+ chars; the 5-spec design-only judge stopped `budget_reached` (324 vs 320 cents) while still patching verdict.json. The Codebase-researcher roster in spec_judge.md was never used (tools in all six sessions: bash/read/write/edit only).

**Root cause.** xhigh effort plus an unbounded 'check it' mandate, a quote-everything verdict format with no length cap, and a round-2 description that gives no permission to carry unchanged scores forward; the judge budget is per round, not per spec.

**Fix.** agents/spec_judge.md: `effort: high` (xhigh only via model_override for round ≥ 2 or sets > 3 specs); cap per-criterion evidence at one sentence with path/AC references (add maxLength ~400 to criteria[].evidence in schemas/judge_verdict.schema.json); 'run the test_plan commands that exist on main; build a prototype only when a score hinges on it'; write verdict.json directly with the write tool. specify.py `_judge`: for round_no > 1 upload a unified diff r{n-1}→r{n} and change JUDGE_DESCRIPTION to re-score only criteria below 2 or whose text changed, copying the others forward; judge each spec in its own parallel session with a short coherence pass, and scale stage_budget_usd['judge'] by len(specs). Measure that round-2 pass/revise decisions are unchanged over the next two runs.

Files: `agents/spec_judge.md`, `schemas/judge_verdict.schema.json`, `orchestrator/factory/stages/specify.py`, `orchestrator/factory/state.py`, `rubrics/spec.md`

Saves: ~$1-1.5 and 5-8 min per judge session from effort and evidence changes alone; ~$5-7 and ~20 min on a run shaped like run_57fde1adc2; removes the budget cliff for multi-item runs.

### 10. Round-2 review runs two identical xhigh passes that re-prove the same four withdrawals with near-identical probes on a two-commit diff, and the merge judge then re-runs the same reproductions a third time

**medium** · cost or time

Evidence:

- r2 p1 sesn_01DzT6s14PgcAsuUn6DUwNRB ($2.86, 737 s) wrote probe_race_post_response.py, mutants_round2.sh, f3_every_commit.sh, f2_zone_choice_check.py; r2 p2 sesn_01MVuQtzgYXW8gMPXMJBpajR ($2.37, 704 s) wrote probe_race_post_response.py, f2_local_clock_mutants.sh, f3_ac9_every_commit.py, f2_zone_choice_check.py — the same four re-adjudications (F-1, F-2, F-3, DET) with the same verdicts; p2 final message: "I withdrew all four round-1 findings, and I found nothing new to raise in the round-2 changes". The round-2 diff was two commits (e563e95, 93ced45). Round 1, by contrast, was complementary: p1 found the to_dict race, p2 found the untested UTC half via mutation.
- Merge judge sesn_017ab1VHPvv1WSucha69Aomh then re-ran the race probe, the round-1 to_dict mutant, three host-date mutants and the d33b244 ancestry check although agents/merge_judge.md step 3 says "spot-check one or two that were originally blocking" (none were). Reviewers cost $10.09 of the run's $32.90; env setup (venv/pip/uv) is 83-134 s of tool time in every pass (r1 p1 built three venvs); agents/reviewer.md `effort: xhigh` with 65-73% of output as hidden reasoning.

**Root cause.** REVIEW_DESCRIPTION gives both round-2 passes the identical mandate ('judge each previous finding against your refutation condition … then review the new hunks') and nothing scales pass count or probe budget to the size of the new diff; the merge-judge brief's spot-check rule is not followed when reviewers attach probe output.

**Fix.** implement.py one_pass(): for round_no > 1, pass 1 adjudicates previous_findings + review_response, pass 2 (if any) reviews delta.patch only with independent probes; or run a single pass when the round's diff is under a policy threshold (~200 lines). agents/reviewer.md: `effort: high`; 'scale the probe budget to the diff: under ~300 changed lines run the test plan once at HEAD, probe only branches the diff introduces, no multi-interpreter runs unless the manifest declares them'. agents/merge_judge.md: do not re-run withdrawn reproductions when the reviewer attached the probe output. Optionally ship a requirements lock or pre-built venv to cut the 80-130 s setup per pass.

Files: `orchestrator/factory/stages/implement.py`, `agents/reviewer.md`, `agents/merge_judge.md`, `orchestrator/factory/state.py`

Saves: ~$2.4 and 11-12 min per follow-up round (one pass instead of two), ~$3-4 and ~10 min per review round at effort high, ~1.5 min per pass from shared env setup.

### 11. Implementer sessions pay twice for verification: each in-session grader pass (~3 min, ~25% of the session) re-runs the [deterministic] rubric items the orchestrator recomputes seconds later, and the brief sets no proportionality for evidence, so nits get 240,000-round race probes, dual-interpreter runs and 12 evidence files (which also triggered the known outputs-page crash)

**medium** · cost or time

Evidence:

- span.outcome_evaluation_start/end: sesn_01HP6jF3Q8JtLwheYzAPH9Da 09:21:11-09:24:05; sesn_01BVeVGShXZXmDKDndRqboLM 11:55:24-11:58:06 and 12:10:51-12:13:28; sesn_0113PgU49waf9hoRSUDhyPEv 12:39:09-12:42:09 — 11.2 of 56.9 implementer minutes, ~$2.7 of $10.81. Grader explanations: "I checked everything in a fresh clone", "I2: all 11 commands exit 0 in a fresh worktree at 1ad4332", "All 9 preview e2e commands exited 0" — the orchestrator ran the same commands at 12:13:38 and 12:50:56 (artifacts/implement/*-test_results.json). sessions.py hands the grader the whole rubrics/implementation.md including sections marked '[deterministic] computed by the orchestrator'; I9 still says "read from the GitLab pipeline's environment" and the grader wrote "I cannot run the GitLab preview from here".
- sesn_0113PgU49waf9hoRSUDhyPEv, answering F-1 minor, F-2 minor, F-3 nit: 25 of 51 tool calls are evidence work — two venvs (py3.11 46 s, py3.12 15 s) and the full plan on both though the manifest names only python-3.12, race probes of 100,000 and 240,000 rounds ("none in 240,000 rounds"), a TZ mutant harness, every-commit AC-9 checks, and 12 files under /mnt/session/outputs/evidence/ (f2_local_clock_mutants.sh/.out, probe_race_post_response.py/.out, test_plan_py311.out, …) which pushed review_response.json onto page 2 and produced run.failed 12:42:28 → run.resumed 12:50:56 (8.5 min). sesn_01BVeVGShXZXmDKDndRqboLM ran a 4-mutant suite before any grader demand although rubric I1 asks only for red/green on base_sha.

**Root cause.** The outcome rubric sent in user.define_outcome is the unfiltered merge-judge rubric, so an opaque Opus grader re-executes checks that are exact and free on the orchestrator side; agents/implementer.md rewards 'make it easy to verify' and 'answer every finding' with no cap on evidence effort, no rule that nits/minors may be accepted_risk, and no restriction on what goes to /mnt/session/outputs/.

**Fix.** implement.py (or a `ctx.rubric('implementation', grader=True)` helper in stages/__init__.py): strip I2, I3, I4, I8, I9 from the grader rubric, keep I1/I5 (as corrected)/I6/I7, add 'do not clone, start the service or run mutation tests; verify from the report and the working tree'; consider outcome_max_iterations=2 for implement in state.py; fix the GitLab wording in I9. agents/implementer.md: evidence budget (red/green of new tests against base_sha once, in the report; no mutation runs, no extra interpreters, no per-commit reruns unless the spec asks); 'for nit or minor, fix only if one commit, else accepted_risk with one sentence'; 'write only implementation_report.json, review_response.json (and blocked.md) to /mnt/session/outputs/'. agents/reviewer.md: refutation conditions must be satisfiable by an existing test or a single command.

Files: `orchestrator/factory/stages/implement.py`, `orchestrator/factory/stages/__init__.py`, `rubrics/implementation.md`, `agents/implementer.md`, `agents/reviewer.md`, `orchestrator/factory/state.py`

Saves: ~2 min and 15-20% of cost per grader pass (~$1.5-2 and 6-8 min across the three implementer sessions); ~5-7 min and ~$1 per response round; no more outputs-listing overflow.

### 12. The platform-contract skill still never reaches a session on an existing repository: the brief says 'read the skill first' with no path, so every decomposer and judge spends 60-73 s on `find /`, delegates the hunt to a researcher thread, and escalates 'skill not available' at every Gate 1

**medium** · orchestrator

Evidence:

- sesn_018gnYfc2tTLKyQsX9gEi2b3 08:47:24→08:48:32 `ls -la /mnt/skills 2>&1; find / -path /proc -prune -o \( -iname "*platform-contract*" -o -iname "SKILL.md" …` (68 s); sesn_01SyjddpEZarkfJc2ts9PZep 10:04:02→10:05:09 (67 s); sesn_017fz7xYwqW6K8vQtRkRpkAf 73 s of a 107 s session; the post-fix Switchboard decomposer sesn_013D9xDj93EVjGEYxiuGZYxD at 16:10 still sees `/mnt/skills: total 4 … (empty)` and spent 5 of 12 tool calls hunting. In sesn_01DnmwanuQE13EvMvGfTDaxi a whole researcher thread (sthr_01RvL94cpHV8m6yDuDtmdmQP) was asked to 'search the filesystem outside the repo … for a document called a "platform contract"' and was the long pole (08:14:44 vs 08:12:28/08:13:22). Gate 1 in 4/4 completed runs carries a 'The platform-contract skill was not available in this session' question, which graders flag as the weakest D6 criterion.
- Commit 0e6ccd2 seeds the skill only inside ensure_repository, which returns at orchestrator/factory/forge/__init__.py:161-162 `if not repo.get("create"): return None` before the seed at :170-173; examples/sample-app/outcome.json repository `create: None`, so factory-sample-app runs will still have no skill; decompose.py:51-52 uploads only outcome.json/outcome.md (+prior/). agents/decomposer.md:42 "Read the `platform-contract` skill first" with no location (same in spec_writer.md:45, spec_judge.md:33, implementer.md:35, reviewer.md:36); the 16:43 Switchboard writer found it only at /workspace/repo/.claude/skills/platform-contract/SKILL.md.

**Root cause.** The orchestrator ships the skill neither in uploads nor in the agent's skills mount, and only seeds it into repositories it creates; the briefs make reading it step 1 without saying where it is or what to do if it is absent.

**Fix.** stages/decompose.py, specify.py and implement.py: add `uploads["skills/platform-contract/SKILL.md"]` from skills/platform-contract/SKILL.md and name the path in the kickoff; all five briefs: 'Read /mnt/session/uploads/skills/platform-contract/SKILL.md (or /workspace/repo/.claude/skills/platform-contract/SKILL.md) first; if neither exists, say so once in assumptions and do not search the filesystem'; forge/__init__.py: move the skill seed above the `create` guard so existing repositories get it too.

Files: `orchestrator/factory/stages/decompose.py`, `orchestrator/factory/stages/specify.py`, `orchestrator/factory/stages/implement.py`, `orchestrator/factory/forge/__init__.py`, `agents/decomposer.md`, `agents/spec_writer.md`, `agents/spec_judge.md`, `agents/implementer.md`, `agents/reviewer.md`

Saves: 60-170 s per decomposer/judge session (the only >20 s gaps that are not model or grader time), one researcher thread per decomposition, one fewer open question per Gate 1.

### 13. The 'authoritative' verification is not a clean sandbox: LocalVerifier and LocalPreview run the target repo's commands with the orchestrator's own virtualenv first on PATH, which the PR template describes as 'a clean sandbox' and which will break on the first repository whose dependencies differ (Switchboard)

**medium** · orchestrator

Evidence:

- implement.py:187 `env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"}` (LocalVerifier.run_commands) and :104 (LocalPreview.start); run_57fde1adc2 r2-test_results.json `local-all` stdout_tail: "starting TZ=<+14>-14 /Users/will/Desktop/factory_agents/orchestrator/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 54919" — the target app's e2e ran under the orchestrator's interpreter.
- templates/cr_template.md Verification: "Run by the orchestrator in a clean sandbox on `{{head_sha}}`"; PR #1 rendered the preview line as "http://127.0.0.1:54991, mode `local`, pipeline none; not a deployed environment" and emitted the 'After merge the orchestrator watches the platform's dev deploy' boilerplate although deploy_observe has no local mode. schemas/service_manifest.schema.json runtime has only kind/version/entrypoint/port — no setup/install command the verifier could use.

**Root cause.** FACTORY_PREVIEW=local was built for the sample app whose deps happen to live in the orchestrator venv; deterministic checks test_plan_commands and e2e_preview therefore depend on the host environment, not the repo's declared dependencies, and the template asserts isolation that does not exist.

**Fix.** implement.py LocalVerifier.checkout: create a per-run venv under verify/ from the repo's pyproject (uv venv + uv pip install -e .[dev]) or a `runtime.setup` command added to schemas/service_manifest.schema.json, put that venv first on PATH for run_commands and LocalPreview, and fail closed with a clear deterministic check when setup fails. templates/cr_template.md + agents/integrator.md: describe the actual mode ('orchestrator host, per-run venv' vs CI) and drop the dev-deploy sentence when preview_mode is local. Needed before the Switchboard run reaches its implement stage.

Files: `orchestrator/factory/stages/implement.py`, `schemas/service_manifest.schema.json`, `templates/cr_template.md`, `agents/integrator.md`

Saves: Prevents false test_plan_commands/e2e_preview failures (or passes) on any repo whose deps differ from the orchestrator's venv; each false failure would cost a full round (issue 6).

### 14. merge_findings de-duplicates by (category, first location) and renumbers every round, so round-2 findings lose identity: the same withdrawn finding appears twice, ids no longer match review_response.json, the DET entry vanishes, checks_performed from two passes are concatenated unattributed, and scope_check is dropped

**medium** · orchestrator

Evidence:

- implement.py:523 `key = (f["category"], f["evidence"]["locations"][0].split(":")[0] + ":" + f["evidence"]["locations"][0].split(":")[-1][:3])`; :533 `f["finding_id"] = f"F-{i}"`. Merged r2: [('F-1','minor','withdrawn'), ('F-2','minor','withdrawn'), ('F-3','minor','withdrawn'), ('F-4','nit','withdrawn'), ('F-5','nit','open')] while review_response.json answered ['F-1','F-2','F-3','DET-ac_test_mapping']: the withdrawn UTC-clock finding is both F-2 (loc tests/test_overdue.py:188) and F-3 (loc app/storage.py:10), round-1 F-3 became F-4, the DET entry is gone; merged checks_performed has 43 rows with no pass number; r1-p1's top-level scope_check is absent from the merged file.
- merge_readiness.json unresolved_findings had to be written as "F-2 (duplicated in findings.json as F-3)" and "F-4 (round-1 F-3, d33b244)"; residual_risks: "checks_performed also concatenates two round-2 passes with different figures (118/1007 vs 88/633) … a later audit should not rely on the ids".

**Root cause.** merge_findings was written for a round-1 union by location; in round 2 reviewers restate carried-over findings with their own first location, the orchestrator-injected DET finding is in no pass, and unconditional renumbering discards the ids the implementer answered to.

**Fix.** implement.py merge_findings: for round_no > 1 match carried-over findings on the id the reviewer kept (add `previous_finding_id` to schemas/review_findings.schema.json; agents/reviewer.md: keep prior ids verbatim), dedupe withdrawals/upholds by that id keeping the stricter status, number only new findings from max(existing)+1, keep DET-* ids and carry the implementer's disposition, tag each finding and checks_performed row with `pass`, carry scope_check through; unit test merging two round-2 passes with differing locations and asserting a single F-2.

Files: `orchestrator/factory/stages/implement.py`, `schemas/review_findings.schema.json`, `agents/reviewer.md`, `orchestrator/tests/`

Saves: Judge and integrator stop spending tokens reconciling ids; the audit trail becomes usable.

### 15. On judge-revise rounds the writer gets its own previous spec only by accident (uploaded as a 'sibling' through a key mismatch) and no explicit prior draft, so revisions become rewrites with new names that the judge re-verifies in full

**medium** · orchestrator

Evidence:

- specify.py:140 `{s: specs[s] for s in specs if s != w}` — `specs` is keyed by spec_id (`spec_wi-…`) and `w` is the work_item_id (`wi-…`), so a writer's own spec is never excluded (and with several items would leak every spec regardless of dependency); :142 `(prior_specs or {}).get(w) if round_no == 1 else None` so no prior/spec.json on judge-revise rounds. Both single-item round-2 writers list `sibling_specs` in `ls -R /mnt/session/uploads` (sesn_0174kkgGfVJ5DaBDHhmMR6go, sesn_016coJpjbDxi2abtYcs9dMem), which agents/spec_writer.md defines as "specs for items you depend on or that depend on you".
- Cycle A r2 writer renamed the clock seam (utc_today×16 vs r1's today_utc), the test file (tests/test_due_dates.py×7) and the store method (list_overdue×4): a rewrite the r2 judge re-verified in full ($2.92, the costliest judge session) and whose vocabulary later collided with r1's in the phantom I-1 (issue 1). Cycle B r2 writer, which happened to keep today_utc/test_overdue/list_dicts, got "Round-1 I-1 is resolved".

**Root cause.** Key mismatch in the sibling filter, no prior-spec upload on rounds > 1, and no 'revise in place' instruction in the writer brief.

**Fix.** specify.py: filter siblings by `specs[s]["work_item_id"] != w` (and by dependency relation per the brief); upload prior/spec.json on every round > 1; agents/spec_writer.md: 'On a revision, start from prior/spec.json and keep every name, signature and AC id the judge did not object to; record what changed in revision_notes.md'; unit test for the sibling filter with two work items.

Files: `orchestrator/factory/stages/specify.py`, `agents/spec_writer.md`, `orchestrator/tests/test_specify_parallel.py`

Saves: ~$1-2 of judge re-verification per revise round and fewer rounds; removes the I-1 class of consistency findings.

### 16. Run bookkeeping is wrong on every PR: session.finished carries no spec_id so the provenance table lists 5 of 17 sessions ($10.47 of $32.90) with no duration; the merge judge is always told 'round 1'; agent-written provenance placeholders ('sesn_unknown', model 'claude', invented timestamps) survive setdefault; interrupted sessions are never counted

**low** · orchestrator

Evidence:

- run_57fde1adc2/events.jsonl: all 17 session.finished rows have `"spec_id": null` (common.py:45 emits only stage/actor); runner.py:217 `if ev.kind == "session.finished" and (ev.spec_id == spec_id or ev.stage in ("decompose", "judge"))` → run_summary.sessions = decompose + 4 judge entries; PR #1 body: "Total list cost for the run: $32.90 (the sessions above sum to $10.47; the run total is from run_summary.json). Duration: not provided in run_summary.json." With several work items the same filter attaches every judge session to every PR and no implementer/reviewer session to any.
- implement.py:453 `round=len([1 for _ in range(1)])` → merge judge kickoff "round 1." after two review rounds and merge_readiness.json round 1; its produced_by.session_id is 'sesn_unknown' because implement.py:463 `verdict.setdefault("produced_by", provenance(...))` (same pattern decompose.py:66, specify.py:77/111, merged findings copy passes[0].produced_by at :540): run_57fde1adc2 r2-spec.json produced_at '2026-10-10T12:00:00Z' for a session that ended 11:28:27; the decomposer said so itself (sesn_01DnmwanuQE13EvMvGfTDaxi 08:17:02: "The ids recorded for who produced this file (`agent_decomposer`, `sesn_unknown`) are placeholders"). Sessions stopped by SIGTERM or crash (sesn_01UmNgA3s9MzkB5h35tXTedL $2.33, sesn_015g1NDcxEAzagTWdKPp9bV1 $0.55, sesn_01TPe7sW1VkDudUicBTH7Mua $0.21, sesn_017fz7xYwqW6K8vQtRkRpkAf $0.11, sesn_01GKhgTd3EAvyGhampVfE2qm $0.13) get no session.finished, so $3.33 is missing from run totals.

**Root cause.** persist_outputs does not thread work_item_id/spec_id into its events; the schemas require produced_by so agents invent one and the orchestrator defers to it; the merge-judge round is a placeholder expression; interrupt_active() sends user.interrupt without retrieving usage or emitting an event.

**Fix.** common.py persist_outputs: accept and forward work_item_id/spec_id (callers in implement.py/integrate.py pass them) and always overwrite produced_by.{agent_id,agent_version,session_id,model,list_cost_usd,produced_at} from StageResult (keep the agent's `inputs`); runner.py _sessions_for: include implement/review/merge_judge sessions for the spec and add duration from stage.entered/run.completed; implement.py: pass `round=round_no` to MERGE_JUDGE_DESCRIPTION and set verdict['round']; sessions.py interrupt_active: retrieve each session after the interrupt and emit session.finished with stop_reason 'interrupted' and its cost; templates/cr_template.md renders the new fields.

Files: `orchestrator/factory/stages/common.py`, `orchestrator/factory/runner.py`, `orchestrator/factory/stages/implement.py`, `orchestrator/factory/stages/specify.py`, `orchestrator/factory/stages/decompose.py`, `orchestrator/factory/sessions.py`, `templates/cr_template.md`

Saves: None in spend; correct provenance, cost and round numbers on every PR and in the Console.

### 17. budget_reached and max_iterations_reached are recorded but never acted on, and stage caps are either 5-10x actuals or (under FACTORY_BUDGET_SCALE=0.4) below one judge round

**low** · orchestrator

Evidence:

- run_83af9398ce judge sesn_01PTXP2jnz8WnwmpBXwBkzEp: session.usage list_cost 324 vs budget max_list_cost 320 (0.4 × $8), stop_reason budget_reached at +996 s while it was still `sed -i … build_verdict.py` at +984 s; events.jsonl 08:05:24 session.finished {stop_reason budget_reached, outcome_result null} followed by judge.verdict {round 1, revise, blocking 3} and writer r2 14 s later. run_3834b69e13's decomposer ended `max_iterations_reached` after 4 grader cycles ($2.33) and would have gone to Gate 1 unflagged had the run not crashed.
- state.py defines RunStage.PAUSED_BUDGET and reduce() handles `budget.reached`, web/app.py lists it, but nothing emits it; `grep -rn 'budget_reached|max_iterations_reached|outcome_result' orchestrator/factory` matches only sessions.py and common.py (recording). Live judge sessions cost $2.02-3.24 against a scaled cap of $3.20; unscaled caps are implement 40 vs max 5.38, review 15 vs 2.86, judge 8 vs 3.24.

**Root cause.** The driver treats every idle as success; caps were set before cost data existed and scaled uniformly.

**Fix.** common.py persist_outputs (or stage callers): on stop_reason 'budget_reached' emit `budget.reached` and open a budget gate instead of consuming the output (or send one 'write {output} now from what you have' message with a one-time +25% bump); attach outcome_result 'max_iterations_reached' to the gate summary. state.py Policy.stage_budget_usd from observed p95 × ~3 (judge 6, review 6, implement 15, specify 3, decompose 4, merge_judge 3); cli.py: FACTORY_BUDGET_SCALE never below a stage's observed p50; outcome_max_iterations per stage (decompose 2, specify 3, implement 3).

Files: `orchestrator/factory/stages/common.py`, `orchestrator/factory/sessions.py`, `orchestrator/factory/state.py`, `orchestrator/factory/cli.py`, `orchestrator/factory/runner.py`

Saves: Avoids a downstream round built on a cut-off verdict (~$1-3 per occurrence) and bounds a runaway session at ~$15 instead of $40.

### 18. Reviewer and merge-judge sessions are told to 'Commit as you go and git push -u origin <branch> when done', contradicting their read-only briefs

**low** · orchestrator

Evidence:

- orchestrator/factory/forge/github.py:28 GIT_SETUP: "Commit as you go and `git push -u origin {branch}` when done." is interpolated as {git} into REVIEW_DESCRIPTION and MERGE_JUDGE_DESCRIPTION (implement.py:44, :53, :75) and appears in every reviewer/judge user.message of run_57fde1adc2 and run_4c9e37ea11, followed by "You never edit source files."
- r2 p2 sesn_01MVuQtzgYXW8gMPXMJBpajR final message: "I didn't commit or push anything: a reviewer doesn't edit source, and adding review files to the branch would put files outside the allowed paths and fail AC-12's scope check." — a less careful pass would push probes and trip diff_scope/protected_tests on the next verification.

**Root cause.** One git clause is reused for every role; Forge.sandbox_git_instructions has no read-only variant.

**Fix.** forge/github.py (and gitlab.py): add a read-only variant ('mounted at /workspace/repo on {branch}; fetch/checkout only; never commit or push') and use it in implement.py for reviewer, merge_judge and integrator descriptions.

Files: `orchestrator/factory/forge/github.py`, `orchestrator/factory/forge/gitlab.py`, `orchestrator/factory/stages/implement.py`

Saves: Removes a brief contradiction and a scope-check hazard.

### 19. The spec-writer brief does not ask for what rubric S1 scores, so the platform grader sends 1 in 3 writers back for the same reason

**low** · agent brief or rubric

Evidence:

- span.outcome_evaluation_end needs_revision citing S1 in 4 of 12 writer sessions: sesn_01ToMwBGBr4PVfF5snXLBV1P iter 0 and 1 "S1 gap: the goal never names the outcome's success metrics … grep finds neither name in spec.json or spec.md"; sesn_019YUJzZ6U2xADZfMJHPE6Wm iter 0 "S1: The goal never names the success metrics … Under the rubric's wording that is a 0"; sesn_016g8wi838c7p6Ae8Zuhioq5 iter 0 "S1: the goal never names the metrics Backwards compatibility or Overdue visibility".
- rubrics/spec.md S1: "names the success metric(s) it serves. 0: … names no metric"; agents/spec_writer.md step 1: "Restate the item's intent in one paragraph as the goal" — no mention of metrics or of what would show the goal was missed. Writer sessions with a revision ran 268-453 s vs 106-221 s without.

**Root cause.** The rubric was tightened without updating the writer's instruction for the goal paragraph, so first drafts systematically fail S1 and learn the fix through the grader loop.

**Fix.** agents/spec_writer.md step 1: 'Name the outcome success metric(s) this item serves, using their exact names from outcome.json, and say what would show the goal was missed in production.' Optionally a deterministic pre-check in checks.py that each spec goal contains at least one outcome metric name.

Files: `agents/spec_writer.md`, `orchestrator/factory/checks.py`

Saves: ~$0.1-0.3 and 1-2 min per affected writer session (~$1 and ~6 min across today's 12).

### 20. Gate waits are 25% of a full run's wall clock (25-29 min per gate) for decisions recorded as pre-authorised proxy approvals of the defaults

**low** · cost or time

Evidence:

- run_57fde1adc2: gate_1 09:32:53→09:59:44 (1611 s), gate_2 #1 10:33:33→11:02:24 (1731 s), gate_2 #2 70 s — 3413 s of 13870 s; run_4c9e37ea11 gate_1 1511 s; run_149c48b75c gate_1 1731 s. Decisions read "Approved by Claude on the owner's instruction to run the pipeline through to a pull request. Use the defaults for the open questions…".
- common.py open_gate_and_wait polls every 10 s with no auto-decision path; every open question in these runs was non-blocking with a default (5, 6, 8 questions) and each decision accepted the defaults; the one human-made decision (run_83af9398ce) took 2m20s.

**Root cause.** Human/proxy decision latency, not orchestrator code: the run has no notion of a standing owner instruction, so an approval already given in words still waits for a click.

**Fix.** Add a policy flag (e.g. `--auto-decide gate_1=approve_if_no_blocking_questions,gate_2=approve_if_pass_0_blocking`) handled in stages/common.py open_gate_and_wait: when set and the condition holds, record gate.decided with actor 'policy' and the standing instruction as comments; keep the gate page for audit. Bench runs only.

Files: `orchestrator/factory/stages/common.py`, `orchestrator/factory/runner.py`, `orchestrator/factory/cli.py`, `orchestrator/factory/state.py`

Saves: ~55 min of wall clock per bench run; no spend change.

### 21. Codebase-researcher indirection is unconditional and its reports are thrown away: on a 100-line repo the decomposer waits ~3 min on threads, one decomposer deleted the report before outputs were fetched, and the writer and judge then re-read the same seven files

**low** · cost or time

Evidence:

- sesn_01DnmwanuQE13EvMvGfTDaxi: three researcher threads created 08:11:56-57, decomposer idle 08:11:58→08:15:04 (170 s of a 358 s session, ~$0.28 of $0.97 in threads) for app/main.py, app/storage.py, tests/test_todos.py; at 08:15:10 tool_use `mkdir -p /tmp/decomposer/research && mv /mnt/session/outputs/research/todo-api.md /tmp/decomposer/research/ && rmdir /mnt/session/outputs/research` — no run has a research artefact; specify.py:53-57 forwards nothing from research.
- Downstream re-reading: run_57fde1adc2 spec r1 sesn_01L8FFFFehSrMcaYTvuJuC8m 10 repo-file reads; judge r1 sesn_01SyjddpEZarkfJc2ts9PZep 22 reads across 21 tool calls; run_4c9e37ea11 spec r1 sesn_01ToMwBGBr4PVfF5snXLBV1P 24 reads in 13 tool calls. agents/codebase_researcher.md writes a file only 'when a slug is given'; the decomposer gave none in run_57fde1adc2 ('Write your report as your reply (no files needed)').

**Root cause.** The goal-blind researcher is designed to keep large-repo reading out of the lead's context, but is mandated regardless of repo size; no artefact type exists for research, so the only surviving evidence is the path:line citations copied into `notes`.

**Fix.** agents/decomposer.md and spec_writer.md: 'If /workspace/repo has fewer than ~30 source files, read it yourself; use the researcher only for larger repositories or multi-file questions'; when used, give each researcher a slug and leave /mnt/session/outputs/research/*.md in place; decompose.py returns the research files and specify.py uploads them to writers as `research/<name>` (inputs, not proof — note in rubrics/spec.md).

Files: `agents/decomposer.md`, `agents/spec_writer.md`, `agents/codebase_researcher.md`, `orchestrator/factory/stages/decompose.py`, `orchestrator/factory/stages/specify.py`

Saves: ~$0.2-0.3 and 1-3 min per decomposer/writer session on small repos; 1-2 min and a handful of tool calls per writer session on larger ones.

## What worked and should not be touched

- Decomposition on a mounted repository is right-sized, fast and cheap — do not touch common.py NO_REPO_RUBRIC_NOTE, the rubric D2 anchors, or the one-vertical-slice guidance: after those landed, run_4c9e37ea11, run_57fde1adc2 and run_149c48b75c each produced exactly one vertical-slice item, grader satisfied at iteration 0 (26-54 s), $0.81-0.97, 261-358 s; parallel Codebase-researcher threads returned path:line evidence in 30-80 s that graders independently verified (D4 = 3; 'storage.py:10-17 is the Todo dataclass … There is indeed no GET /todos/{id} route'); deterministic checks (schema, DAG, metric coverage) passed first time on all four completed decompositions; artefacts matched the agent's final message in every run; the greenfield decomposer kept the skeleton inside the single item and recomputed the owner's figures. Contrast: the pre-note run_3834b69e13 looped 4 grader iterations for $2.33 and invented spike/test-only items.
- Round-1 adversarial review, the merge judge and the integrator find and prove real defects by execution and should keep their shape: r1 p1 (sesn_01GE8BycnTE1VnLb7QB7i3Dr) found the to_dict done/overdue race and p2 (sesn_01G73xDftZH8ms5zWGzBbUpR) found via mutation that no test pinned the UTC half of the rule, with zero overlap; both were fixed with regression tests (e563e95, 93ced45); every pass ran the full 11-command plan from a clean clone, verified red-on-base, validated findings.json, listed 20-23 repeatable checks_performed and stayed read-only (probes persisted under artifacts/review/ for re-running); the merge judge at effort high ($0.93, 263 s) reproduced the withdrawals itself ('0 torn in 20000 at head … 70 with the round-1 to_dict') and wrote an honest summary_for_human disclosing the preview caveat; the integrator (Sonnet, $0.28, 85 s) confirmed branch head == judged sha, got the custom-tool round trip (requires_action → open_change_request → cr.json) right first time, and disclosed every judgement call. Spec judges likewise caught real defects by running code (pydantic bare-date trap, {"due_date": 0} → 1970-01-01, a must-pass command that fails on a correct build).
- The platform plumbing and orchestrator overhead are solid — leave sessions.py's driver, interrupt_active, resume(), persist/verify hand-offs and the cost bookkeeping alone: the github_repository mount and git proxy handled branch create, incremental pushes and two --force-with-lease pushes with no token in the sandbox; permission_policy auto produced 0 pauses across 139 implementer tool calls and 0 denials; SIGTERM → user.interrupt reached in-flight sessions within 1 s (4 interrupted sessions cost $1.00 total); the 12:42 crash resume reused persisted artefacts and re-ran nothing twice on the way to PR #1; session create was 8-16 s and fetch+persist+local verification (clone, pytest, uvicorn preview, 9 e2e entries) 0.2-6 s between every pair of sessions; orchestrator-recorded cost matched the platform to the cent for all 17 finished sessions of run_57fde1adc2 and repricing every model request reproduces $50.03 within 1.2%; 93% of input tokens are cache-served; reviewer passes and (since 165128b) spec writers genuinely run in parallel.

## Action list, in order

1. specify.py: set spec['_markdown'] from res.outputs['spec.md'] in _write_one and use it for the judge; carry spec_md into the implementer, reviewer and merge-judge uploads; prefix artefacts with a gate-cycle counter so cycle B never overwrites cycle A; pass the previous cycle's verdict and a round offset into run_specify; assert no upload is empty; add the two-cycle test to test_specify_parallel.py. (Issue 1; also fixes the sibling filter and prior/spec.json on rounds > 1, issue 15.)
2. store.py: threading.Lock + flock around RunStore.append with a cached last seq, and an N-thread contiguity test — before any run with more than one work item. (Issue 2.)
3. rubrics/spec.md S12 + agents/spec_judge.md: add the 'not single-run checkable → local entry + verification_note scores 2' anchor and align with checks.e2e_coverage; add the S1 metric sentence to agents/spec_writer.md step 1. (Issues 3, 19.)
4. rubrics/implementation.md I5 → protected tests green per commit, full plan at HEAD, non-blocking; agents/implementer.md: never rewrite pushed history, evidence budget, only named outputs to /mnt/session/outputs/; strip the [deterministic] sections from the grader rubric and fix the GitLab wording; spec_writer.md/rubrics S7: test_plan.commands runnable at any commit, no fixed ports. (Issues 4, 11, 6c.)
5. implement.py deterministic path: derive passing_tests from the orchestrator's own junit/-rA pytest output (+ --collect-only for unknown ids) and add schemas/implementation_report.schema.json; DET-only failures → implementer response + re-check, no review round; round ≥ 2 → one reviewer pass over delta.patch; run commands with start_new_session + killpg and tag 'address already in use' as environmental. (Issues 5, 6, 10.)
6. Severity: reviewer.md (reproduced concurrency/security/data ≥ major; violates a criterion → blocking), merge_judge.md R2 calibration pass, implement.py standing includes reproduced concurrency/security findings regardless of label. (Issue 8.)
7. Local preview truthfulness: LOCAL_PREVIEW_CLAUSE + preview_info() say 'unreachable from sandbox, manifest-entrypoint instance is the stand-in'; reviewer.md step 4 marks met/not_met on it; add merge_readiness.criteria and have the integrator render the AC table from it plus test_results covers_ac; fix cr_template's 'clean sandbox' and dev-deploy wording. (Issues 7, 13 wording.)
8. LocalVerifier: per-run venv from pyproject or a runtime.setup manifest field, fail closed — required before the Switchboard run's implement stage. (Issue 13.)
9. Spec judge: effort high, one-sentence evidence with maxLength in judge_verdict.schema.json, prototype only when a score hinges on it, round ≥ 2 gets spec.diff and re-scores only changed/low criteria, per-spec parallel judging with budget scaled by len(specs); reviewer effort high with a diff-scaled probe budget; merge judge spot-checks only. (Issues 9, 10.)
10. Ship skills/platform-contract/SKILL.md in every stage's uploads with its path in the kickoff and the 'if absent, say so once, do not search' line in all five briefs; move the skill seed above the create guard in forge/__init__.py. (Issue 12.)
11. Bookkeeping batch: merge_findings stable ids + pass tags + DET carry-through; persist_outputs(work_item_id, spec_id) and _sessions_for including implement/review/merge_judge sessions + duration; always overwrite produced_by; merge judge round=round_no; interrupted sessions emit session.finished; budget_reached → budget gate and recalibrated caps; read-only git clause for reviewer/judge/integrator; researcher slug + forwarded reports and a repo-size threshold; optional --auto-decide gate policy for bench runs. (Issues 14, 16, 17, 18, 20, 21.)
