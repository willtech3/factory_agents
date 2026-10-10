"""The eval graders agree with the example artifacts and the statistics helpers behave."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evals"))

from graders import deterministic as det  # noqa: E402
from graders import stochastic as sto  # noqa: E402


def test_grade_decomposition_example(examples):
    item = json.loads((ROOT / "evals/datasets/outcomes-golden/items.jsonl").read_text().splitlines()[0])
    g = det.grade_decomposition(examples["outcome"], examples["decomposition"], item)
    assert g["all_checks_passed"]
    assert g["expected_questions_surfaced"] == 2       # grace period + retry engine both surfaced
    assert g["forbidden_scope_hits"] == []
    assert g["large_items"] == 0


def test_grade_spec_example(examples):
    g = det.grade_spec(examples["spec_smart_retries"], examples["decomposition"])
    assert g["all_checks_passed"], g
    assert g["unplanned_automated_acs"] == []
    assert not g["root_glob_in_allowed_paths"]


def test_grade_judge_verdict_clean_and_defect(examples):
    v = examples["verdict_round2"]
    clean = det.grade_judge_verdict(v, {"defect_code": "clean"})
    assert clean["clean"] and not clean["false_positive"]
    # The example verdict reports SR-1 (minor) on spec_smart-retries; a planted blocking defect there should count as missed at severity.
    missed = det.grade_judge_verdict(v, {"defect_code": "AMB", "location": "spec_smart-retries/AC-4", "expected_severity": "blocking"})
    assert missed["detected"] and not missed["detected_at_severity"]


def test_grade_review_example(examples):
    f = examples["findings_round1"]
    race = det.grade_review(f, {"defect_code": "RACE", "location": "app/models/payment_attempt.rb:31-38",
                               "expected_category": "concurrency", "expected_severity": "blocking"})
    assert race["detected"] and race["category_match"] and race["severity_ok"] and race["has_reproduction"]
    assert race["malformed_findings"] == []
    dead = det.grade_review(f, {"defect_code": "DEAD", "location": "db/migrate/20261009_create_payment_attempts.rb:9",
                               "expected_category": "maintainability", "expected_severity": "minor"})
    assert dead["detected"] and not dead["over_rejected_minor"]


def test_paired_discrimination():
    pairs = {
        "p1": {"code": "RACE", "clean": {"flagged_serious": False}, "mutant": {"detected": True}},
        "p2": {"code": "RACE", "clean": {"flagged_serious": True}, "mutant": {"detected": True}},   # flags everything
        "p3": {"code": "DEV", "clean": {"flagged_serious": False}, "mutant": {"detected": False}},
    }
    pd = det.paired_discrimination(pairs, "detected")
    assert pd["RACE"] == 0.5 and pd["DEV"] == 0.0


def test_statistics_helpers():
    assert sto.cohens_kappa([1, 1, 0, 0], [1, 1, 0, 0]) == 1.0
    assert abs(sto.cohens_kappa([1, 0, 1, 0], [1, 1, 0, 0])) < 1e-9
    mean, lo, hi = sto.bootstrap_ci([1, 1, 1, 0, 0])
    assert lo <= mean <= hi and abs(mean - 0.6) < 1e-9
    m, lo, hi = sto.beta_credible_interval(4, 5)
    assert lo < m < hi and 0.5 < m < 0.9
    assert sto.pass_power_k({"a": [True, True, False], "b": [True, True, True]}, 3) < sto.pass_power_k({"a": [True, True, False], "b": [True, True, True]}, 1)
    assert sto.intervals_non_overlapping((0.2, 0.1, 0.3), (0.6, 0.5, 0.7))
    assert not sto.intervals_non_overlapping((0.2, 0.1, 0.55), (0.6, 0.5, 0.7))
    assert sto.normalise("## **Bold** `code`  \n") == "Bold code"
