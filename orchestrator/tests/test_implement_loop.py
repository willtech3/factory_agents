"""The implement loop against a real local git remote with fake sessions. No network.

Deterministic-only findings (the orchestrator's own checks, no blocking or major reviewer finding) get an implementer
response round and a re-check that goes straight to the merge judge; a standing reviewer finding still gets a fresh
review round. Every kickoff names the platform-contract skill upload. In local preview mode the preview record says
`unreachable_from_sandbox` with no URL while the orchestrator's own instance runs the preview entries."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import make_config
from support.sessions import stage_result

from factory.gates import InMemoryQueue, LogNotifier
from factory.stages import RunContext
from factory.stages.common import SKILL_UPLOAD_PATH
from factory.stages.implement import SETUP_CHECK, LocalVerifier, preview_info, run_implement_one
from factory.state import WORK_ITEM_TRANSITIONS, Policy, WorkItemStage
from factory.store import RunStore
from support.repos import git, seeded_remote  # noqa: F401
from support.repos import seeded_remote as _remote  # noqa: F401  (the old name other branches use)

HTTP_MANIFEST = {"service": "stdlib-http", "runtime": {"kind": "python", "entrypoint": "python -m http.server 8000 --bind 127.0.0.1", "port": 8000},
                 "health": {"path": "/"}, "env": []}
REPORT = {"criteria": [{"ac_id": "AC-1", "tests": ["tests/test_a.py::test_a"]}], "head_commit": "x"}
BLOCKING = {"finding_id": "F-1", "severity": "blocking", "category": "logic", "claim": "wrong", "status": "open",
            "evidence": {"locations": ["app/main.py:1"]}, "violates": "AC-1", "refutation_condition": "show it right"}


def _spec(local: bool) -> dict:
    ac = [{"ac_id": "AC-1", "user_visible": False, "verification": "automated_test", "given": "g", "when": "w", "then": "t"}]
    e2e = []
    if local:
        ac.append({"ac_id": "AC-2", "user_visible": True, "verification": "manual", "given": "g", "when": "w", "then": "t"})
        e2e.append({"command": "curl -fsS $BASE_URL/", "target": "preview", "covers_ac": ["AC-2"]})
    return {"spec_id": "spec_det", "work_item_id": "wi-det", "title": "det", "allowed_paths": ["app/**", "service_manifest.json"],
            "acceptance_criteria": ac, "_markdown": "# spec",
            # the venv the verifier built must be the interpreter the test plan runs under
            "test_plan": {"commands": ["python -c \"import sys; assert sys.prefix != sys.base_prefix\""], "e2e": e2e}}


def _push(origin: Path, tmp: Path, branch: str, base: str, files: dict[str, str], n: int) -> None:
    """What a fake implementer session does: commit `files` on `branch` (cut from `base` the first time) and push."""
    w = tmp / f"work{n}"
    subprocess.run(["git", "clone", "-q", str(origin), str(w)], check=True)
    if subprocess.run(["git", "rev-parse", "-q", "--verify", f"origin/{branch}"], cwd=w, capture_output=True).returncode == 0:
        git(w, "checkout", "-q", "-b", branch, f"origin/{branch}")
    else:
        git(w, "checkout", "-q", "-b", branch, f"origin/{base}")
    for rel, content in files.items():
        (w / rel).parent.mkdir(parents=True, exist_ok=True)
        (w / rel).write_text(content)
    git(w, "add", "-A")
    # --allow-empty: a round the loop should not have run (the same files again) must fail the test's assertions on
    # the session sequence, not crash git with "nothing to commit"
    git(w, "commit", "-q", "--allow-empty", "-m", f"round {n}")
    git(w, "push", "-q", "origin", branch)


def _result(outputs: dict[str, bytes]):
    return stage_result(outputs, sid="sesn_x", agent_id="agent_x", cost=0.1)


class Driver:
    """Implementer rounds push real commits (round 1 the app, round 2 the manifest); reviewers return the findings
    scripted per round; the judge says ready."""

    def __init__(self, origin: Path, tmp: Path, findings_by_round: dict[int, list[dict]]):
        self.origin, self.tmp, self.findings_by_round, self.calls = origin, tmp, findings_by_round, []

    def run(self, **kw):
        self.calls.append(kw)
        key, title = kw["agent_key"], kw["title"]
        round_no = int(m.group(1)) if (m := re.search(r" r(\d+)", title)) else 0
        if key == "implementer":
            files = {"app/main.py": "X = 1\n"} if round_no == 1 else {"service_manifest.json": json.dumps(HTTP_MANIFEST)}
            _push(self.origin, self.tmp, "factory/run_loop0000001/spec_det", "main", files, round_no)
            outputs = {"implementation_report.json": json.dumps(REPORT).encode()}
            if "findings.json" in kw["uploads"]:
                answered = json.loads(kw["uploads"]["findings.json"])["findings"]
                outputs["review_response.json"] = json.dumps({"responses": [{"finding_id": f["finding_id"], "disposition": "fixed",
                                                                            "explanation": "done"} for f in answered]}).encode()
            return _result(outputs)
        if key == "reviewer":
            fs = json.loads(json.dumps(self.findings_by_round.get(round_no, [])))
            return _result({"findings.json": json.dumps({"findings": fs, "checks_performed": [], "spec_conformance": []}).encode()})
        if key == "merge_judge":
            return _result({"merge_readiness.json": json.dumps({"result": "ready", "summary_for_human": "fine",
                                                                 "criteria": [{"ac_id": "AC-1", "status": "met", "evidence": "tests/test_a.py::test_a"}]}).encode()})
        raise AssertionError(f"unexpected agent {key}")


def _ctx(tmp_path, root, driver, policy: Policy | None = None, **cfg):
    """`policy` defaults to two first-round passes whatever the diff size (these fixtures' diffs are a few lines, which
    the default policy reviews with one pass; `test_review_is_sized_to_the_change` covers that)."""
    runs = tmp_path / "runs"
    ctx = RunContext(cfg=make_config(root, runs, **cfg), store=RunStore(runs), driver=driver, queue=InMemoryQueue(),
                     notifier=LogNotifier(), forge=None, policy=policy or Policy(review_single_pass_lines=0), run_id="run_loop0000001")
    ctx.event("run.created", {"policy": {"preview_mode": "local"}, "budget_usd": 50, "outcome_id": "o", "title": "t"})
    ctx.event("artifact.produced", {"artifact_type": "decomposition", "revision": 1, "work_items": [{"work_item_id": "wi-det", "depends_on": []}]}, stage="decompose")
    for st in (WorkItemStage.SPEC_DRAFTING, WorkItemStage.SPEC_JUDGING, WorkItemStage.SPEC_APPROVED):
        ctx.event("work_item.stage", {"stage": st.value}, work_item_id="wi-det")
    return ctx


def _titles(driver) -> list[str]:
    """Session titles in call order, with the reviewer passes of one round in pass order: they run in parallel
    threads (with the security pass beside them), so the order the fake driver saw them in is not deterministic."""
    raw = [kw["title"].split(" ", 1)[1] for kw in driver.calls]
    out, run = [], []
    for t in raw + [None]:
        if t is not None and t.startswith("review ") and " p" in t:
            run.append(t)
            continue
        out.extend(sorted(run)); run = []
        if t is not None:
            out.append(t)
    return out


def _item_path(ctx) -> list[WorkItemStage]:
    """The work item's stage transitions as the event log records them, each checked against the state machine, and the
    whole log replayed: the parallel reviewer passes append from worker threads, so this also guards the contiguous
    event seqs store.py assigns (post-mortem 2026-10-10, issue 2)."""
    stages = [WorkItemStage(e.payload["stage"]) for e in ctx.store.events(ctx.run_id) if e.kind == "work_item.stage"]
    for a, b in zip(stages, stages[1:]):
        assert b in WORK_ITEM_TRANSITIONS[a], f"illegal work item transition {a.value} -> {b.value}"
    ctx.store.state(ctx.run_id)                     # replay refuses a log whose seqs collide or skip
    return stages


@pytest.mark.parametrize("preview_mode", ["platform", pytest.param("local", marks=pytest.mark.skipif(not shutil.which("curl"), reason="the spec's e2e entry uses curl"))])
def test_deterministic_only_findings_get_a_response_round_and_a_recheck_not_a_review(tmp_path, root, preview_mode):
    origin = seeded_remote(tmp_path)
    driver = Driver(origin, tmp_path, {})                      # reviewers find nothing; round 1 fails only manifest_valid (+ e2e_preview locally)
    ctx = _ctx(tmp_path, root, driver, preview_mode=preview_mode)
    verifier = LocalVerifier(str(origin), tmp_path / "runs" / ctx.run_id / "verify")
    verdict = run_implement_one(ctx, _spec(preview_mode == "local"), str(origin), "main", verifier, {})
    assert verdict["result"] == "ready" and verdict["_branch"] == "factory/run_loop0000001/spec_det"
    assert _titles(driver) == ["implement spec_det r1", "review spec_det r1 p1", "review spec_det r1 p2", "implement spec_det r2", "merge-judge spec_det"]

    # round 1: the per-run environment was built and only the orchestrator's own checks failed
    det1 = {d["check"]: d for d in json.loads(ctx.store.get_artifact(ctx.run_id, "implement", "spec_det-r1-deterministic_checks.json"))}
    assert det1[SETUP_CHECK]["passed"] and "nothing installed" in det1[SETUP_CHECK]["detail"]
    assert det1["test_plan_commands"]["passed"] and not det1["manifest_valid"]["passed"]
    failed1 = sorted(k for k, d in det1.items() if not d["passed"])
    assert failed1 == (["e2e_preview", "manifest_valid"] if preview_mode == "local" else ["manifest_valid"])
    merged1 = json.loads(ctx.store.get_artifact(ctx.run_id, "review", "spec_det-r1-findings.merged.json"))
    assert sorted(f["finding_id"] for f in merged1["findings"]) == [f"DET-{k}" for k in failed1]
    # the response round answered exactly those and was told it is a deterministic-only round
    r2 = driver.calls[3]
    assert "DET-*" in r2["description"] and "straight to the merge judge" in r2["description"]
    assert json.loads(r2["uploads"]["findings.json"])["findings"] == merged1["findings"]
    det2 = {d["check"]: d for d in json.loads(ctx.store.get_artifact(ctx.run_id, "implement", "spec_det-r2-deterministic_checks.json"))}
    assert all(d["passed"] for d in det2.values()), det2
    assert verdict["deterministic_checks"] == [d for d in det2.values()]
    # the judge got the implementer's answers and the test results with the environment record
    mj = driver.calls[4]
    answered = {r["finding_id"] for r in json.loads(mj["uploads"]["review_response.json"])["responses"]}
    assert answered == {f"DET-{k}" for k in failed1}
    tr = json.loads(mj["uploads"]["test_results.json"])
    assert tr["environment"]["ready"] is True and tr["environment"]["mode"] == preview_mode and tr["environment"]["env_dir"].endswith("verify/venv")
    assert tr["environment"]["host"] == ("orchestrator" if preview_mode == "local" else "platform")
    # events and replay: responding (deterministic-only) -> merge judging, with no second review round
    kinds = [(e.kind, e.payload) for e in ctx.store.events(ctx.run_id)]
    assert ("work_item.stage", {"stage": "responding", "deterministic_only": True}) in kinds
    skipped = [p for k, p in kinds if k == "review.findings" and p.get("deterministic_only")]
    assert skipped and skipped[0]["round"] == 2 and "no review round" in skipped[0]["skipped"]
    assert _item_path(ctx)[-4:] == [WorkItemStage.IMPLEMENTING, WorkItemStage.REVIEWING, WorkItemStage.RESPONDING, WorkItemStage.MERGE_JUDGING]
    assert not any(t.startswith("review spec_det r2") for t in _titles(driver))

    # every session got the platform-contract skill, by its uploaded path
    for c in driver.calls:
        assert SKILL_UPLOAD_PATH in c["uploads"] and c["uploads"][SKILL_UPLOAD_PATH].startswith(b"---")
        assert "/mnt/session/uploads/skills/platform-contract/SKILL.md" in c["description"] and "do not search" in c["description"]

    if preview_mode == "local":
        # preview.json told the agents the truth; the orchestrator's own instance ran the preview entry in round 2
        for c in driver.calls[:4]:
            pv = json.loads(c["uploads"]["preview.json"])
            assert pv["status"] == "unreachable_from_sandbox" and pv["url"] is None and pv["mode"] == "local"
        assert "local-manifest" in driver.calls[1]["description"] and "not_met" in driver.calls[1]["description"]
        assert "local-manifest" not in driver.calls[0]["description"] and "runtime.entrypoint" in driver.calls[0]["description"]
        tr1 = json.loads(ctx.store.get_artifact(ctx.run_id, "implement", "spec_det-r1-test_results.json"))
        assert tr1["preview"]["orchestrator_instance"]["status"] == "failed" and "manifest" in tr1["preview"]["orchestrator_instance"]["error"]
        assert tr1["e2e"][0]["exit_code"] is None
        inst = tr["preview"]["orchestrator_instance"]
        assert inst["status"] == "ready" and inst["url"].startswith("http://127.0.0.1:") and tr["preview"]["url"] is None
        assert tr["e2e"][0]["exit_code"] == 0 and tr["e2e"][0]["covers_ac"] == ["AC-2"] and tr["e2e"][0]["base_url"] == inst["url"]
        assert "local" in mj["description"] and "orchestrator_instance" in mj["description"]
    else:
        assert "orchestrator_instance" not in mj["description"]


def test_a_standing_reviewer_finding_still_gets_a_fresh_review_round(tmp_path, root):
    origin = seeded_remote(tmp_path)
    driver = Driver(origin, tmp_path, {1: [BLOCKING]})         # round 1: one blocking finding beside the DET entry; round 2: clean
    ctx = _ctx(tmp_path, root, driver)
    verifier = LocalVerifier(str(origin), tmp_path / "runs" / ctx.run_id / "verify")
    verdict = run_implement_one(ctx, _spec(False), str(origin), "main", verifier, {})
    assert verdict["result"] == "ready"
    # round 2 is one pass over the delta, not two passes over the whole diff again
    assert _titles(driver) == ["implement spec_det r1", "review spec_det r1 p1", "review spec_det r1 p2", "implement spec_det r2",
                               "review spec_det r2 p1", "merge-judge spec_det"]
    assert "DET-*" not in driver.calls[3]["description"]
    kinds = [(e.kind, e.payload) for e in ctx.store.events(ctx.run_id)]
    assert ("work_item.stage", {"stage": "responding", "deterministic_only": False}) in kinds
    assert not any(k == "review.findings" and p.get("deterministic_only") for k, p in kinds)
    assert _item_path(ctx)[-5:] == [WorkItemStage.IMPLEMENTING, WorkItemStage.REVIEWING, WorkItemStage.RESPONDING,
                                    WorkItemStage.REVIEWING, WorkItemStage.MERGE_JUDGING]


def test_preview_info_in_local_mode_is_unreachable_with_no_url(tmp_path, root):
    ctx = RunContext(cfg=make_config(root, tmp_path, preview_mode="local"), store=RunStore(tmp_path), driver=None, queue=InMemoryQueue(),
                     notifier=LogNotifier(), forge=None, policy=Policy(), run_id="run_prev00000001")
    info = preview_info(ctx, "factory/r/s", "abc")
    assert info["status"] == "unreachable_from_sandbox" and info["url"] is None and info["mode"] == "local" and info["pipeline"] is None
    assert "runtime.entrypoint" in info["note"]
    ctx.cfg = make_config(root, tmp_path, preview_mode="platform")
    assert preview_info(ctx, "factory/r/s", "abc")["status"] == "unavailable"


def test_resume_on_changed_head_rechecks_preserves_proof_and_does_not_reuse_old_reviews(tmp_path, root):
    """An older controller left round 1's report for a head the branch no longer has: the report is adopted
    (`adopt_rounds`), its implementer is not asked again, and the head check fails on the new head."""
    origin = seeded_remote(tmp_path)
    branch = "factory/run_loop0000001/spec_det"
    _push(origin, tmp_path, branch, "main", {"app/main.py": "X = 2\n", "service_manifest.json": json.dumps(HTTP_MANIFEST)}, 0)
    driver = Driver(origin, tmp_path, {})
    ctx = _ctx(tmp_path, root, driver)
    for stage in (WorkItemStage.IMPLEMENTING, WorkItemStage.REVIEWING):
        ctx.event("work_item.stage", {"stage": stage.value}, work_item_id="wi-det")
    old_report = {**REPORT, "head_sha": "old-candidate"}
    ctx.store.put_artifact(ctx.run_id, "implement", "spec_det-r1-implementation_report.json", json.dumps(old_report).encode())
    old_proof = ctx.store.put_artifact(ctx.run_id, "implement", "spec_det-r1-test_results.json", b'{"old": true}')
    for i in (1, 2):
        ctx.store.put_artifact(ctx.run_id, "review", f"spec_det-r1-baseold-base-headold-candidate-p{i}-findings.json",
                               json.dumps({"diff_ref": {"base_sha": "old-base", "head_sha": "old-candidate"}, "findings": []}).encode())
    verifier = LocalVerifier(str(origin), tmp_path / "runs" / ctx.run_id / "verify")
    verdict = run_implement_one(ctx, _spec(False), str(origin), "main", verifier, {})
    assert verdict["result"] == "ready"
    assert _titles(driver) == ["review spec_det r1 p1", "review spec_det r1 p2", "implement spec_det r2", "merge-judge spec_det"]
    failed = ctx.store.get_json(ctx.run_id, "implement", "spec_det-r1-deterministic_checks.json")
    assert any(c["check"] == "implementation_head" and not c["passed"] for c in failed)
    assert ctx.store.get_artifact(ctx.run_id, "history", f"implement/spec_det-r1-test_results.json/{old_proof}") == b'{"old": true}'
    old_review = "spec_det-r1-baseold-base-headold-candidate-p1-findings.json"
    assert ctx.store.get_json(ctx.run_id, "review", old_review)["diff_ref"]["head_sha"] == "old-candidate"
    assert ctx.store.get_json(ctx.run_id, "implement", "spec_det-r1-implementation_report.json") == old_report


def test_review_is_sized_to_the_change(tmp_path, root):
    """A first round over a small change is one general pass (the default policy); its plan is on the events."""
    origin = _remote(tmp_path)
    driver = Driver(origin, tmp_path, {})
    ctx = _ctx(tmp_path, root, driver, policy=Policy())
    verifier = LocalVerifier(str(origin), tmp_path / "runs" / ctx.run_id / "verify")
    assert run_implement_one(ctx, _spec(False), str(origin), "main", verifier, {})["result"] == "ready"
    assert _titles(driver) == ["implement spec_det r1", "review spec_det r1 p1", "implement spec_det r2", "merge-judge spec_det"]
    review = next(e.payload for e in ctx.store.events(ctx.run_id) if e.kind == "review.findings" and e.payload["round"] == 1)
    assert review["passes"] == 1 and review["scope"] == "full" and review["security_pass"] is False
    plan = next(e.payload["review_plan"] for e in ctx.store.events(ctx.run_id)
                if e.kind == "work_item.stage" and e.payload.get("stage") == "reviewing")
    assert plan["hand_written_lines"] == 1 and plan["passes"] == 1


def test_a_follow_up_round_reads_only_the_delta(tmp_path, root):
    origin = _remote(tmp_path)
    driver = Driver(origin, tmp_path, {1: [BLOCKING]})
    ctx = _ctx(tmp_path, root, driver)
    verifier = LocalVerifier(str(origin), tmp_path / "runs" / ctx.run_id / "verify")
    assert run_implement_one(ctx, _spec(False), str(origin), "main", verifier, {})["result"] == "ready"
    r1_head = json.loads(ctx.store.get_artifact(ctx.run_id, "review", "spec_det-r1-findings.merged.json"))["diff_ref"]["head_sha"]
    r2 = next(c for c in driver.calls if c["title"].endswith("review spec_det r2 p1"))
    delta = r2["uploads"]["delta.patch"].decode()
    assert "service_manifest.json" in delta and "app/main.py" not in delta          # round 2 pushed only the manifest
    assert "app/main.py" in r2["uploads"]["diff.patch"].decode()                     # the whole change stays as context
    assert f"{r1_head}.." in r2["description"] and "previous_findings.json" in r2["description"]
    review2 = next(e.payload for e in ctx.store.events(ctx.run_id) if e.kind == "review.findings" and e.payload["round"] == 2)
    assert review2["scope"] == "delta" and review2["passes"] == 1


def test_the_implementers_grader_gets_only_the_items_it_scores(tmp_path, root):
    origin = _remote(tmp_path)
    driver = Driver(origin, tmp_path, {})
    ctx = _ctx(tmp_path, root, driver)
    verifier = LocalVerifier(str(origin), tmp_path / "runs" / ctx.run_id / "verify")
    run_implement_one(ctx, _spec(False), str(origin), "main", verifier, {})
    rubric = next(c for c in driver.calls if c["agent_key"] == "implementer")["rubric_md"]
    assert "## I1." in rubric and "## I7." in rubric
    assert "## I2." not in rubric and "## I9." not in rubric and "grader copy ends here" not in rubric
    assert ctx.grader_rubric("spec") == ctx.rubric("spec")              # no marker: the whole rubric


def test_review_plan_brings_the_security_pass_back_only_when_it_has_work():
    from factory.stages.implement import review_plan
    policy = Policy()
    previous = {"diff_ref": {"head_sha": "abc"}, "findings": [
        {"finding_id": "F-1", "pass": "1", "status": "open"}, {"finding_id": "F-2", "pass": "security", "status": "withdrawn"}]}
    response = {"responses": []}
    quiet = review_plan(policy, None, previous, response, {"app/x.py": ["x = 1"]}, security_available=True)
    assert quiet["scope"] == "delta" and quiet["passes"] == 1 and quiet["security"] is False
    standing = {**previous, "findings": [*previous["findings"], {"finding_id": "F-3", "pass": "security", "status": "upheld"}]}
    assert review_plan(policy, None, standing, response, {}, security_available=True)["security"] is True
    leaked = review_plan(policy, None, previous, response, {"app/x.py": ['token = "ghp_' + "a" * 36 + '"']}, security_available=True)
    assert leaked["security"] is True and "secret scan" in leaked["security_why"]
    assert review_plan(policy, None, standing, response, {}, security_available=False)["security"] is False
    # first rounds: one pass up to the threshold, the configured passes above it or when the size is unknown
    assert review_plan(policy, {"hand_written": 150}, None, None, None, True)["passes"] == 1
    assert review_plan(policy, {"hand_written": 151}, None, None, None, True)["passes"] == 2
    assert review_plan(policy, None, None, None, None, True) == {
        "scope": "full", "previous_head": None, "hand_written_lines": None, "passes": 2, "security": True, "security_why": "first round"}
