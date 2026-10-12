"""Stage 3, per spec: implement -> adversarial review -> respond -> fresh review -> merge judge.

Each box is its own session with its own context. The reviewer in round 2 is a *new*
session that receives the new diff plus the implementer's responses; it is not a
continuation of round 1 (one fresh pass per round; Song, arXiv:2603.16244). The merge
judge receives the orchestrator's deterministic checks and may not override them.

Authoritative verification includes the spec's `test_plan.e2e` entries run against the
branch's **preview environment**. The platform's own CI (GitHub Actions or GitLab CI) deploys
every factory branch to a per-branch preview; the orchestrator reads the preview URL from the
forge's deployment or environment record (`Forge.environment_url_for_branch`) and hands it to
the implementer and reviewer as `preview.json`. No agent deploys anything
(docs/05-platform-and-deployment.md).

Repository access in the sandbox is the forge's business: GitHub sessions get the native
`github_repository` resource (token injected by Anthropic's git proxy), GitLab sessions get the
vault/bearer-header clone instructions of ADR-0003. Both come from `Forge.sandbox_git_instructions`.

`FACTORY_PREVIEW=local` (no platform yet): the orchestrator runs the authoritative verification on its own
host, in a per-run virtual environment it builds from the target repository (`LocalVerifier.prepare_environment`),
and starts the service itself from the manifest entrypoint for the preview entries (`LocalPreview`). No sandbox
can reach that instance, and `preview.json` says so (`unreachable_from_sandbox`): reviewers start their own
instance from the same entrypoint and judge the preview entries against it (post-mortem 2026-10-10, issues 7, 13).

`FACTORY_PREVIEW=managed` (an onboarded app): nothing of the target repository executes on the orchestrator host. Each
pushed head is verified in a fresh Anthropic-hosted sandbox built from the app's approved test recipe
(`hosted_verifier.HostedVerifier`): the recipe's own lifecycle, the test_plan commands, pytest collection and the
"preview" e2e entries with their browser evidence, all bound to the exact head and recipe. The host only reads git.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .. import visual
from .. import checks, security, steps, verification
from ..hosted_verifier import ENVIRONMENT_CHECK
from ..budget import reserve_budget
from ..checks import CheckResult
from ..config import agent_model
# The controller's own verification host and the one verification pipeline (ADR-0014); re-exported for older callers.
from ..local_verifier import SETUP_CHECK, LocalPreview, LocalVerifier, measure_size, parse_junit, verification_env  # noqa: F401
from ..verification import (evidence_from_reports, journey_name, passed_test_ids, preview_info,  # noqa: F401
                            wait_for_preview)
from ..state import WorkItemStage
from . import RunContext
from .common import enter_item_stage, persist_outputs, platform_skill, public, reject_output, require_json, run_session, stamp_provenance, artifact_json_or_none, would_reuse
from .common import git as _git                # one git helper for every stage

NO_FORGE_GIT_SETUP = """Git access: no forge is configured for this run. The repository at {repo_url} is not reachable
from the sandbox; work on branch {branch} from {base_branch} only if it was mounted for you, otherwise record the
blocker in blocked.md."""

NO_FORGE_GIT_READ_ONLY = """Git access: no forge is configured for this run. If the repository at {repo_url} was mounted for
you, read branch {branch} there; this session is read-only on it: never commit, never push, and write everything you
produce under /mnt/session/outputs/."""

IMPLEMENT_DESCRIPTION = """Implement specification {spec_id} from /mnt/session/uploads/spec.json{spec_md_clause}.
{git}
{skill_clause}
{review_clause}
Write implementation_report.json{response_clause} to /mnt/session/outputs/ and validate it with the validate.py tool
(schema implementation_report.schema.json). The report must map every acceptance criterion to the tests that prove it, as
`criteria: [{{"ac_id": "AC-1", "tests": ["tests/test_x.py::test_name", ...]}}, ...]` with pytest node ids exactly as
`pytest --collect-only -q` prints them, or for Bun tests `path/to/x.test.ts::outer describe > inner describe > test name`
(the file relative to the repository root, describe blocks outermost first, the test name as Bun reports it); the
orchestrator runs the test plan itself, derives the passing set from its own run's test reports, and fails any automated
criterion whose tests it did not collect or did not see pass. Run every command in the spec's test_plan before finishing
and record the results (a structured entry {{"runner", "select"}} runs as `python3 -m pytest -q <select>` or
`bun test <select>`; a `journey` e2e entry is `factory:journey <name>`). For a criterion proved by an e2e entry itself, use `e2e:<zero-based index>` in tests (for example
`e2e:0` for test_plan.e2e[0]). That entry must declare the criterion in covers_ac and pass in the controller's run,
including its exact browser evidence when visual; a baseline or an agent's claim never counts. {preview_clause}{guidance_clause}"""

GUIDANCE_CLAUSE = """
The owner approved this spec at Gate 2 with notes for you: /mnt/session/uploads/gate2_guidance.md. Follow them where they fit
inside the approved spec. They never change its acceptance criteria, allowed paths or test plan; where a note conflicts
with the spec, follow the spec and record the conflict in implementation_report.json notes."""

REVIEW_DESCRIPTION = """Review the change for specification {spec_id}. The diff is at /mnt/session/uploads/diff.patch
(base {base_sha}, head {head_sha}); the repository is cloned at /workspace/repo on branch {branch}.
{git}
{skill_clause}
This is review round {round}.{second_round}
{preview_clause}
Write findings.json to /mnt/session/outputs/ and validate it. You never edit source files."""

DELTA_REVIEW_CLAUSE = """ This is a follow-up round. First judge every finding in /mnt/session/uploads/previous_findings.json
against its refutation condition using review_response.json, and record each as upheld or withdrawn under its unchanged id.
Then review /mnt/session/uploads/delta.patch: the commits since the head the previous round reviewed ({previous_head}..{head_sha}).
diff.patch is the whole change, for context only: raise a new finding only on lines the delta changes or on behaviour the delta
breaks. Run the test plan once at this head; re-run a previous reproduction only to judge its own finding."""

NO_NEW_COMMITS_CLAUSE = """ This is a follow-up round and the implementer pushed no new commits: every previous finding was
answered in review_response.json without a code change. Judge each finding in /mnt/session/uploads/previous_findings.json against
its refutation condition and record it upheld or withdrawn under its unchanged id; do not review the unchanged diff again."""

PREVIEW_CLAUSE = """The platform has deployed this branch to a preview environment; its URL and pipeline are in
/mnt/session/uploads/preview.json. Run every test_plan.e2e entry with target "preview" against that URL (export
BASE_URL from preview.json) and record each command's exit code and output. The orchestrator re-runs them itself."""

LOCAL_PREVIEW_CLAUSE = """There is no preview deployment for this branch and none is reachable from this sandbox:
/mnt/session/uploads/preview.json says status "unreachable_from_sandbox" with a null url. The orchestrator runs every
test_plan.e2e entry itself, on its own host, against an instance it starts from the service manifest's runtime.entrypoint
(the manifest's runtime.port replaced by a free port, also exported as PORT) in a per-run virtual environment built from the
repository's pyproject (or the manifest's runtime.setup command); that run is the one that counts. Stand in for it the
same way: start the service from that entrypoint on a free port yourself, export BASE_URL, run every "preview" entry and
every "local" entry against it, and record each command, exit code and output tail. The service must start with the
manifest command and answer the manifest's health path, with nothing installed beyond the repository's declared
dependencies."""

LOCAL_PREVIEW_REVIEW_CLAUSE = LOCAL_PREVIEW_CLAUSE + """ The instance you start from the manifest entrypoint is the preview
stand-in for your spec_conformance table: record each run in checks_performed with target "local-manifest" and mark every
user_visible criterion met or not_met on that evidence. not_assessed is only for a criterion you could not exercise because
the entrypoint failed to start (which is itself a blocking finding), never because the orchestrator's instance was out of
reach."""

NO_PREVIEW_CLAUSE = """No preview environment exists for this branch yet (see "status" in /mnt/session/uploads/preview.json).
Push your branch early; the platform's pipeline deploys it. Run the test_plan.e2e entries with target "local" now and
record the "preview" ones as not run. The orchestrator runs the preview entries against the deployed branch once the
pipeline for your pushed head finishes; any later round of yours receives preview.json with the URL."""

