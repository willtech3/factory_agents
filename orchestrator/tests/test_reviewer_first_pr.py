"""Post-mortem 2026-10-11, replayed: the evergreen_chat#120 case through the real integrate stage and the Console API.

The pull request a reviewer gets is short and written in the app's terms; everything else (criteria, findings,
verification, media, per-session cost) is in the run record on the Console's run page."""
import re

from fastapi.testclient import TestClient
from support import evergreen_case as case

# Checked here independently of pr_description.lint, so a regression in the lint cannot hide one in the output.
BANNED = [r"\bAC-\d", r"\bF-\d", r"\bDET-", r"(?i)merge judge", r"(?i)orchestrator", r"(?i)\brecipe", r"(?i)\bjourney",
          r"(?i)\brounds?\b", r"\bsesn_", r"\$\d", r"(?<![\w/])I(?![\w/'])", r"(?i)\bimplementer\b", r"(?i)\bintegrator\b"]


def _integrate(tmp_path, root, examples, *, evaluation=False, extra_files=None):
    from test_session_cutoff import AutoQueue, _ctx

    from factory.gates import Decision
    from factory.stages.integrate import run_integrate
    from factory.state import WorkItemStage
    _, work, base, head = case.repository(tmp_path, extra_files)
    opened, created = [], []
    ctx = _ctx(tmp_path / "runs", root, examples, case.integrator(opened), AutoQueue(Decision.APPROVE))
    ctx.event("artifact.produced", {"artifact_type": "decomposition", "revision": 1,
                                    "work_items": [{"work_item_id": case.WORK_ITEM, "title": "Frontier models", "depends_on": []}]})
    for run_stage in ("specifying", "gate_2_spec_readiness", "implementing"):
        ctx.event("stage.entered", {"stage": run_stage})
    for stage in [WorkItemStage.SPEC_DRAFTING, WorkItemStage.SPEC_JUDGING, WorkItemStage.SPEC_APPROVED,
                  WorkItemStage.IMPLEMENTING, WorkItemStage.REVIEWING, WorkItemStage.MERGE_JUDGING]:
        ctx.event("work_item.stage", {"stage": stage.value}, work_item_id=case.WORK_ITEM, spec_id=case.SPEC_ID)
    spec = case.spec()
    rows = case.browser_evidence(ctx, tmp_path, spec, head)

    ctx.forge, api = github(created)
    target = "main"
    if evaluation:
        # As in a real evaluation: the copy is cut from the original baseline, pinned on its own branch, while the
        # original change already reached main as a squash, without the files the copy adds (`extra_files`).
        target = "eval-baseline"
        case.git(work, "push", "-q", "origin", f"{base}:refs/heads/{target}")
        case.git(work, "checkout", "-q", "-b", "original", base)
        case.git(work, "checkout", head, "--", ".")
        for path in extra_files or {}:
            case.git(work, "rm", "-q", "-f", path)
        case.git(work, "commit", "-q", "-m", "Squash of the original change")
        case.git(work, "push", "-q", "origin", "HEAD:main")
        case.git(work, "checkout", "-q", case.BRANCH)
        ctx.evaluation_target = (target, base)
        ctx.event("artifact.produced", {"artifact_type": "evaluation_target", "branch": target, "base_sha": base, "normal_target": "main"})
    verifier = type("Verifier", (), {"checkout": staticmethod(lambda b: (case.git(work, "checkout", "-q", b), work)[1])})()
    run_integrate(ctx, spec, case.verdict(base, head), case.findings(), case.response(), case.test_results(rows, head),
                  case.run_summary(), target, verifier=verifier)
    ctx.event("run.completed", {"specs_with_change_requests": [case.SPEC_ID]})
    ctx.api_calls = api.calls
    return ctx, created[0], head


def github(created):
    """The real GitHub client against a scripted API: the orphan evidence branch for the media, then the pull request."""
    from test_forge import Seq, _forge
    repo = "/repos/willtech3/evergreen_chat"

    def pull(body, params):
        created.append({"title": body["title"], "description": body["body"], "draft": body["draft"],
                        "source_branch": body["head"], "target_branch": body["base"]})
        return {"number": 120, "html_url": "https://github.com/willtech3/evergreen_chat/pull/120"}
    routes = {f"GET {repo}/git/ref/heads/factory-evidence": Seq([(404, {}), (200, {"object": {"sha": "orphan1"}})]),
              f"POST {repo}/git/blobs": (201, {"sha": "blob1"}), f"POST {repo}/git/trees": (201, {"sha": "tree1"}),
              f"POST {repo}/git/commits": (201, {"sha": "commit1"}), f"POST {repo}/git/refs": (201, {}),
              f"GET {repo}/git/commits/orphan1": {"tree": {"sha": "tree0"}},
              f"PATCH {repo}/git/refs/heads/factory-evidence": {"object": {"sha": "commit1"}},
              f"POST {repo}/pulls": (201, pull), f"POST {repo}/issues/120/labels": (200, []),
              f"POST {repo}/pulls/120/requested_reviewers": (201, {})}
    forge, api = _forge(routes)
    forge.repo = "willtech3/evergreen_chat"
    return forge, api


