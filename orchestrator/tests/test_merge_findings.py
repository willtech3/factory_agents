import copy

from factory.stages.implement import merge_findings


def test_merge_findings_dedupes_and_keeps_strictest(examples):
    f1 = examples["findings_round1"]
    f2 = copy.deepcopy(f1)
    # Second pass found the same concurrency bug but rated it major, plus a new finding.
    f2["findings"][0]["severity"] = "major"
    f2["findings"].append({
        "finding_id": "F-9", "severity": "major", "category": "error_handling",
        "claim": "x", "evidence": {"locations": ["app/services/billing/stripe_webhook_handler.rb:52"]},
        "violates": ["AC-8"], "refutation_condition": "y", "status": "open",
    })
    f2["spec_conformance"][0]["status"] = "not_met"  # stricter view on AC-1 from pass 2
    merged = merge_findings([f1, f2], "spec_smart-retries", 1, "base", "head", "br")
    ids = [f["finding_id"] for f in merged["findings"]]
    assert ids == [f"F-{i}" for i in range(1, len(ids) + 1)]       # renumbered
    assert len(merged["findings"]) == 5                              # 4 unique from pass 1 + 1 new
    top = merged["findings"][0]
    assert top["category"] == "concurrency" and top["severity"] == "blocking"   # strictest severity kept
    ac1 = next(r for r in merged["spec_conformance"] if r["ac_id"] == "AC-1")
    assert ac1["status"] == "not_met"
    assert len(merged["checks_performed"]) == 2 * len(f1["checks_performed"])


def _f(fid, sev, cat, loc, status="open", previous=None, **extra):
    f = {"finding_id": fid, "severity": sev, "category": cat, "claim": fid, "evidence": {"locations": [loc]},
         "violates": ["AC-1"], "refutation_condition": "r", "status": status, **extra}
    if previous:
        f["previous_finding_id"] = previous
    return f


def test_round_two_keeps_previous_ids_carries_dispositions_and_numbers_only_new_findings():
    """Post-mortem issue 14: in round 2 the merge re-keyed everything by location and renumbered, so one withdrawn
    finding appeared twice, ids stopped matching review_response.json, the DET entry vanished and two passes'
    checks_performed were concatenated unattributed."""
    from factory.stages.implement import apply_deterministic, deterministic_findings, tag_pass
    from factory.checks import CheckResult

    previous = {"findings": [_f("F-1", "minor", "concurrency", "app/storage.py:31"),
                             _f("F-2", "minor", "test_quality", "tests/test_overdue.py:188"),
                             _f("F-3", "nit", "maintainability", "app/main.py:9"),
                             {**_f("DET-ac_test_mapping", "blocking", "test_quality", "deterministic_checks.json:1"), "violates": "orchestrator check"}]}
    response = {"responses": [{"finding_id": "F-1", "disposition": "fixed"}, {"finding_id": "F-2", "disposition": "refuted"},
                              {"finding_id": "F-3", "disposition": "fixed"}, {"finding_id": "DET-ac_test_mapping", "disposition": "fixed"}]}
    p1 = tag_pass({"checks_performed": [{"check": "race probe", "method": "m", "result": "118/1007"}], "scope_check": {"files_outside_allowed_paths": ["x.py"]},
                   "findings": [_f("F-1", "minor", "concurrency", "app/storage.py:31", status="withdrawn"),
                                _f("F-2", "minor", "test_quality", "app/storage.py:10", status="withdrawn", previous="F-2"),  # restated at another location
                                _f("F-1", "minor", "correctness", "app/dates.py:40")]}, "1")          # a NEW finding that reused the id F-1
    p2 = tag_pass({"checks_performed": [{"check": "race probe", "method": "m", "result": "88/633"}],
                   "findings": [_f("F-1", "major", "concurrency", "app/storage.py:33", status="upheld"),   # the race still reproduces
                                _f("F-9", "minor", "test_quality", "tests/test_overdue.py:188", status="withdrawn"),   # kept the location, not the id
                                _f("F-2", "major", "correctness", "app/dates.py:40")]}, "2")          # the same new finding, rated higher
    merged = merge_findings([p1, p2], "spec_x", 2, "b", "h", "br", previous=previous, response=response)
    by_id = {f["finding_id"]: f for f in merged["findings"]}
    assert list(by_id) == ["F-1", "F-2", "F-3", "DET-ac_test_mapping", "F-4"]
    assert by_id["F-1"]["status"] == "upheld" and by_id["F-1"]["severity"] == "major" and by_id["F-1"]["pass"] == "2"   # stricter re-evaluation wins
    assert by_id["F-2"]["status"] == "withdrawn" and by_id["F-2"]["previous_finding_id"] == "F-2"                        # one entry, not two
    assert by_id["F-3"]["status"] == "fixed" and by_id["DET-ac_test_mapping"]["status"] == "fixed"                        # nobody re-evaluated: the implementer's disposition
    assert by_id["F-4"]["category"] == "correctness" and by_id["F-4"]["severity"] == "major" and by_id["F-4"]["pass"] == "2"
    assert [row["pass"] for row in merged["checks_performed"]] == ["1", "2"]                                            # attributed, not concatenated
    assert merged["scope_check"] == {"files_outside_allowed_paths": ["x.py"], "unrelated_changes": []}
    # this round's deterministic checks: a check failing again re-opens its DET entry in place; a passing one stays closed
    det_again = deterministic_findings([CheckResult("ac_test_mapping", False, "AC-2: tests not found")])
    folded = apply_deterministic(merged["findings"], det_again)
    assert [f["finding_id"] for f in folded] == ["F-1", "F-2", "F-3", "DET-ac_test_mapping", "F-4"]
    assert next(f for f in folded if f["finding_id"] == "DET-ac_test_mapping")["status"] == "open"
    folded = apply_deterministic([f for f in merged["findings"] if f["finding_id"] != "DET-ac_test_mapping"] + [_f("DET-e2e_preview", "blocking", "test_quality", "d:1")], [])
    assert next(f for f in folded if f["finding_id"] == "DET-e2e_preview")["status"] == "fixed"                         # carried open, check passes now