SECURITY_REVIEW_DESCRIPTION = """Security review of the change for specification {spec_id}. The diff is at /mnt/session/uploads/diff.patch
(base {base_sha}, head {head_sha}); the repository is cloned at /workspace/repo on branch {branch}.
{git}
{skill_clause}
This is review round {round}. Two general adversarial reviewers run beside you on the same diff; you cover what they
do not: trust boundaries, attacker-controlled input, authorisation, secrets, dependencies, and the attack paths the
diff opens. The orchestrator's deterministic pre-checks are at /mnt/session/uploads/security_prechecks.json (a secret
scan over the added lines and, when the host has the tools, a dependency audit and a SAST run): inputs to judge, not
verdicts.{second_round}
{preview_clause}
Write findings.json to /mnt/session/outputs/ (category security, or concurrency / data_integrity when that is what the
defect is; one cwe per finding) and validate it. You never edit source files."""

MERGE_JUDGE_DESCRIPTION = """Decide merge readiness for specification {spec_id}, round {round}.
Inputs are under /mnt/session/uploads/: spec.json, diff.patch, findings.json, review_response.json,
test_results.json (authoritative; run by the orchestrator), deterministic_checks.json (you may not override these).
{git}
{skill_clause}
{criteria_clause}
Write merge_readiness.json to /mnt/session/outputs/ and validate it."""

CRITERIA_CLAUSE = """Fill `criteria` with your own status per acceptance criterion from evidence you located: the orchestrator's
e2e rows in test_results.json (each names the criteria it covers in covers_ac, and its target and exit code) are the
authoritative preview evidence; the reviewer's spec_conformance is an input, not the answer."""

MANAGED_CRITERIA_CLAUSE = CRITERIA_CLAUSE + """ This run used preview mode "managed": test_results.json `environment` names the
hosted sandbox session, the exact head and the approved recipe fingerprint every row is bound to. A row with exit_code null
was not run and proves nothing. A criterion is met only through rows that name it in covers_ac (UI criteria also need that
row's passing visual evidence); the recipe's baseline check (`hosted_environment`) shows the app runs, never that a criterion
is met. Inspect the uploaded visual_evidence.json and screenshots; if you cannot view an image, say so and do not claim visual
review."""

LOCAL_CRITERIA_CLAUSE = CRITERIA_CLAUSE + """ This run used preview mode "local": the orchestrator started the service from the
manifest entrypoint on its own host and ran the "preview" entries there (test_results.json `preview.orchestrator_instance`
and the e2e rows); reviewers could not reach that instance and ran the entries against one they started themselves. A
service criterion whose e2e rows exit 0 is met on that evidence. UI criteria additionally require passing visual evidence. Inspect the uploaded visual_evidence.json and screenshots; if you cannot view an image, explicitly report that limitation and do not claim visual review."""


MANAGED_PREVIEW_CLAUSE = """There is no preview URL for this branch and none is reachable from this sandbox:
/mnt/session/uploads/preview.json says status "hosted_sandbox" with a null url. After you push, the orchestrator verifies
your pushed head in a separate Anthropic-hosted test sandbox built from the app's approved test recipe (the service
manifest's test_environment.profile, default test_environment.json): the recipe's prepare/start/seed and baseline check,
every test_plan command, pytest collection, and every test_plan.e2e entry against the app inside that sandbox ($BASE_URL).
Only that run counts. The recipe's trusted part (network, secrets, data, command_environment) is pinned and a change to it
blocks verification; its commands, services and checks may change with your code and are verified with it. An e2e entry
`factory:journey <name>` (a `journey` entry) runs the factory's own browser runner on the journey you write
to /mnt/session/outputs/journeys/<name>.json (the format is in the platform-contract skill): one step per step of the
entry's visual contract, acting by role, label or text, with the expectations that prove the criterion. The journey is a
run artifact the orchestrator uploads and binds by hash; never commit browser-test code or anything that reads
FACTORY_VISUAL_REQUEST or FACTORY_EVIDENCE_DIR (the factory_proof_in_diff check refuses it). Test outcomes count from the
spec's structured test commands ({"runner": "pytest" | "bun", ...}), which the sandbox runs with its own reporters, and from
pytest runs inside its other commands. Run what you can in this sandbox and record the results."""

JOURNEY_REVIEW_CLAUSE = """
The change's journeys are at /mnt/session/uploads/journeys/: run artifacts the factory's browser runner executed in the hosted
sandbox, not part of the diff. Review them as the evidence for their criteria: a journey that would still pass with the
behaviour broken leaves its criterion unproven, which is a finding against the change. A finding about a journey's own
plumbing only (how it reports a failure, its wording, a tighter check that proves nothing more) carries
"target": "verification"; it is recorded for the factory and never blocks the change or starts another round."""

MANAGED_PREVIEW_REVIEW_CLAUSE = """No preview URL is reachable from this sandbox (/mnt/session/uploads/preview.json: status
"hosted_sandbox", url null). The orchestrator verified this head in a separate Anthropic-hosted sandbox built from the app's
approved test recipe; deterministic_checks.json carries those results, and visual_evidence.json with the screenshots carries
the browser evidence. Never claim to have exercised a deployed preview. To probe behaviour, start the app in your own sandbox
from the recipe's commands; your sandbox may lack the recipe's network allowlist and test credentials, so mark what you could
not exercise not_assessed with that reason, never met."""


def _branch_for(run_id: str, spec_id: str) -> str:
    return f"factory/{run_id}/{spec_id}"


def git_clauses(ctx: RunContext, repo_url: str, base_branch: str, branch: str) -> tuple[str, str]:
    """(write clause for the implementer, read-only clause for reviewers, the merge judge and the integrator).

    One clause used to serve every role, so reviewers were told to "commit as you go and push" right before "you
    never edit source files"; a less careful pass would have pushed its probes and tripped diff_scope on the next
    verification (post-mortem issue 18)."""
    if ctx.forge is None:
        return (NO_FORGE_GIT_SETUP.format(repo_url=repo_url, base_branch=base_branch, branch=branch),
                NO_FORGE_GIT_READ_ONLY.format(repo_url=repo_url, branch=branch))
    return (ctx.forge.sandbox_git_instructions(repo_url, base_branch, branch),
            ctx.forge.sandbox_git_instructions(repo_url, base_branch, branch, read_only=True))


def review_evidence_prefix(ctx: RunContext, sid: str, round_no: int, pass_id: str,
                           base_sha: str, head_sha: str) -> tuple[str, dict[str, Any] | None]:
    """The artifact prefix of one review pass, named for the exact diff it read (base and head), and its findings when
    that pass already persisted them and they name the same diff: only a review of the same diff is reused."""
    prefix = f"{sid}-r{round_no}-base{base_sha}-head{head_sha}-{pass_id}-"
    found = artifact_json_or_none(ctx, "review", prefix + "findings.json")
    ref = (found or {}).get("diff_ref") or {}
    return prefix, found if ref.get("base_sha") == base_sha and ref.get("head_sha") == head_sha else None


class BuildBlocked(RuntimeError):
    """The implementer wrote blocked.md: it cannot go on without a change of scope or a human. Its round is not
    recorded as finished, so the build-recovery decision that follows runs that implementer again."""


