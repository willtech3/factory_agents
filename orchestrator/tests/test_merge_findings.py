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
