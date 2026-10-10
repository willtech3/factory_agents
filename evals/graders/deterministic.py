"""Deterministic graders for eval runs. Thin wrappers over the orchestrator's checks so the
eval harness and production use the exact same code paths (an eval that passes a check the
pipeline does not run is not measuring the pipeline).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "orchestrator"))

from factory import checks  # noqa: E402

SCHEMAS = ROOT / "schemas"


def grade_decomposition(outcome: dict[str, Any], decomposition: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    results = [
        checks.schema_valid(decomposition, SCHEMAS / "decomposition.schema.json", SCHEMAS),
        checks.dag_acyclic(decomposition["work_items"]),
        checks.metric_coverage(outcome, decomposition),
    ]
    questions = [q["question"].lower() for q in decomposition.get("open_questions", [])] + \
                [c["question"].lower() for c in outcome.get("clarifications", [])]
    expected = item.get("expected_questions", [])
    surfaced = [e for e in expected if any(k in q for q in questions for k in e.lower().split()[:2])]
    forbidden_hits = [f for f in item.get("forbidden", [])
                      if any(f.lower() in (w["title"] + " " + w["intent"]).lower() for w in decomposition["work_items"])]
    sizes = [w["size"] for w in decomposition["work_items"]]
    return {
        "checks": [r.to_dict() for r in results],
        "all_checks_passed": all(r.passed for r in results),
        "expected_questions_surfaced": len(surfaced), "expected_questions_total": len(expected),
        "forbidden_scope_hits": forbidden_hits,
        "work_item_count": len(sizes), "large_items": sizes.count("L"),
        "summary_lines": decomposition["summary"].count("\n") + 1,
    }


def grade_spec(spec: dict[str, Any], decomposition: dict[str, Any], repo_files: set[str] | None = None) -> dict[str, Any]:
    results = [
        checks.schema_valid(spec, SCHEMAS / "spec.schema.json", SCHEMAS),
        checks.spec_traceability(spec, decomposition),
    ]
    acs = spec["acceptance_criteria"]
    automated = [a for a in acs if a["verification"] == "automated_test"]
    planned = {ac for t in spec["test_plan"]["new_tests"] for ac in t["covers_ac"]}
    unplanned = [a["ac_id"] for a in automated if a["ac_id"] not in planned]
    root_glob = any(p.strip() in ("**", "**/*", "*") for p in spec["allowed_paths"])
    missing_paths = []
    if repo_files is not None:
        import re
        for m in re.findall(r"([\w./-]+\.(?:rb|py|ts|js|go|java|rs|md|yml|yaml)):\d+", spec.get("context", "")):
            if m not in repo_files:
                missing_paths.append(m)
    return {
        "checks": [r.to_dict() for r in results],
        "all_checks_passed": all(r.passed for r in results) and not unplanned and not root_glob and not missing_paths,
        "ac_count": len(acs), "automated_share": len(automated) / max(1, len(acs)),
        "unplanned_automated_acs": unplanned, "root_glob_in_allowed_paths": root_glob,
        "cited_paths_missing": missing_paths,
    }


def grade_judge_verdict(verdict: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """Did the judge report an issue at the labelled location with at least the expected severity?"""
    sev_rank = {"nit": 0, "minor": 1, "major": 2, "blocking": 3}
    issues = [i for ps in verdict["per_spec"] for i in ps["issues"]]
    for bucket in verdict["coherence"].values():
        issues.extend(bucket)
    if item["defect_code"] == "clean":
        serious = [i for i in issues if sev_rank[i["severity"]] >= 2]
        return {"clean": True, "false_positive": bool(serious), "serious_issue_count": len(serious),
                "result": verdict["overall"]["result"]}
    loc_key = item["location"].split("/")[0].split(":")[0].strip()
    hits = [i for i in issues if loc_key.lower() in (i["location"] + " " + " ".join(i.get("related_spec_ids", []))).lower()]
    good = [i for i in hits if sev_rank[i["severity"]] >= sev_rank[item["expected_severity"]]]
    return {"clean": False, "detected": bool(hits), "detected_at_severity": bool(good),
            "reported_severities": [i["severity"] for i in hits], "result": verdict["overall"]["result"],
            "blocked": verdict["overall"]["result"] != "pass"}


def grade_review(findings: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    sev_rank = {"nit": 0, "minor": 1, "major": 2, "blocking": 3}
    fs = findings["findings"]
    malformed = [f["finding_id"] for f in fs if not f.get("refutation_condition") or not f["evidence"].get("locations")]
    if item["defect_code"] == "clean":
        serious = [f for f in fs if sev_rank[f["severity"]] >= 2]
        return {"clean": True, "flagged_serious": bool(serious), "serious_count": len(serious),
                "malformed_findings": malformed, "checks_performed": len(findings["checks_performed"])}
    file_key = item["location"].split(":")[0]
    hits = [f for f in fs if any(l.startswith(file_key) for l in f["evidence"]["locations"])]
    cat_ok = [f for f in hits if f["category"] == item["expected_category"]]
    sev_ok = [f for f in hits if sev_rank[f["severity"]] >= sev_rank[item["expected_severity"]]]
    over = item["expected_severity"] == "minor" and any(sev_rank[f["severity"]] >= 2 for f in hits)
    return {"clean": False, "detected": bool(hits), "category_match": bool(cat_ok), "severity_ok": bool(sev_ok),
            "over_rejected_minor": over, "has_reproduction": any(f["evidence"].get("reproduction") for f in hits),
            "malformed_findings": malformed, "checks_performed": len(findings["checks_performed"])}


def paired_discrimination(results_by_pair: dict[str, dict[str, dict[str, Any]]], flag_key: str) -> dict[str, float]:
    """For each pair {clean: result, mutant: result}, flag rate on mutant minus flag rate on clean.

    `flag_key` is the boolean field to read ("detected" on mutants, "flagged_serious"/"false_positive" on clean).
    Returns per-defect-code averages. A reviewer that flags everything scores ~0 here.
    """
    by_code: dict[str, list[float]] = {}
    for pair_id, pair in results_by_pair.items():
        clean, mutant, code = pair.get("clean"), pair.get("mutant"), pair.get("code", "?")
        if clean is None or mutant is None:
            continue
        clean_flag = float(bool(clean.get("flagged_serious") or clean.get("false_positive")))
        mutant_flag = float(bool(mutant.get(flag_key)))
        by_code.setdefault(code, []).append(mutant_flag - clean_flag)
    return {code: sum(v) / len(v) for code, v in by_code.items()}