def run_implement_one(ctx: RunContext, spec: dict[str, Any], repo_url: str, base_branch: str,
                      verifier: LocalVerifier, dependency_specs: dict[str, dict], *, spec_md: str = "",
                      guidance: str = "", recovery: dict[str, Any] | None = None,
                      corrective: dict[str, Any] | None = None) -> dict[str, Any]:
    """Drive one spec through implement/review/judge. Returns the final merge_readiness verdict. `guidance` is the owner's
    bounded Gate 2 approval notes for this spec (`runner.record_approval`), uploaded to every implementer round.

    `spec_md` is the writer's spec.md for this spec (the runner carries it from Gate 2); the implementer, every
    reviewer pass and the merge judge receive it beside spec.json.

    Each round is a durable step (`implement/<spec>/r<n>`, ADR-0013) with its implementer session as a step inside it
    (`.../author`), and the merge judgement is one too (`implement/<spec>/judge#<k>`, `k` counting the owner's recovery
    decisions on the spec). A replay gets the rounds that finished back from their steps and continues at the first that
    did not; an older controller's rounds are adopted from their artefacts first (`adopt_rounds`). `recovery` is the
    owner's latest build-recovery decision on the spec: a recheck (approve) verifies the final head again and asks a new
    merge judge, and a blocked round's implementer runs again. `corrective` is their latest request for changes, which
    stays authorized through later rechecks: the round it names goes back to the implementer with their instructions
    and one more review is allowed."""
    _require_recorded_preview_mode(ctx)
    build = _Build(ctx, spec, repo_url, base_branch, verifier, dependency_specs, spec_md)
    sid, wid = build.sid, build.wid
    adopt_rounds(ctx, spec, corrective)
    current = ctx.store.state(ctx.run_id).work_items.get(wid)
    if current is not None and (current.stage in (WorkItemStage.SPEC_APPROVED, WorkItemStage.BLOCKED, WorkItemStage.ESCALATED)
                                or (current.stage == WorkItemStage.MERGE_JUDGING and corrective is not None)):
        # An item that has not started (or that the owner sent back) moves to implementing; one already in a round
        # stays where its last recorded event put it.
        ctx.event("work_item.stage", {"stage": WorkItemStage.IMPLEMENTING.value, "branch": build.branch}, work_item_id=wid, spec_id=sid)
    epoch = sum(1 for e in ctx.store.events(ctx.run_id) if e.kind == "build.recovery" and e.spec_id == sid)
    corrective_round = int(corrective["round"]) if corrective is not None else None
    review_through = max(ctx.policy.review_max_rounds, (corrective_round or 0) + 1)
    carried: dict[str, Any] = {"findings": None, "response": None, "det_only": False,
                               "journeys": _blobs(ctx, latest_journeys(ctx, sid, spec))}
    round_no = 0
    while True:
        round_no += 1
        notes = guidance
        if corrective is not None and round_no > corrective_round:
            notes = guidance + "\n# Build recovery instructions from the owner\n" + corrective.get("comments", "")
        inputs = {"findings": _sha_of(carried["findings"]), "response": _sha_of(carried["response"]),
                  "det_only": carried["det_only"], "journeys": carried["journeys"], "guidance": notes,
                  "review_through": round_no > review_through}
        try:
            result = steps.run_step(ctx, f"implement/{sid}/r{round_no}", inputs,
                                    lambda n=round_no, c=carried, g=notes: build.round(n, c, g, review_through))
        except BuildBlocked as blocked:
            return {"result": "escalate", "reason": str(blocked)}
        if corrective is not None and round_no == corrective_round and result["next"] == "judge":
            # The owner sent this round back: their findings (or the round's own) go to one more implementer round.
            findings = corrective.get("review_findings") or (_json_of(ctx, result["findings"]) if result["findings"] else None)
            carried = {"findings": findings or {"findings": []}, "response": _json_of(ctx, result["response"]),
                       "det_only": False, "journeys": result["journeys"]}
            continue
        if result["next"] == "judge":
            break
        carried = {"findings": _json_of(ctx, result["findings"]), "response": _json_of(ctx, result["response"]),
                   "det_only": result["det_only"], "journeys": result["journeys"]}
    recheck = recovery is not None and recovery.get("decision") == "approve"
    return steps.run_step(ctx, f"implement/{sid}/judge#{epoch}", {"round": steps.inputs_sha(result), "recheck": recheck},
                          lambda: build.judge(round_no, result, recheck))


def _blobs(ctx: RunContext, files: dict[str, bytes]) -> dict[str, str]:
    return {name: ctx.store.put_blob(ctx.run_id, content) for name, content in sorted(files.items())}


def _files(ctx: RunContext, blobs: dict[str, str]) -> dict[str, bytes]:
    return {name: ctx.store.get_blob(ctx.run_id, sha) for name, sha in sorted(blobs.items())}


def _sha_of(value: Any) -> str | None:
    return None if value is None else steps.inputs_sha(value)


def _json_of(ctx: RunContext, blob: str | None) -> Any:
    return None if blob is None else steps.get_json(ctx, blob)


