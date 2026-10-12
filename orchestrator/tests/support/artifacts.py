"""Schema-valid agent outputs for the test fakes. The orchestrator validates every agent output on receipt (rule
`artifact-schema`, ADR-0015), so a fake writes what a real agent must; the controller stamps `produced_by` itself."""


def finding(n: int = 1, *, severity: str = "minor", claim: str = "a finding", category: str = "correctness",
            location: str = "app/main.py:1", status: str = "open", **extra) -> dict:
    return {"finding_id": f"F-{n}", "severity": severity, "category": category, "claim": claim,
            "evidence": {"locations": [location]}, "violates": ["AC-1"], "refutation_condition": "a test shows otherwise",
            "status": status, **extra}


def findings(spec_id: str, items=(), *, round: int = 1, base: str = "0" * 40, head: str = "0" * 40, branch: str = "b",
             conformance=()) -> dict:
    return {"review_id": "rev_fake0001", "spec_id": spec_id, "round": round,
            "diff_ref": {"base_sha": base, "head_sha": head, "branch": branch},
            "checks_performed": [{"check": "read the diff against the spec", "method": "inspection", "result": "done"}],
            "findings": list(items), "spec_conformance": list(conformance)}


def report(spec_id: str, criteria: list[dict], **extra) -> dict:
    """implementation_report.json: `criteria` is [{ac_id, tests}]."""
    return {"spec_id": spec_id, "criteria": list(criteria), "commands_run": [], "files_changed": [], **extra}


def response(spec_id: str, answered: list[dict], *, disposition: str = "fixed", review_id: str = "rev_fake0001",
             round: int = 1) -> dict:
    """review_response.json answering every finding in `answered` the same way."""
    return {"response_id": "rsp_fake0001", "review_id": review_id, "spec_id": spec_id, "round": round,
            "responses": [{"finding_id": f["finding_id"], "disposition": disposition, "explanation": "done"} for f in answered],
            "new_head_sha": "0" * 40, "tests_after": {"commands": [], "all_passed": True}}


def merge_readiness(spec: dict, result: str = "ready", *, criteria: list[dict] | None = None, summary: str = "fine",
                    **extra) -> dict:
    """merge_readiness.json; by default every acceptance criterion of `spec` is met (rule `merge-ready`)."""
    if criteria is None:
        criteria = [{"ac_id": ac["ac_id"], "status": "met", "evidence": "the orchestrator's run"} for ac in spec.get("acceptance_criteria", [])]
    return {"verdict_id": "mrv_fake0001", "spec_id": spec["spec_id"], "round": 1, "result": result, "unresolved_findings": [],
            "criteria": criteria, "deterministic_checks": [], "residual_risks": [],
            "pr_summary": {"what_changed": "the change", "how_tested": "the test plan", "decisions": [], "read_first": []},
            "summary_for_human": summary, **extra}
