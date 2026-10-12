"""Acceptance criteria describe the product, not the proof (post-mortem 2026-10-11 §2)."""
from factory import checks


def _spec(*thens):
    return {"acceptance_criteria": [{"ac_id": f"AC-{i}", "given": "a signed-in member", "when": "they open the picker", "then": t}
                                    for i, t in enumerate(thens, 1)]}


def test_a_criterion_about_the_evidence_protocol_is_refused():
    # evergreen_chat#120's AC-13, nearly verbatim
    r = checks.criteria_describe_product(_spec(
        "the picker lists Claude Haiku 5.5 under Anthropic",
        "with FACTORY_EVIDENCE_DIR set, result.json is valid per the interface with at least five screenshots and one webm demo"))
    assert not r.passed and "AC-2 (FACTORY_EVIDENCE_DIR)" in r.detail and "AC-1" not in r.detail
    assert not checks.criteria_describe_product(_spec("result.json reports viewport_observed 390x844")).passed


def test_product_behaviour_passes_even_when_it_mentions_files_or_screens():
    assert checks.criteria_describe_product(_spec(
        "the export button writes result.json with one row per conversation",
        "the screenshot tool saves a PNG of the visible chat")).passed
    # the app's own names and words are product, not the factory's protocol
    assert checks.criteria_describe_product(_spec(
        "calling factory_reset() empties the model cache",
        "a request with FACTORY_ID set is tagged with that id",
        "the settings page shows the evidence directory the user chose")).passed


def test_specify_refuses_it_before_the_judge(tmp_path, root, examples):
    """The pre-check runs beside schema and traceability and lands in the verdict as a blocking issue."""
    import json

    from support.sessions import AutoQueue, run_after_gate_1, stage_result

    from factory.gates import Decision
    from factory.stages.specify import run_specify
    decomposition = examples["decomposition"]
    wid = decomposition["work_items"][0]["work_item_id"]
    spec = dict(examples["spec_smart_retries"], spec_id=f"spec_{wid.removeprefix('wi-')}", work_item_id=wid)
    spec["acceptance_criteria"] = [dict(spec["acceptance_criteria"][0], then="FACTORY_VISUAL_REQUEST is answered with result.json")] + \
        spec["acceptance_criteria"][1:]

    def run(**kw):
        if kw["agent_key"] == "spec_writer":
            return stage_result({"spec.json": json.dumps(spec).encode(), "spec.md": b"# spec"})
        verdict = {"round": 1, "overall": {"result": "pass", "blocking_issue_count": 0, "summary_for_human": "ok"},
                   "per_spec": [{"spec_id": spec["spec_id"], "criteria": [], "issues": []}],
                   "coherence": {"interface_mismatches": [], "overlaps": [], "gaps": [], "ordering_problems": [], "contradictions": []},
                   "goal_coverage": []}
        return stage_result({"verdict.json": json.dumps(verdict).encode()})
    ctx = run_after_gate_1(tmp_path, root, examples, type("D", (), {"run": staticmethod(run)})(), AutoQueue(Decision.APPROVE))
    _, verdict = run_specify(ctx, examples["outcome"], dict(decomposition, work_items=[decomposition["work_items"][0]]))
    assert verdict["overall"]["result"] == "revise"           # the judge passed it; the pre-check did not
    issues = [i for ps in verdict["per_spec"] for i in ps["issues"]]
    assert any(i["issue_id"] == "DET-criteria_describe_product" and i["severity"] == "blocking" for i in issues)