class _Build:
    """What every round of one spec's build shares: the branch, the base it is cut from, the git clauses and uploads."""

    def __init__(self, ctx: RunContext, spec: dict[str, Any], repo_url: str, base_branch: str, verifier: LocalVerifier,
                 dependency_specs: dict[str, dict], spec_md: str):
        self.ctx, self.spec, self.repo_url, self.base_branch, self.verifier = ctx, spec, repo_url, base_branch, verifier
        self.dependency_specs = dependency_specs
        self.sid, self.wid = spec["spec_id"], spec["work_item_id"]
        self.spec_md = (spec_md or spec.get("_markdown") or "").strip()
        self.branch = _branch_for(ctx.run_id, self.sid)
        self.git, self.git_ro = git_clauses(ctx, repo_url, base_branch, self.branch)   # the implementer pushes; everyone else reads
        self.base_sha = _git(verifier.checkout(base_branch), "rev-parse", "HEAD").strip()
        self.skill_uploads, self.skill_clause = platform_skill(ctx)
        self.managed = ctx.cfg.preview_mode == "managed"

    def mount(self, checkout: str) -> dict[str, Any] | None:
        ctx = self.ctx
        return ctx.forge.session_repository_resource(self.repo_url, checkout) if ctx.forge is not None else None

    def spec_uploads(self) -> dict[str, bytes]:
        out = {"spec.json": json.dumps(public(self.spec), indent=2).encode(), **self.skill_uploads}
        if self.spec_md:
            out["spec.md"] = self.spec_md.encode()
        return out

    # ---------------------------------------------------------------- one round

    def round(self, round_no: int, carried: dict[str, Any], guidance: str, review_through: int) -> dict[str, Any]:
        """Implementer, the orchestrator's own verification, and (unless the round goes straight to the judge) the review.
        Its result is what the next round and the judge need, the larger parts kept as blobs."""
        ctx, sid, wid = self.ctx, self.sid, self.wid
        findings, det_only = carried["findings"], carried["det_only"]
        author_inputs = {"findings": _sha_of(findings), "det_only": det_only, "journeys": carried["journeys"], "guidance": guidance}
        reused_author = steps.recorded(ctx, f"implement/{sid}/r{round_no}/author", author_inputs)
        author = steps.run_step(ctx, f"implement/{sid}/r{round_no}/author", author_inputs,
                                lambda: self.author(round_no, findings, det_only, _files(ctx, carried["journeys"]), guidance))
        report = _json_of(ctx, author["report"])
        response = _json_of(ctx, author["response"])
        journeys = _files(ctx, author["journeys"])
        if findings and response is not None:
            response["round"] = round_no - 1
            ctx.event("review.response", {"round": round_no - 1, "dispositions": [r["disposition"] for r in response["responses"]]},
                      work_item_id=wid, spec_id=sid, actor="agent")
        v = self.verify(round_no, report, journeys, check_reported_head=reused_author)
        det = v["det"]
        result = {"report": author["report"], "response": steps.put_json(ctx, response) if response is not None else None,
                  "journeys": author["journeys"], "head_sha": v["head_sha"], "base_sha": self.base_sha,
                  "det": [d.to_dict() for d in det], "test_results": steps.put_json(ctx, v["test_results"]),
                  "patch": ctx.store.put_blob(ctx.run_id, v["diff"]["patch"].encode()), "visual": _blobs(ctx, v["visual_uploads"]),
                  "preview": v["preview"], "findings": steps.put_json(ctx, findings) if findings is not None else None,
                  "next": "judge", "det_only": False}
        if round_no > review_through:
            return result            # last implementer response done; go straight to the merge judge
        if det_only and all(d.passed for d in det):
            # The previous round's only standing findings were the orchestrator's own checks; they pass on the
            # pushed head, so a fresh adversarial read would re-prove what round one already proved. Straight to
            # the judge, which still sees findings.json and the implementer's answers (post-mortem issue 6).
            ctx.event("review.findings", {"round": round_no, "count": 0, "blocking": 0, "deterministic_only": True,
                                          "skipped": "deterministic checks pass on the response; no review round"},
                      work_item_id=wid, spec_id=sid)
            return result
        merged = self.review(round_no, report, response, findings, v, journeys)
        result["findings"] = steps.put_json(ctx, merged)
        standing = standing_findings(merged["findings"])
        if not standing and all(d.passed for d in det):
            return result            # nothing standing; skip the implementer round and go to the judge
        # Only the orchestrator's own checks stand (no agent finding is blocking or major): the next round is an
        # implementer response re-verified by the checks, not a fresh review of the whole diff.
        result["det_only"] = _deterministic_only(standing)
        enter_item_stage(ctx, wid, sid, WorkItemStage.RESPONDING, deterministic_only=result["det_only"])
        return {**result, "next": "respond"}

    def author(self, round_no: int, findings: dict[str, Any] | None, det_only: bool, journeys: dict[str, bytes],
               guidance: str) -> dict[str, Any]:
        """The implementer's session for this round (round 1 implements; later rounds respond to findings)."""
        ctx, sid, wid = self.ctx, self.sid, self.wid
        uploads = {**self.spec_uploads(), **{f"journeys/{name}.json": content for name, content in sorted(journeys.items())}}
        if guidance:
            uploads["gate2_guidance.md"] = guidance.encode()          # the owner's bounded approval notes; never a spec change
        for dsid, ds in self.dependency_specs.items():
            uploads[f"dependency_specs/{dsid}.json"] = json.dumps(public(ds), indent=2).encode()
        review_clause, response_clause = "", ""
        preview = preview_info(ctx, self.branch)
        uploads["preview.json"] = json.dumps(preview, indent=2).encode()
        preview_clause = (MANAGED_PREVIEW_CLAUSE if preview.get("mode") == "managed" else PREVIEW_CLAUSE if preview["status"] == "ready"
                          else LOCAL_PREVIEW_CLAUSE if preview.get("mode") == "local" else NO_PREVIEW_CLAUSE)
        if journeys:
            preview_clause += (" Previously saved browser journeys are at /mnt/session/uploads/journeys/. "
                               "Reuse these definitions; do not reconstruct them from the spec prose. Preserve existing assertions "
                               "unless a review finding requires a targeted correction. The controller carries unchanged journeys forward.")
        if findings:
            uploads["findings.json"] = json.dumps(findings, indent=2).encode()
            review_clause = ("Reviewer findings are at /mnt/session/uploads/findings.json. Respond to every finding "
                             "(fixed / refuted with evidence / accepted_risk) and push the updated branch.")
            if det_only:
                review_clause += (" Every standing finding is a DET-* entry: a deterministic check the orchestrator runs itself "
                                  "(its detail and reproduction say exactly what failed). Fix the cause and push; the orchestrator "
                                  "re-runs the checks on your pushed head and, if they pass, goes straight to the merge judge.")
            response_clause = " and review_response.json"
        session = dict(
            prefix=f"{sid}-r{round_no}-", spec_id=sid,
            agent_key="implementer", title=f"{ctx.run_id} implement {sid} r{round_no}", run_id=ctx.run_id,
            uploads=uploads, work_item_id=wid,
            description=IMPLEMENT_DESCRIPTION.format(spec_id=sid, git=self.git, review_clause=review_clause, skill_clause=self.skill_clause,
                                                     spec_md_clause=" (spec.md is the readable form)" if self.spec_md else "",
                                                     response_clause=response_clause, preview_clause=preview_clause,
                                                     guidance_clause=GUIDANCE_CLAUSE if guidance else ""),
            rubric_md=ctx.grader_rubric("implementation"), max_iterations=ctx.policy.outcome_max_iterations,
            budget_usd=ctx.policy.stage_budget_usd["implement"],
            repository=self.mount(self.base_branch if round_no == 1 else self.branch),
        )
        if not would_reuse(ctx, stage="implement", **session):
            reserve_budget(ctx, "implement")
        res = run_session(ctx, stage="implement", **session)
        if "blocked.md" in res.outputs:
            persist_outputs(ctx, "implement", res, prefix=f"{sid}-r{round_no}-", work_item_id=wid, spec_id=sid)
            ctx.event("work_item.stage", {"stage": WorkItemStage.BLOCKED.value}, work_item_id=wid, spec_id=sid)
            reason = res.outputs["blocked.md"].decode(errors="replace")
            raise BuildBlocked(str(reject_output(res, reason)))    # its session is not reused: a recovery asks again
        report = require_json(res, "implementation_report.json")
        stamp_provenance(report, ctx, res, "Implementer", agent_model("implementer"))
        res.outputs["implementation_report.json"] = json.dumps(report, indent=2).encode()
        response = None
        if findings:
            response = require_json(res, "review_response.json")
            stamp_provenance(response, ctx, res, "Implementer", agent_model("implementer"))
            res.outputs["review_response.json"] = json.dumps(response, indent=2).encode()
        persist_outputs(ctx, "implement", res, prefix=f"{sid}-r{round_no}-", work_item_id=wid, spec_id=sid)
        return {"report": steps.put_json(ctx, report), "response": steps.put_json(ctx, response) if response is not None else None,
                "journeys": _blobs(ctx, latest_journeys(ctx, sid, self.spec))}

    # ---------------------------------------------------------------- verification

    def verify(self, round_no: int, report: dict[str, Any], journeys: dict[str, bytes], *,
               check_reported_head: bool = False) -> dict[str, Any]:
        """The orchestrator's own verification of the branch head (`verification.verify`) and the checks no agent can
        override (`verification.judge`)."""
        ctx, sid, wid, spec, verifier = self.ctx, self.sid, self.wid, self.spec, self.verifier
        repo = verifier.checkout(self.branch)
        head_sha = _git(repo, "rev-parse", "HEAD").strip()
        ctx.event("verification.started", {"head_sha": head_sha, "round": round_no}, spec_id=sid, work_item_id=wid)
        diff = verifier.analyse(repo, self.base_sha, head_sha)
        target = verification.target_for(ctx, repo, self.base_sha, head_sha, self.branch)
        bundle = verification.verify(ctx, target, verification.plan_of(spec), spec=spec, verifier=verifier, journeys=journeys,
                                     round_no=round_no, work_item_id=wid)
        visual_uploads = visual.persist(ctx, sid, round_no, head_sha, bundle.e2e)
        measured = measure_size(repo, self.base_sha, head_sha, spec)
        det = verification.judge(bundle, spec, diff=diff, report=report, manifest_path=repo / ctx.cfg.service_manifest_path,
                                 schema_dir=ctx.cfg.schemas_dir, size=measured, size_budget_lines=ctx.policy.size_budget_lines,
                                 reported_head=report.get("head_sha") if check_reported_head else None)
        test_results = bundle.test_results()
        ctx.store.put_derived_artifact(ctx.run_id, "implement", f"{sid}-r{round_no}-deterministic_checks.json",
                                       json.dumps([d.to_dict() for d in det], indent=2).encode())
        ctx.store.put_derived_artifact(ctx.run_id, "implement", f"{sid}-r{round_no}-test_results.json",
                                       json.dumps(test_results, indent=2).encode())
        return {"repo": repo, "head_sha": head_sha, "diff": diff, "det": det, "test_results": test_results, "preview": bundle.preview,
                "visual_uploads": visual_uploads, "measured": measured, "cmd_results": bundle.commands, "e2e_results": bundle.e2e}

    # ---------------------------------------------------------------- review

    def review(self, round_no: int, report: dict[str, Any], response: dict[str, Any] | None, findings: dict[str, Any] | None,
               v: dict[str, Any], journeys: dict[str, bytes]) -> dict[str, Any]:
        """Fresh-context adversarial review shaped by the change (`review_plan`): general passes plus a security pass,
        merged, with the orchestrator's failed checks as findings no reviewer can clear."""
        ctx, sid, wid, verifier = self.ctx, self.sid, self.wid, self.verifier
        base_sha, branch, managed, head_sha, repo, diff, det = self.base_sha, self.branch, self.managed, v["head_sha"], v["repo"], v["diff"], v["det"]
        preview, visual_uploads = v["preview"], v["visual_uploads"]
        security_available = "security_reviewer" in ctx.cfg.agents
        previous_head = ((findings or {}).get("diff_ref") or {}).get("head_sha") if findings and response else None
        delta = delta_since(verifier, repo, previous_head, head_sha) if previous_head else None
        plan = review_plan(ctx.policy, v["measured"], findings, response, (delta or {}).get("added_lines"), security_available)
        if plan["scope"] == "delta" and delta is None and previous_head != head_sha:
            # The head the previous round reviewed is not in this branch's history: read the whole change again.
            plan = {**plan, "scope": "full", "security": bool(ctx.policy.security_review and security_available),
                    "security_why": "the previous head is gone; full review"}
        pass_ids = list(range(1, plan["passes"] + 1))
        with_security = plan["security"]
        # A pass that already read this exact diff (named for its base and head) is reused, not paid for again: a round
        # that runs again after a crash keeps the reviews it had.
        review_bindings = {i: review_evidence_prefix(ctx, sid, round_no, f"p{i}", base_sha, head_sha) for i in pass_ids}
        security_prefix, saved_security = review_evidence_prefix(ctx, sid, round_no, "sec", base_sha, head_sha)
        reused = {i: binding[1] for i, binding in review_bindings.items()}
        reused_sec = saved_security if with_security else None
        to_run = [i for i in pass_ids if reused.get(i) is None]
        run_sec = with_security and reused_sec is None
        if to_run or run_sec:
            enter_item_stage(ctx, wid, sid, WorkItemStage.REVIEWING, review_round=round_no, review_plan=plan)
            # every session of the round, before the first one starts: the general passes and the security pass together
            if to_run:
                reserve_budget(ctx, "review", len(to_run), extra={"security_review": 1} if run_sec else None)
            else:
                reserve_budget(ctx, "security_review", 1)
        spec_uploads = self.spec_uploads()
        journey_uploads = {f"journeys/{name}.json": content for name, content in sorted(journeys.items())}
        rev_uploads = {**spec_uploads, "diff.patch": diff["patch"].encode(), **visual_uploads, **journey_uploads,
                       "implementation_report.json": json.dumps(report, indent=2).encode(),
                       "preview.json": json.dumps(preview, indent=2).encode(),
                       "deterministic_checks.json": json.dumps([d.to_dict() for d in det], indent=2).encode()}
        rev_preview_clause = (MANAGED_PREVIEW_REVIEW_CLAUSE if preview.get("mode") == "managed" else
                              PREVIEW_CLAUSE if preview["status"] == "ready" and preview.get("mode") != "local" else
                              LOCAL_PREVIEW_REVIEW_CLAUSE if preview.get("mode") == "local" else NO_PREVIEW_CLAUSE)
        if journeys:
            rev_preview_clause += JOURNEY_REVIEW_CLAUSE
        second = ""
        if findings and response:
            rev_uploads["previous_findings.json"] = json.dumps(findings, indent=2).encode()
            rev_uploads["review_response.json"] = json.dumps(response, indent=2).encode()
            if plan["scope"] == "delta" and delta is not None and delta["patch"].strip():
                rev_uploads["delta.patch"] = delta["patch"].encode()
                second = DELTA_REVIEW_CLAUSE.format(previous_head=previous_head, head_sha=head_sha)
            elif plan["scope"] == "delta":
                second = NO_NEW_COMMITS_CLAUSE
            else:
                second = (" Judge each previous finding against your refutation condition using review_response.json "
                          "(upheld/withdrawn), then review the new hunks.")

        def one_pass(i: int) -> dict[str, Any]:
            if reused.get(i) is not None:
                return tag_pass(reused[i], str(i))
            r = run_session(
                ctx, prefix=review_bindings[i][0], spec_id=sid,
                agent_key="reviewer", title=f"{ctx.run_id} review {sid} r{round_no} p{i}", run_id=ctx.run_id,
                stage="review", uploads=rev_uploads, work_item_id=wid,
                description=REVIEW_DESCRIPTION.format(spec_id=sid, base_sha=base_sha, head_sha=head_sha, branch=branch,
                                                      git=self.git_ro, round=round_no, second_round=second, skill_clause=self.skill_clause,
                                                      preview_clause=rev_preview_clause),
                rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["review"],
                metadata={"pass": str(i)}, repository=self.mount(branch),
            )
            f = require_json(r, "findings.json")
            stamp_provenance(f, ctx, r, "Adversarial reviewer", agent_model("reviewer"))
            r.outputs["findings.json"] = json.dumps(f, indent=2).encode()
            persist_outputs(ctx, "review", r, prefix=review_bindings[i][0], work_item_id=wid, spec_id=sid)
            return tag_pass(f, str(i))

        def security_pass() -> dict[str, Any] | None:
            """One security reviewer beside the general passes; skipped when the policy or the deployment lacks it."""
            if not with_security:
                return None
            if reused_sec is not None:
                return tag_pass(reused_sec, "security")
            pre = managed_prechecks(diff["added_lines"]) if managed else security.prechecks(repo, diff["changed"], diff["added_lines"])
            ctx.store.put_artifact(ctx.run_id, "review", security_prefix + "security_prechecks.json", json.dumps(pre, indent=2).encode())
            security_skill = ctx.cfg.skills_dir / "security-review" / "SKILL.md"
            if not security_skill.is_file():
                raise RuntimeError("security reviewer requires skills/security-review/SKILL.md")
            r = run_session(
                ctx, prefix=security_prefix, spec_id=sid,
                agent_key="security_reviewer", title=f"{ctx.run_id} security-review {sid} r{round_no}", run_id=ctx.run_id,
                stage="review", uploads={**rev_uploads, "security_prechecks.json": json.dumps(pre, indent=2).encode(),
                         "skills/security-review/SKILL.md": security_skill.read_bytes()}, work_item_id=wid,
                description=SECURITY_REVIEW_DESCRIPTION.format(spec_id=sid, base_sha=base_sha, head_sha=head_sha, branch=branch,
                                                               git=self.git_ro, round=round_no, second_round=second, skill_clause=self.skill_clause,
                                                               preview_clause=rev_preview_clause),
                rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["security_review"],
                metadata={"pass": "security"}, repository=self.mount(branch),
            )
            f = require_json(r, "findings.json")
            stamp_provenance(f, ctx, r, "Security reviewer", agent_model("security_reviewer"))
            r.outputs["findings.json"] = json.dumps(f, indent=2).encode()
            persist_outputs(ctx, "review", r, prefix=security_prefix, work_item_id=wid, spec_id=sid)
            return tag_pass(f, "security")

        with ThreadPoolExecutor(max_workers=ctx.policy.reviewer_passes + 1) as pool:
            sec_future = pool.submit(steps.carry(security_pass))
            passes = list(pool.map(steps.carry(one_pass), pass_ids))
            sec = sec_future.result()
        merged = merge_findings(passes + ([sec] if sec else []), sid, round_no, base_sha, head_sha, branch,
                                previous=findings, response=response)
        # A failed deterministic check is a finding the implementer must answer; nobody else can clear it.
        merged["findings"] = apply_deterministic(merged["findings"], deterministic_findings(det, v["cmd_results"], v["e2e_results"], managed=managed))
        ctx.store.put_derived_artifact(ctx.run_id, "review", f"{sid}-r{round_no}-findings.merged.json",
                                       json.dumps(merged, indent=2).encode())
        record_verification_findings(ctx, sid, round_no, merged["findings"])
        ctx.event("review.findings", {"round": round_no, "count": len(merged["findings"]),
                                      "blocking": sum(f["severity"] == "blocking" for f in merged["findings"]),
                                      "passes": len(pass_ids), "security_pass": with_security, "scope": plan["scope"]},
                  work_item_id=wid, spec_id=sid, actor="agent")
        return merged

    # ---------------------------------------------------------------- the merge judge

    def judge(self, round_no: int, final: dict[str, Any], recheck: bool) -> dict[str, Any]:
        """The independent merge judge on the final round. A recheck (the owner's build-recovery approve) verifies that
        head again first: the judge never sees evidence older than the decision to look again, and an earlier verdict
        never stands in for it."""
        ctx, sid, wid = self.ctx, self.sid, self.wid
        det = [CheckResult.from_dict(d) for d in final["det"]]
        test_results = _json_of(ctx, final["test_results"])
        patch = ctx.store.get_blob(ctx.run_id, final["patch"])
        visual_uploads = _files(ctx, final["visual"])
        head_sha = final["head_sha"]
        journeys = _files(ctx, final["journeys"])
        if recheck:
            v = self.verify(round_no, _json_of(ctx, final["report"]), journeys, check_reported_head=True)
            det, test_results, head_sha, visual_uploads = v["det"], v["test_results"], v["head_sha"], v["visual_uploads"]
            patch = v["diff"]["patch"].encode()
        findings, response = _json_of(ctx, final["findings"]), _json_of(ctx, final["response"])
        mj_uploads = {
            "spec.json": json.dumps(public(self.spec), indent=2).encode(), "diff.patch": patch, **visual_uploads,
            **{f"journeys/{name}.json": content for name, content in sorted(journeys.items())},
            **({"spec.md": self.spec_md.encode()} if self.spec_md else {}),
            "findings.json": json.dumps(findings or {"findings": []}, indent=2).encode(),
            "review_response.json": json.dumps(response or {"responses": []}, indent=2).encode(),
            "test_results.json": json.dumps(test_results, indent=2).encode(),
            "deterministic_checks.json": json.dumps([d.to_dict() for d in det], indent=2).encode(),
            **self.skill_uploads,
        }
        input_hashes = {name: hashlib.sha256(content).hexdigest() for name, content in sorted(mj_uploads.items())}

        def name_this_judgement() -> str:
            # A new judge output is immutable, even when it revisits the same implementation round: the next free name,
            # chosen once (a step of its own), so a judgement replayed after a crash keeps the name its verdict was saved
            # under instead of looking like an earlier attempt.
            folder = ctx.cfg.runs_dir / ctx.run_id / "artifacts" / "merge_judge"
            original_prefix = f"{sid}-r{round_no}-"
            attempts = sorted(folder.glob(original_prefix + "*merge_readiness.json"))
            return original_prefix if not attempts else original_prefix + f"recheck-{len(attempts):04d}-"
        judge_prefix = steps.run_step(ctx, f"{steps.scope()}/name", {"round": round_no}, name_this_judgement)
        enter_item_stage(ctx, wid, sid, WorkItemStage.MERGE_JUDGING)
        session = dict(
            prefix=judge_prefix, spec_id=sid,
            agent_key="merge_judge", title=f"{ctx.run_id} merge-judge {sid}", run_id=ctx.run_id,
            uploads=mj_uploads, work_item_id=wid,
            description=MERGE_JUDGE_DESCRIPTION.format(spec_id=sid, round=round_no, git=self.git_ro, skill_clause=self.skill_clause,
                                                       criteria_clause=MANAGED_CRITERIA_CLAUSE if ctx.cfg.preview_mode == "managed" else
                                                       LOCAL_CRITERIA_CLAUSE if ctx.cfg.preview_mode == "local" else CRITERIA_CLAUSE),
            rubric_md=None, max_iterations=1, budget_usd=ctx.policy.stage_budget_usd["merge_judge"],
            repository=self.mount(self.branch),
        )
        if not would_reuse(ctx, stage="merge_judge", **session):
            reserve_budget(ctx, "merge_judge")
        res = run_session(ctx, stage="merge_judge", **session)
        verdict = require_json(res, "merge_readiness.json")
        verdict["round"] = round_no
        # Deterministic checks are not negotiable: a failing check forces another_round or escalate.
        if any(not d.passed for d in det) and verdict["result"] == "ready":
            verdict["result"] = "another_round"
            verdict["summary_for_human"] = "Overridden: deterministic checks failed. " + verdict["summary_for_human"]
        verdict["deterministic_checks"] = [d.to_dict() for d in det]
        stamp_provenance(verdict, ctx, res, "Merge judge", agent_model("merge_judge"))
        verdict["_input_hashes"] = input_hashes
        verdict["_head_sha"], verdict["_branch"], verdict["_base_sha"] = head_sha, self.branch, self.base_sha
        res.outputs["merge_readiness.json"] = json.dumps(verdict, indent=2).encode()
        persist_outputs(ctx, "merge_judge", res, prefix=judge_prefix, work_item_id=wid, spec_id=sid)
        ctx.event("merge.verdict", {"result": verdict["result"], "round": round_no}, work_item_id=wid, spec_id=sid, actor="agent")
        return verdict


