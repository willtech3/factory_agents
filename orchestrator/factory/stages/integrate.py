"""Open a reviewed change request; keep the approved record in Factory's artifact store.

The change request is written for its code reviewer (`pr_description.py`, post-mortem 2026-10-11 §4-§6); the full
evidence goes to the spec's run record (`run_record.py`), which the description links once. Nothing is committed to the
change's branch or the default branch; the description's screenshots and demo are published through the forge outside
every diff (`Forge.publish_media`: an orphan evidence branch on GitHub, the uploads API on GitLab; ADR-0012)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .. import pr_description, pr_media, run_record, size, visual
from ..budget import reserve_budget
from ..forge import Forge
from ..sessions import SessionContext
from ..state import WorkItemStage
from . import RunContext
from .common import enter_item_stage, persist_outputs, run_session

INTEGRATE_DESCRIPTION = """Open the change request (pull request or merge request) for specification {spec_id}.
Inputs are under /mnt/session/uploads/: pr_description.md (the description the orchestrator rendered from cr_template.md),
pr_lint.json (what would keep that draft from opening; usually empty), pr_facts.json (title, branches, labels, reviewers,
the run-record link), spec.json and merge_readiness.json. The branch {branch} is already pushed; its head must equal
{head_sha} (the exact head verified and approved by the merge judge).
{git}
Send pr_description.md as it is unless pr_lint.json lists problems; then rephrase only the sentences it names, in the
app's own terms, and keep every section and the one run-record link. open_change_request refuses a description that
breaks those rules and says why. Everything the description leaves out is already in the run record, and the screenshots
in pr_description.md are already published outside the diff. Do not write or commit anything to the target repository.
Write cr.json to /mnt/session/outputs/."""


def artifact_links(ctx: RunContext, spec_id: str) -> dict[str, str]:
    base = f"{ctx.cfg.artifact_base_url}/api/artifacts/{quote(ctx.run_id, safe='')}"
    return {"spec_json": f"{base}/specify/final-{quote(spec_id, safe='')}.json",
            "spec_md": f"{base}/specify/final-{quote(spec_id, safe='')}.md",
            "verdict": f"{base}/judge/final-verdict.json",
            "merge_readiness": f"{base}/integrate/{quote(spec_id, safe='')}-approved-merge_readiness.json"}


def make_open_change_request(ctx: RunContext, forge: Forge, spec_id: str, work_item_id: str, expected_branch: str,
                             expected_target: str, *, record_url: str | None = None, allowed_words: list[str] | None = None,
                             banner: str = ""):
    """The `open_change_request` tool. The controller owns the branches, labels and draft flag, prepends the evaluation
    banner, and lints the description (`pr_description.lint`) before the forge sees it; a refused description comes
    back to the integrator with the reasons, so it can rephrase and call again. `record_url=None` skips the lint
    (callers that open a change request without a run record)."""
    def handler(tool_input: dict[str, Any], sctx: SessionContext) -> str:
        if tool_input["source_branch"] != expected_branch:
            return json.dumps({"error": f"source_branch must be {expected_branch}"})
        if tool_input["target_branch"] != expected_target:
            return json.dumps({"error": f"target_branch must be {expected_target}"})
        opened = _opened(ctx, spec_id, expected_branch)
        if opened is not None:
            # Already open: a reattached controller replays a tool call its predecessor answered (ADR-0013), and the
            # forge must not get a second change request for the same branch.
            return json.dumps({"id": opened["id"], "url": opened["url"], "web_url": opened["url"], "iid": opened["id"]})
        evaluation = ctx.evaluation_target
        title = tool_input["title"]
        description = banner + tool_input["description_markdown"]
        if record_url is not None:
            problems = pr_description.title_problems(title) + pr_description.lint(
                tool_input["description_markdown"], record_url=record_url, allowed_words=allowed_words or (),
                reserved_lines=len(banner.splitlines()))
            if problems:
                return json.dumps({"error": "description refused; fix these and call again", "problems": problems})
        if evaluation:
            title = "[Factory evaluation] " + title
        cr = forge.create_change_request(
            source_branch=tool_input["source_branch"], target_branch=tool_input["target_branch"],
            title=title[:120], description=description,
            labels=sorted(set(tool_input.get("labels", []) + ["factory", f"spec:{spec_id}", f"run:{ctx.run_id}"])),
            reviewers=tool_input.get("reviewers", []), draft=bool(evaluation or tool_input.get("draft", False)),
        )
        ctx.store.put_artifact(ctx.run_id, "integrate", f"{spec_id}-pr_description.md", description.encode(), replace=True)
        ctx.event("cr.opened", {"id": cr.id, "url": cr.url, "branch": cr.source_branch, "forge": forge.kind},
                  work_item_id=work_item_id, spec_id=spec_id)
        ctx.notifier.change_request_opened(ctx.run_id, spec_id, cr.url)
        return json.dumps({"id": cr.id, "url": cr.url, "web_url": cr.url, "iid": cr.id})
    return handler


def evaluation_banner(base_sha: str, default_branch: str, beyond: dict[str, Any] | None) -> str:
    """One sentence that this is an evaluation copy, then what it holds beyond the current default branch: the part a
    reviewer of the copy has to read (post-mortem 2026-10-11 §6)."""
    text = (f"> **Evaluation copy, do not merge:** this draft replays an already-merged change against its original "
            f"baseline `{base_sha[:10]}`.\n")
    if beyond is None:
        return text + f"> What it holds beyond `{default_branch}` was not measured.\n\n"
    files = beyond.get("files", [])
    if not files:
        return text + f"> It holds nothing beyond the current `{default_branch}`.\n\n"
    listed = ", ".join(f"`{f['path']}` (+{f['added']} / −{f['removed']})" for f in files[:5])
    more = f" and {len(files) - 5} more" if len(files) > 5 else ""
    return (text + f"> Beyond the current `{default_branch}` it holds {len(files)} file{'s' if len(files) != 1 else ''}, "
            f"+{beyond['added']} / −{beyond['removed']}: {listed}{more}.\n\n")


def run_integrate(ctx: RunContext, spec: dict[str, Any], verdict: dict[str, Any], findings: dict[str, Any],
                  response: dict[str, Any], test_results: Any, run_summary: dict[str, Any],
                  base_branch: str, *, verifier: Any | None = None, spec_md: str = "",
                  spec_verdict: dict[str, Any] | None = None) -> dict[str, Any]:
    """Write the spec's run record, render and lint the reviewer-first description, and open the change request at the
    exact head the merge judge approved: directly when the description passes its lint, through the integrator (which
    rewords only what the lint names) when it does not."""
    assert ctx.forge is not None, "a forge is required to open change requests"
    sid, wid = spec["spec_id"], spec["work_item_id"]
    branch, judged_sha = verdict["_branch"], verdict["_head_sha"]
    enter_item_stage(ctx, wid, sid, WorkItemStage.INTEGRATING)

    # No integration commit: the PR must contain the exact candidate the judge reviewed.
    head_sha = judged_sha
    stats: dict[str, Any] | None = None
    measured: dict[str, Any] | None = None
    beyond: dict[str, Any] | None = None
    default_branch = _normal_default_branch(ctx, base_branch)
    if verifier is not None:
        repo = verifier.checkout(branch)
        if _git(repo, "rev-parse", "HEAD").strip() != judged_sha:
            raise RuntimeError("implementation branch changed after the merge judgement; review the new head first")
        if ctx.evaluation_target:
            base = verifier.checkout(base_branch)
            if _git(base, "rev-parse", "HEAD").strip() != ctx.evaluation_target[1]:
                raise RuntimeError("evaluation comparison branch moved after review")
            repo = verifier.checkout(branch)
        base_sha = verdict.get("_base_sha") or _merge_base(repo, base_branch, judged_sha)
        if base_sha:
            stats, measured = size.measure_repo(repo, base_sha, judged_sha, spec.get("regenerated") or [])
        if ctx.evaluation_target and stats is not None:
            beyond = _numstat_beyond(verifier, default_branch, judged_sha, [f["path"] for f in stats["files"]])
            verifier.checkout(branch)
    budget = getattr(ctx.policy, "size_budget_lines", None) or size.DEFAULT_BUDGET
    estimate = size.estimate_of(spec)
    sized = {"diff": stats, "measured": measured, "estimate": estimate, "budget": dict(budget),
             "flag": size.flag(measured, estimate, budget), "line": size.line(measured, estimate, budget)}
    ctx.store.put_artifact(ctx.run_id, "integrate", f"{sid}-approved-merge_readiness.json",
                           json.dumps(verdict, indent=2).encode())

    links = artifact_links(ctx, sid)
    evidence = [e for e in visual.summaries(ctx.cfg.runs_dir / ctx.run_id / "artifacts", ctx.store.events(ctx.run_id))
                if e.get("spec_id") == sid]
    record = run_record.build(run_id=ctx.run_id, spec=spec, verdict=verdict, findings=findings, response=response,
                              test_results=test_results if isinstance(test_results, dict) else {}, run_summary=run_summary,
                              visual_evidence=evidence, size=sized, base_branch=base_branch, artifact_links=links)
    media_markdown = publish_media(ctx, sid, judged_sha, evidence, record)
    run_record.store(ctx, record)

    record_url = pr_description.run_record_url(ctx.cfg.artifact_base_url, ctx.run_id, sid)
    facts = pr_description.facts(spec=spec, verdict=verdict, test_results=test_results if isinstance(test_results, dict) else {},
                                 preview_mode=ctx.cfg.preview_mode, head_sha=judged_sha, diff_stats=stats, record_url=record_url,
                                 size_line=sized["line"], media_markdown=media_markdown)
    draft = pr_description.render(facts, ctx.cfg.templates_dir / "cr_template.md")
    banner = evaluation_banner(ctx.evaluation_target[1], default_branch, beyond) if ctx.evaluation_target else ""
    allowed_words = [str(spec.get("title") or ""), str(spec.get("goal") or "")]
    problems = pr_description.title_problems(facts["title"]) + pr_description.lint(
        draft, record_url=record_url, allowed_words=allowed_words, reserved_lines=len(banner.splitlines()))
    pr_facts = {"title": facts["title"], "source_branch": branch, "target_branch": base_branch, "head_sha": judged_sha,
                "labels": ["factory", f"spec:{sid}", f"run:{ctx.run_id}"], "reviewers": list(run_summary.get("reviewers") or []),
                "run_record_url": record_url, "draft": bool(ctx.evaluation_target),
                "evaluation_banner": banner or None, "max_lines": pr_description.MAX_LINES}
    open_change_request = make_open_change_request(ctx, ctx.forge, sid, wid, branch, base_branch, record_url=record_url,
                                                   allowed_words=allowed_words, banner=banner)
    res = None
    if not problems:
        # The rendered description passes its lint: the controller opens the change request itself, through the same
        # fenced tool the integrator would call. An agent session here only ever passed the draft through (live run 2:
        # a Sonnet session, ~2 minutes, to send pr_description.md unchanged).
        sctx = SessionContext(session_id="controller", run_id=ctx.run_id, stage="integrate", work_item_id=wid)
        answer = json.loads(open_change_request({"source_branch": branch, "target_branch": base_branch, "title": facts["title"],
                                                 "description_markdown": draft, "labels": pr_facts["labels"],
                                                 "reviewers": pr_facts["reviewers"], "draft": pr_facts["draft"]}, sctx))
        if "error" in answer:
            problems = list(answer.get("problems") or [answer["error"]])
    if _opened(ctx, sid, branch) is None:
        # Only sentences the lint names need rewording, in the app's own terms: that is the integrator's whole job now.
        uploads = {
            "spec.json": json.dumps(spec, indent=2).encode(),
            "merge_readiness.json": json.dumps(verdict, indent=2).encode(),
            "pr_description.md": draft.encode(),
            "pr_lint.json": json.dumps({"problems": problems}, indent=2).encode(),
            "pr_facts.json": json.dumps(pr_facts, indent=2).encode(),
        }
        reserve_budget(ctx, "integrate")
        res = run_session(
            ctx, prefix=f"{sid}-", spec_id=sid,
            agent_key="integrator", title=f"{ctx.run_id} integrate {sid}", run_id=ctx.run_id, stage="integrate",
            uploads=uploads, work_item_id=wid,
            description=INTEGRATE_DESCRIPTION.format(spec_id=sid, branch=branch, head_sha=head_sha,
                                                     git=ctx.forge.sandbox_git_instructions(ctx.cfg.repo_url, base_branch, branch, read_only=True)),
            rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["integrate"],
            custom_tools={"open_change_request": open_change_request},
            repository=ctx.forge.session_repository_resource(ctx.cfg.repo_url, branch),
        )
    # The forge result, not an agent-authored cr.json, proves that a PR exists.
    opened = _opened(ctx, sid, branch)
    if opened is None:
        persist_outputs(ctx, "integrate", res, prefix=f"{sid}-unconfirmed-{res.session_id}-", work_item_id=wid, spec_id=sid)
        raise RuntimeError(f"integrator session {res.session_id} did not open a change request for {sid}")
    cr = {"id": opened["id"], "url": opened["url"], "web_url": opened["url"], "iid": opened["id"]}
    record["pull_request"] = {"id": opened["id"], "url": opened["url"], "description": f"integrate/{sid}-pr_description.md"}
    run_record.store(ctx, record)
    if res is not None:
        res.outputs["cr.json"] = json.dumps(cr, indent=2).encode()
        persist_outputs(ctx, "integrate", res, prefix=f"{sid}-", work_item_id=wid, spec_id=sid)
    else:
        digest = ctx.store.put_artifact(ctx.run_id, "integrate", f"{sid}-cr.json", json.dumps(cr, indent=2).encode())
        ctx.event("artifact.produced", {"artifact_type": "cr", "path": f"integrate/{sid}-cr.json", "sha256": digest,
                                        "opened_by": "controller"}, stage="integrate", work_item_id=wid, spec_id=sid)
    ctx.event("work_item.stage", {"stage": WorkItemStage.CR_OPEN.value, "cr_id": cr.get("id", cr.get("iid"))},
              work_item_id=wid, spec_id=sid)
    return cr


def publish_media(ctx: RunContext, sid: str, head_sha: str, evidence: list[dict[str, Any]], record: dict[str, Any]) -> str:
    """Up to three screenshots and one demo for the description's "See it" section, published through the forge where it
    keeps them out of every diff (`Forge.publish_media`; ADR-0012). A failure to publish leaves the section out and is
    recorded; it never blocks the change request."""
    if not ctx.cfg.pr_media or not hasattr(ctx.forge, "publish_media"):
        record["published_media"] = {"status": "off"}
        return ""
    items, why = pr_media.select(evidence, head_sha, ctx.cfg.runs_dir / ctx.run_id / "artifacts")
    if not items:
        record["published_media"] = {"status": "none", "reason": why}
        return ""
    try:
        markdown = ctx.forge.publish_media(f"{ctx.run_id}/{sid}/{head_sha[:10]}", items)
    except Exception as exc:  # noqa: BLE001  (the forge's HTTP errors)
        record["published_media"] = {"status": "failed", "reason": str(exc)[:300]}
        ctx.event("media.publish_failed", {"reason": str(exc)[:300]}, spec_id=sid, stage="integrate")
        return ""
    record["published_media"] = {"status": "published", "reason": why, "files": [{"name": i.name, "role": i.role} for i in items]}
    return markdown


def _opened(ctx: RunContext, spec_id: str, branch: str) -> dict[str, Any] | None:
    """The `cr.opened` this spec's branch got from the forge, if any."""
    return next((ev.payload for ev in reversed(ctx.store.events(ctx.run_id))
                 if ev.kind == "cr.opened" and ev.spec_id == spec_id and ev.payload.get("branch") == branch), None)