def test_the_pull_request_is_short_and_written_for_the_reviewer(tmp_path, root, examples):
    ctx, cr, head = _integrate(tmp_path, root, examples)
    body = cr["description"]
    lines = body.rstrip("\n").splitlines()
    assert len(lines) <= 60, len(lines)
    record_url = f"{ctx.cfg.artifact_base_url}/#/runs/{ctx.run_id}#record-{case.SPEC_ID}"
    prose = re.sub(r"\]\([^)]*\)", "]", body.replace(record_url, ""))
    for pattern in BANNED:
        assert not re.search(pattern, prose), (pattern, re.search(pattern, prose).group(0))
    assert "127.0.0.1" not in body.replace(record_url, "") and body.count(record_url) == 1
    # see it: three screenshots and one demo, from passing checks, served from the evidence branch, never from the diff
    see_it = body.split("## See it")[1].split("##")[0]
    images = re.findall(r"!\[([^\]]*)\]\(([^)]+)\)", see_it)
    assert len(images) == 4 and sum(u.split("?")[0].endswith(".gif") for _, u in images) == 1
    assert all(u.startswith("https://github.com/willtech3/evergreen_chat/blob/factory-evidence/") and u.endswith("?raw=true") for _, u in images)
    assert images[-1][0] == "Demo: Picking a model and chatting"
    assert not any(p.endswith("/heads/main") or "/contents/" in p for _, p, _, _ in ctx.api_calls)
    assert "|---" not in body and "Provenance" not in body
    # the product decision leads; every decision carries its file and line
    decisions = body.split("## Needs your decision")[1].split("##")[0].strip().splitlines()
    assert decisions[0].startswith("- The picker labels the xhigh effort as Max") and decisions[0].endswith("(`src/lib/models.ts:41`).")
    # hand-written lines split from the regenerated catalog; the committed evidence fixture is what puts it over budget
    assert ("1095 hand-written lines (app 71, tests 1024); 7604 generated lines in `catalog-watch/openrouter-models.json`, "
            "`catalog-watch/report.md`, regenerated by `bun run catalog:refresh`. Estimated 120 (S; XS ≤ 50, S ≤ 150, M ≤ 400). "
            "**Over budget: 1095 hand-written lines is above size M (≤ 400).**") in body
    assert f"Ran on commit `{head[:10]}`" in body and "3 of 3 end-to-end checks passed, all in a real browser at 1440×900 and 390×844" in body
    assert "fresh Linux sandbox built from the app's committed test environment" in body
    assert cr["title"] == "Add source-backed frontier models to the picker" and cr["draft"] is False


def test_the_run_record_holds_everything_the_description_leaves_out(tmp_path, root, examples):
    from factory.web.app import create_app
    ctx, _, _ = _integrate(tmp_path, root, examples)
    with TestClient(create_app(ctx.cfg)) as client:
        run = client.get(f"/api/runs/{ctx.run_id}").json()
        [record] = run["run_records"]
        assert record["pull_request"]["url"] == "https://github.com/willtech3/evergreen_chat/pull/120"
        assert [c["ac_id"] for c in record["criteria"]] == [f"AC-{i}" for i in range(1, 13)]
        assert record["criteria"][8]["e2e"][0]["exit_code"] == 0 and record["criteria"][8]["reviewer_status"] == "not_assessed"
        assert len(record["findings"]) == 10 and record["findings"][4]["contested"] is True
        assert record["judge_summary"].startswith("I re-ran") and len(record["residual_risks"]) == 2
        assert record["size"]["flag"].startswith("Over budget") and record["size"]["measured"]["generated"] == 7604
        assert len(record["checks"]) == 12 and record["verification"]["environment"]["session_id"] == "sesn_verify_r3"
        assert len(record["provenance"]["sessions"]) == 25 and record["provenance"]["run_cost_usd"] == 52.02
        assert len(record["media"]) == 3 and all(m["current"] and m["status"] == "passed" for m in record["media"])
        assert all(len(m["media"]) == 3 and len(m["steps_observed"]) == 3 for m in record["media"])
        demo = next(x for x in record["media"][0]["media"] if x["role"] == "demo")
        served = client.get(f"/api/artifacts/{ctx.run_id}/{demo['artifact']}")
        assert served.status_code == 200 and served.content.startswith(b"GIF89a")
        assert len(run["visual_evidence"]) == 3            # "See the change" shows the same checks
        assert run["summary"]["pull_requests"][0]["url"] == record["pull_request"]["url"]
        assert record["published_media"] == {"status": "published", "reason": "3 screenshots and 1 demo",
                                             "files": record["published_media"]["files"]} and len(record["published_media"]["files"]) == 4
        js = client.get("/static/app.js").text
        assert "function runRecordBlock" in js and "#record-" in js


def test_an_evaluation_copy_says_what_it_holds_beyond_main(tmp_path, root, examples):
    _, cr, _ = _integrate(tmp_path, root, examples, evaluation=True,
                          extra_files={"verification/frontier-models/browser_journey.py": "print('old runner')\n"})
    first, second = cr["description"].splitlines()[:2]
    assert "do not merge" in first and first.count(". ") == 0
    # only what main lacks: the original change reached main through other commits, so it counts as nothing
    assert second == "> Beyond the current `main` it holds 1 file, +1 / −0: `verification/frontier-models/browser_journey.py` (+1 / −0)."
    assert len(cr["description"].rstrip("\n").splitlines()) <= 60 and cr["draft"] is True