def adopt_rounds(ctx: RunContext, spec: dict[str, Any], corrective: dict[str, Any] | None = None) -> None:
    """Record the rounds an older controller left as artefacts (no round steps in the run's database), so the loop
    continues where they stop instead of running the implementer again (ADR-0013). The latest round with an implementer
    report keeps that report (its implementer is not run again; its verification and review are); every earlier round
    with merged findings is recorded as answered. A latest round whose report lacks the response its findings required
    runs its implementer again. Nothing is recorded when the run has round steps, or no report."""
    sid = spec["spec_id"]
    if any(s.key.startswith(f"implement/{sid}/r") for s in ctx.store.steps(ctx.run_id)):
        return
    folder = ctx.cfg.runs_dir / ctx.run_id / "artifacts" / "implement"
    rounds = sorted(int(m.group(1)) for p in folder.glob(f"{sid}-r*-implementation_report.json")
                    if (m := re.fullmatch(re.escape(sid) + r"-r(\d+)-implementation_report\.json", p.name)))
    if not rounds:
        return
    n = rounds[-1]

    def findings_of(k: int) -> dict[str, Any] | None:
        found = artifact_json_or_none(ctx, "review", f"{sid}-r{k}-findings.merged.json")
        if found is None and corrective is not None and int(corrective.get("round", 0)) == k:
            found = corrective.get("review_findings") or {"findings": []}
        return found
    if n > 1 and findings_of(n - 1) is None:
        raise RuntimeError(f"cannot continue {sid}: round {n} has a report but round {n - 1} has no merged findings")
    journeys = _blobs(ctx, latest_journeys(ctx, sid, spec))
    carried: dict[str, Any] = {"findings": None, "response": None, "det_only": False, "journeys": journeys}
    for k in range(1, n + 1):
        report = artifact_json_or_none(ctx, "implement", f"{sid}-r{k}-implementation_report.json")
        response = artifact_json_or_none(ctx, "implement", f"{sid}-r{k}-review_response.json")
        last = k == n and not (corrective is not None and int(corrective.get("round", 0)) == n)
        if last:
            if report is not None and not (carried["findings"] and response is None):
                steps.record_step(ctx, f"implement/{sid}/r{k}/author",
                                  {"report": steps.put_json(ctx, report), "journeys": journeys,
                                   "response": steps.put_json(ctx, response) if response is not None else None})
            return
        findings = findings_of(k)
        det = artifact_json_or_none(ctx, "implement", f"{sid}-r{k}-deterministic_checks.json") or []
        test_results = artifact_json_or_none(ctx, "implement", f"{sid}-r{k}-test_results.json") or {}
        standing = standing_findings((findings or {}).get("findings", []))
        steps.record_step(ctx, f"implement/{sid}/r{k}", {
            "report": steps.put_json(ctx, report or {}), "response": steps.put_json(ctx, response) if response is not None else None,
            "journeys": journeys, "head_sha": ((findings or {}).get("diff_ref") or {}).get("head_sha"), "base_sha": None,
            "det": det, "test_results": steps.put_json(ctx, test_results), "patch": ctx.store.put_blob(ctx.run_id, b""),
            "visual": {}, "preview": test_results.get("preview") or {}, "findings": steps.put_json(ctx, findings or {"findings": []}),
            "next": "judge" if k == n else "respond", "det_only": _deterministic_only(standing)})
        carried = {"findings": findings or {"findings": []}, "response": response, "det_only": _deterministic_only(standing),
                   "journeys": journeys}


def latest_journeys(ctx: RunContext, sid: str, spec: dict[str, Any]) -> dict[str, bytes]:
    """Newest journey outputs named in the spec, including provider listings that flatten `journeys/<name>.json`
    to `<name>.json`. A later round may rewrite one journey and leave the others; unrelated reports never qualify."""
    root = ctx.cfg.runs_dir / ctx.run_id / "artifacts" / "implement"
    names = {name for entry in verification.plan_of(spec).e2e if (name := journey_name(entry["command"]))}
    found: dict[str, tuple[int, bytes]] = {}
    for name in sorted(names):
        pattern = re.compile(re.escape(sid) + r"-r(\d+)-(?:journeys/)?" + re.escape(name) + r"\.json")
        paths = [*root.glob(f"{sid}-r*-{name}.json"), *root.glob(f"{sid}-r*-journeys/{name}.json")]
        matches = [(int(m.group(1)), path) for path in paths if (m := pattern.fullmatch(path.relative_to(root).as_posix()))]
        if not matches:
            continue
        latest = max(round_no for round_no, _ in matches)
        contents = {path.read_bytes() for round_no, path in matches if round_no == latest}
        if len(contents) != 1:
            raise ValueError(f"conflicting journey outputs for {name} in {sid} round {latest}")
        found[name] = (latest, contents.pop())
    return {name: content for name, (_, content) in sorted(found.items())}


def record_verification_findings(ctx: RunContext, sid: str, round_no: int, findings: list[dict[str, Any]]) -> None:
    """Findings about the journey's own plumbing (`target: verification`) never block the change; keep them in the run
    and append them to the factory's backlog (runs/_backlog/verification-findings.jsonl) for the runner's owners."""
    mine = [f for f in findings if f.get("target") == "verification" and f.get("status") in ("open", "upheld")]
    if not mine:
        return
    ctx.store.put_artifact(ctx.run_id, "review", f"{sid}-r{round_no}-verification_findings.json", json.dumps(mine, indent=2).encode(),
                           replace=True)
    backlog = ctx.cfg.runs_dir / "_backlog"
    backlog.mkdir(parents=True, exist_ok=True)
    with (backlog / "verification-findings.jsonl").open("a") as f:
        for finding in mine:
            f.write(json.dumps({"run_id": ctx.run_id, "spec_id": sid, "round": round_no, "finding": finding}) + "\n")
    ctx.event("verification.backlog", {"round": round_no, "count": len(mine), "finding_ids": [f["finding_id"] for f in mine]}, spec_id=sid)


def _require_recorded_preview_mode(ctx: RunContext) -> None:
    """A run started in managed mode never continues with controller-side execution: a resume without the managed
    configuration would otherwise build a local environment and run the repository on this host."""
    from .common import hosted_run
    if hosted_run(ctx.store.events(ctx.run_id)) and ctx.cfg.preview_mode != "managed":
        raise RuntimeError("this run verifies candidates in a hosted sandbox (preview mode managed); continue it with "
                           "FACTORY_PREVIEW=managed and the same app binding, never with local or platform execution")


def _deterministic_only(findings: list[dict[str, Any]]) -> bool:
    """True when `findings` is non-empty and every entry is one of the orchestrator's own DET-* checks."""
    return bool(findings) and all(str(f.get("finding_id", "")).startswith("DET-") for f in findings)


ENVIRONMENTAL_MARKERS = ("address already in use", "eaddrinuse")


def is_environmental(results: list[dict[str, Any]]) -> bool:
    """A failed command whose output blames the verification host (a port already bound) rather than the change."""
    for r in results:
        if r.get("exit_code", 1) in (0, None):
            continue
        text = ((r.get("stderr_tail") or "") + (r.get("stdout_tail") or "")).lower()
        if any(m in text for m in ENVIRONMENTAL_MARKERS):
            return True
    return False


def managed_prechecks(added_lines_by_file: dict[str, list[str]]) -> dict[str, Any]:
    """Security pre-checks without executing anything on this host: the secret scan reads the diff text; the dependency
    audit and SAST would run repository tooling, which managed mode only does in a hosted sandbox."""
    reason = "not run on the orchestrator host in managed preview mode; the security reviewer audits dependencies and code in its own sandbox"
    return {"secret_scan": security.secret_scan(added_lines_by_file),
            "dependency_audit": {"tool": None, "status": "skipped", "reason": reason},
            "sast": {"tool": None, "status": "skipped", "reason": reason}}


def deterministic_findings(det: list[Any], command_results: list[dict[str, Any]] | None = None,
                           e2e_results: list[dict[str, Any]] | None = None, *, managed: bool = False) -> list[dict[str, Any]]:
    """Findings (review_findings schema shape) for every failed deterministic check, so the implementer sees
    exactly what blocks the merge and can fix or refute it like any other finding.

    A failed `verification_setup` (the per-run environment could not be built) is the one finding for that round:
    the command and e2e checks it kept from running are consequences, not separate defects. A command or e2e
    failure whose output says the port was already in use is tagged `environmental`: the implementer still
    answers it (the usual fix is a command that does not assume host state), and the judge weighs it as such."""
    failed = [d for d in det if not d.passed]
    setup_failed = any(d.check == SETUP_CHECK for d in failed)
    out = []
    for d in failed:
        name = d.check.split(":")[0]
        if setup_failed and (name in ("test_plan_commands", "e2e_local", "e2e_preview", "ac_test_mapping") or (managed and name in (ENVIRONMENT_CHECK, "visual_e2e"))):
            continue
        repro, environmental = "", False
        if name == "test_plan_commands":
            rows = [r for r in (command_results or []) if r.get("exit_code", 1) != 0]
            repro = "; ".join(f"{r['command']} -> exit {r.get('exit_code')}: {((r.get('diagnostic') or {}).get('summary') or r.get('stderr_tail') or r.get('stdout_tail') or '')[-300:]}" for r in rows)
            environmental = is_environmental(rows)
        elif name in ("e2e_local", "e2e_preview"):
            target = name.removeprefix("e2e_")
            rows = [r for r in (e2e_results or []) if r.get("target", "preview") == target and not checks.row_passed(r)]
            repro = "; ".join(f"{r['command']} -> exit {r.get('exit_code')}: {((r.get('diagnostic') or {}).get('summary') or r.get('stderr_tail') or r.get('stdout_tail') or '')[-300:]}" for r in rows)
            environmental = is_environmental(rows)
        fix = {"ac_test_mapping": "Map every automated acceptance criterion to a test in implementation_report.json `criteria` ([{ac_id, tests: [...]}]) using pytest node ids exactly as `pytest --collect-only -q` prints them, or Bun ids `path/x.test.ts::outer describe > test name`; the orchestrator only counts tests its own run of the test plan collected or reported and saw pass (Bun only from direct `bun test` commands).",
               "test_plan_commands": "Make every test_plan command exit 0 from a clean checkout; if a command depends on a port or host, make it robust or pick a free port.",
               "protected_tests": ("Existing tests are protected. An approved spec may name an existing test file by its exact path in "
                                   "allowed_paths; that file may then be updated to test the specified behavior. "
                                   "Globs do not authorize existing test edits. Never delete or rename an existing test file, "
                                   "skip a test, or weaken assertions to make the change pass."),
               SETUP_CHECK: ("The hosted verification could not run on this head; the detail says why. A failed recipe prepare phase is "
                             "fixed in the change. A change to the recipe's trusted part (network, secrets, data, command_environment), a missing "
                             "recipe, or test credentials that are not attached needs a person to re-approve the app's recipe: do not "
                             "edit those to get past it.") if managed else
                            "Make the repository installable into a fresh virtual environment: a pyproject.toml whose dependencies resolve (runtime plus a dev/test extra for the test plan), or a `runtime.setup` command in the service manifest that installs what the tests and the entrypoint need.",
               ENVIRONMENT_CHECK: "The app did not start, its services did not answer their health paths, or the recipe's baseline check failed in the hosted sandbox (hosted_verification.json has the phases). Make the change keep the app starting and the baseline journey working; the recipe's commands and services may change with it, its trusted part may not."}.get(name, "Fix the cause named in the detail.")
        if environmental:
            fix = ("The failure reads as environmental (a port already in use on the verification host), not as a defect in the "
                   "change: make the command bind a free port or reuse $BASE_URL instead of a fixed port, then push; the "
                   "orchestrator re-runs the check. ") + fix
        out.append({
            "finding_id": f"DET-{name}", "severity": "blocking",
            "category": "security" if name == "secret_scan" else ("test_quality" if name in ("ac_test_mapping", "test_plan_commands", "test_weakening", "protected_tests", "e2e_coverage", "e2e_local", "e2e_preview", SETUP_CHECK, ENVIRONMENT_CHECK) else "spec_deviation"),
            "claim": f"Deterministic check `{name}` failed: {d.detail}",
            "evidence": {"locations": ["deterministic_checks.json:1"], **({"reproduction": repro} if repro else {}), "observed": d.detail},
            "violates": [f"orchestrator check {name} (cannot be overridden by any judge)"],
            "refutation_condition": f"`{name}` passes on the orchestrator's next verification of the pushed head",
            "status": "open",
            "suggested_fix": fix,
            **({"environmental": True} if environmental else {}),
        })
    return out


def merge_findings(passes: list[dict[str, Any]], spec_id: str, round_no: int, base_sha: str, head_sha: str,
                   branch: str, previous: dict[str, Any] | None = None, response: dict[str, Any] | None = None) -> dict[str, Any]:
    """Union of independent reviewer passes, with ids that survive rounds (post-mortem issue 14).

    Round 1: findings are de-duplicated by (category, first location), the strictest severity wins, and they are
    numbered F-1.. in severity order. A later round gets `previous` (the merged findings the implementer answered)
    and `response` (its review_response.json): a finding a reviewer re-adjudicated keeps its previous id (matched on
    `previous_finding_id`, on its own `finding_id` when the category matches, or on (category, location)), the
    stricter re-evaluation wins across passes (upheld over withdrawn, then the higher severity), every previous
    finding no pass mentioned (the orchestrator's DET-* included) is carried with the implementer's disposition as
    its status, and only new findings are numbered, from the highest previous number on. Each finding and each
    checks_performed row carries the pass that produced it; scope_check is the union of the passes'."""
    prev_findings = (previous or {}).get("findings", [])
    prev_by_id = {f["finding_id"]: f for f in prev_findings}
    prev_by_key = {_loc_key(f): f["finding_id"] for f in reversed(prev_findings)}
    disposition = {r.get("finding_id"): r.get("disposition") for r in (response or {}).get("responses", [])}
    carried: dict[str, dict[str, Any]] = {}
    fresh: dict[tuple[str, str, str], dict[str, Any]] = {}
    checks_performed: list[dict[str, Any]] = []
    conformance: dict[str, dict[str, Any]] = {}
    scope: dict[str, set[str]] = {"files_outside_allowed_paths": set(), "unrelated_changes": set()}
    for p in passes:
        checks_performed.extend(p.get("checks_performed", []))
        for f in p.get("findings", []):
            pid = _previous_id(f, prev_by_id, prev_by_key)
            if pid is not None:
                f = {**f, "finding_id": pid, "previous_finding_id": pid}
                if pid not in carried or _reevaluation_rank(f) > _reevaluation_rank(carried[pid]):
                    carried[pid] = f
                continue
            key = (*_loc_key(f), f.get("target", "change"))      # a verification finding never hides a change finding
            if key not in fresh or _sev(f) > _sev(fresh[key]):
                fresh[key] = f
        for row in p.get("spec_conformance", []):
            prev = conformance.get(row["ac_id"])
            # The stricter status wins across passes.
            if prev is None or _status_rank(row["status"]) > _status_rank(prev["status"]):
                conformance[row["ac_id"]] = row
        for k in scope:
            scope[k] |= set((p.get("scope_check") or {}).get(k, []))
    findings: list[dict[str, Any]] = []
    for fid, prev in prev_by_id.items():                      # previous order first, ids intact
        if fid in carried:
            findings.append(carried[fid])
        else:
            answered = disposition.get(fid)
            findings.append({**prev, "status": answered if answered in ("fixed", "refuted", "accepted_risk") else prev["status"]})
    next_no = max([int(fid[2:]) for fid in prev_by_id if re.fullmatch(r"F-[0-9]+", fid)] + [0]) + 1
    for i, f in enumerate(sorted(fresh.values(), key=_sev, reverse=True), next_no):
        f["finding_id"] = f"F-{i}"
        findings.append(f)
    return {
        "review_id": f"rev_{spec_id.replace('spec_', '')}{round_no:02d}{head_sha[:6]}",
        "spec_id": spec_id, "round": round_no,
        "diff_ref": {"base_sha": base_sha, "head_sha": head_sha, "branch": branch},
        "checks_performed": checks_performed, "findings": findings,
        "spec_conformance": list(conformance.values()),
        "scope_check": {k: sorted(v) for k, v in scope.items()},
        "produced_by": passes[0].get("produced_by", {}),
    }


def apply_deterministic(findings: list[dict[str, Any]], det_findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold this round's failed deterministic checks into the merged findings: a DET-* id that is failing again
    replaces its carried entry (open and blocking once more), a carried DET-* whose check now passes is `fixed`
    unless the implementer's disposition already closed it, and new failures are appended."""
    failing = {f["finding_id"]: f for f in det_findings}
    out = []
    for f in findings:
        fid = f["finding_id"]
        if fid in failing:
            out.append(failing.pop(fid))
        elif fid.startswith("DET-") and f.get("status") in ("open", "upheld"):
            out.append({**f, "status": "fixed"})
        else:
            out.append(f)
    return out + list(failing.values())


def _loc_key(f: dict[str, Any]) -> tuple[str, str]:
    loc = (f.get("evidence") or {}).get("locations") or [""]
    return (f.get("category", ""), loc[0].split(":")[0] + ":" + loc[0].split(":")[-1][:3])


def _previous_id(f: dict[str, Any], prev_by_id: dict[str, dict[str, Any]], prev_by_key: dict[tuple[str, str], str]) -> str | None:
    pid = f.get("previous_finding_id")
    if pid in prev_by_id:
        return pid
    own = f.get("finding_id")
    if own in prev_by_id and prev_by_id[own].get("category") == f.get("category"):
        return own
    return prev_by_key.get(_loc_key(f))


def _reevaluation_rank(f: dict[str, Any]) -> tuple[int, bool, int]:
    return ({"upheld": 3, "open": 2, "fixed": 1, "refuted": 1, "accepted_risk": 1, "withdrawn": 0}.get(f.get("status", "open"), 2),
            f.get("target") != "verification", _sev(f))


def tag_pass(findings: dict[str, Any], label: str) -> dict[str, Any]:
    """Mark every finding and every checks_performed row of a reviewer pass with the pass that produced it (kept
    through merging, so two passes' probe counts are never read as one)."""
    for f in findings.get("findings", []):
        f.setdefault("pass", label)
    for row in findings.get("checks_performed", []):
        row.setdefault("pass", label)
    return findings


def standing_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings the implementer must answer before the merge judge: open or upheld, and either blocking/major or a
    reproduced security, concurrency or data-integrity defect whatever label the reviewer put on it (reviewers
    under-rate rare races; see docs/postmortems/2026-10-10-first-live-runs.md issue 8). A finding about the journey's own
    plumbing (`target: verification`) never stands: it is recorded for the factory (post-mortem 2026-10-11 §1)."""
    out = []
    for f in findings:
        if f["status"] not in ("open", "upheld") or f.get("target") == "verification":
            continue
        reproduced = bool((f.get("evidence") or {}).get("reproduction"))
        if f["severity"] in ("blocking", "major") or (f["category"] in ("security", "concurrency", "data_integrity") and reproduced):
            out.append(f)
    return out


def review_plan(policy: Any, size_measured: dict[str, Any] | None, previous: dict[str, Any] | None,
                response: dict[str, Any] | None, delta_added_lines: dict[str, list[str]] | None,
                security_available: bool) -> dict[str, Any]:
    """The shape of one review round: how many general passes, whether the security pass runs, and what they read.

    A first round reads the whole diff: one general pass when the change is at most `review_single_pass_lines`
    hand-written lines, `reviewer_passes` above that. A later round (the implementer answered `previous`) reads the delta
    since the head the previous round reviewed, with `reviewer_passes_followup` passes that also adjudicate every
    previous finding; the security pass comes back only when one of its findings still stands or the delta's secret scan
    flags a line. Unadjudicated findings keep the implementer's disposition (`merge_findings`)."""
    security_on = bool(security_available and getattr(policy, "security_review", True))
    previous_head = ((previous or {}).get("diff_ref") or {}).get("head_sha") if previous and response else None
    if previous_head:
        standing_security = [f["finding_id"] for f in previous.get("findings", [])
                             if f.get("pass") == "security" and f.get("status") in ("open", "upheld")]
        scan = security.secret_scan(delta_added_lines or {})
        flagged = bool(scan["hits"] or scan["suspects"])
        why = ("previous security findings stand: " + ", ".join(standing_security) if standing_security
               else "the delta's secret scan flagged a line" if flagged else "no security finding stands and the delta is clean")
        return {"scope": "delta", "previous_head": previous_head,
                "passes": max(1, int(getattr(policy, "reviewer_passes_followup", 1))),
                "security": security_on and bool(standing_security or flagged), "security_why": why}
    hand = (size_measured or {}).get("hand_written")
    small = hand is not None and hand <= int(getattr(policy, "review_single_pass_lines", 0) or 0)
    return {"scope": "full", "previous_head": None, "hand_written_lines": hand,
            "passes": 1 if small else max(1, int(policy.reviewer_passes)),
            "security": security_on, "security_why": "first round" if security_on else "off or not deployed"}


def delta_since(verifier: LocalVerifier, repo: Path, previous_head: str, head_sha: str) -> dict[str, Any] | None:
    """What changed between the head the previous round reviewed and this one, or None when git cannot say (the previous
    head is gone from the branch, which a review of the whole diff then covers)."""
    if previous_head == head_sha:
        return None
    try:
        return verifier.analyse(repo, previous_head, head_sha)
    except subprocess.CalledProcessError:
        return None


def _sev(f: dict[str, Any]) -> int:
    return {"nit": 0, "minor": 1, "major": 2, "blocking": 3}[f["severity"]]


def _status_rank(s: str) -> int:
    return {"met": 0, "not_assessed": 1, "untestable_as_written": 2, "not_met": 3}[s]
