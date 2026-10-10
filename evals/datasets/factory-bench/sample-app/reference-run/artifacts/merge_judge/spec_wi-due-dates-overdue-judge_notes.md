# Merge judge notes: spec_wi-due-dates-overdue at 93ced45 (verdict: ready)

The merge_readiness schema has no field for criterion conformance, so this table records where I found the evidence for each criterion.
Local runs: clean clone at 93ced45 with main = 6dec694, Python 3.11.16, fastapi 0.143.0, pydantic 2.14.0. All 11 test_plan commands exit 0 there.

| AC | Status | Located evidence |
|----|--------|------------------|
| AC-1 | met | test_title_only_and_unknown_field_keep_existing_behaviour; local-all `compat`; orchestrator e2e `compat` exit 0 |
| AC-2 | met | test_future_and_null_due_dates_are_echoed_and_not_overdue; e2e `create-future` |
| AC-3 | met | test_past_due_date_is_accepted_and_overdue; e2e `create-past` |
| AC-4 | met | test_overdue_is_strictly_before_today[day,month-edge,year-edge]; test_today_utc_is_the_utc_date_not_the_host_date; e2e `boundary` (local-all with server TZ=<+14>-14) |
| AC-5 | met | test_listing_obeys_rule_for_every_element; test_list_endpoints_read_the_clock_once_per_request[list]; e2e `listing` |
| AC-6 | met | test_overdue_list_is_exactly_open_past_due_todos_in_id_order; e2e `overdue-list` |
| AC-7 | met | test_overdue_list_is_empty_list_when_nothing_is_overdue; local-all `empty-overdue --strict` |
| AC-8 | met | test_completing_clears_overdue_and_removes_from_list; e2e `complete` |
| AC-9 | met | test_malformed_due_date_is_rejected_and_not_stored (17 params); test_real_calendar_dates_are_accepted; e2e `malformed` |
| AC-10 | met (patched clock only, by owner decision) | test_overdue_flips_when_the_date_advances_without_a_write; `rollover-local` PASS |
| AC-11 | met | 39 passed (3 in tests/test_todos.py, unedited per `git diff --exit-code main`) |
| AC-12 | met | plan commands 4, 5, 6 and 9 exit 0; deterministic diff_scope and protected_tests pass |
| AC-13 | met | plan command 7 exits 0. README prose states the rule correctly (open, strictly before the UTC date) |

The reviewer marked AC-1..AC-10 `not_assessed` because it could not reach the preview. test_results.json, which is authoritative, shows all 9 preview entries exit 0. That "preview" was an orchestrator-local instance (mode local).

Reproductions (scripts are in judge_probes/):
- F-5 (open nit): the overdue-first to_dict mutant gives 39 passed, local-all 10/0 and rollover-local pass, but race.py shows 110/20000 torn. hook_test.py passes at head and fails on the mutant.
- F-1 withdrawal: head gives 0/20000 torn. The round-1 to_dict gives 70/20000 torn and fails the regression test.
- F-2 withdrawal: under TZ=UTC, the date.today(), datetime.now().date() and datetime.now().astimezone().date() mutants each fail 1 pytest test and 3 local-all checks.
- Old F-3: d33b244 is not an ancestor of HEAD. 5fd5533, 7a5bfd4 and e563e95 all give 422 for the datetime and integer values, with 0 stored.