def _normal_default_branch(ctx: RunContext, base_branch: str) -> str:
    """The repository's ordinary default branch: the recorded one for an evaluation run, else the change's base."""
    for ev in reversed(ctx.store.events(ctx.run_id)):
        if ev.kind == "artifact.produced" and ev.payload.get("artifact_type") == "evaluation_target":
            return str(ev.payload.get("normal_target") or "main")
    return base_branch


def _ref(repo: Path, branch: str) -> str | None:
    return next((r for r in (f"origin/{branch}", branch)
                 if subprocess.run(["git", "rev-parse", "--verify", "--quiet", r], cwd=repo, capture_output=True,
                                   check=False).returncode == 0), None)


def _merge_base(repo: Path, branch: str, head_sha: str) -> str | None:
    """Where `head_sha` left `branch` (as the forge shows a change request); None when git cannot say."""
    try:
        ref = _ref(repo, branch)
        return _git(repo, "merge-base", ref, head_sha).strip() if ref else None
    except (subprocess.CalledProcessError, OSError):
        return None


def _numstat_beyond(verifier: Any, branch: str, head_sha: str, paths: list[str]) -> dict[str, Any] | None:
    """What an evaluation copy holds beyond the current `branch`, in the files the change touches: a tree diff from
    the branch's tip, not from the merge base, because the copy is cut from a baseline the branch already contains;
    a change that reached the branch through other commits (a merge or a squash of the original) counts as nothing.
    None when git cannot say."""
    if not paths:
        return {"files": [], "added": 0, "removed": 0}
    try:
        repo = verifier.checkout(branch)
        ref = _ref(repo, branch)
        return pr_description.diff_stats(_git(repo, "diff", "--numstat", ref, head_sha, "--", *paths)) if ref else None
    except (subprocess.CalledProcessError, OSError):
        return None


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout
