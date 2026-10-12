"""The forge: where the code lives and where change requests are reviewed.

A *change request* (CR) is a GitHub pull request or a GitLab merge request. The orchestrator
only ever needs the small slice of forge behaviour listed on the `Forge` protocol: commits for seeding a
repository, change requests for Gate 3, labels and comments on them, and read-only pipeline/deployment
lookups for preview and dev URLs. Nothing is committed and no change request is opened before Gate 3
(ADR-0011). Everything forge-specific lives in `forge/github.py` and `forge/gitlab.py`.

Selection is by configuration (`FACTORY_FORGE=github|gitlab`, default github); see `make_forge`.
The sandbox never holds the forge token: GitHub sessions mount the repository with the platform's
native `github_repository` resource (token injected by Anthropic's git proxy), GitLab sessions use
the vault/bearer-header path of ADR-0003. Change requests are always opened host-side by the
orchestrator through the `open_change_request` custom tool (ADR-0008).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config


@dataclass
class Media:
    """One file to show in a change request description: a screenshot or the demo, already validated by the controller."""
    name: str
    content: bytes
    mime_type: str
    alt: str
    role: str                 # "screenshot" | "demo"


@dataclass
class ChangeRequest:
    id: int                   # PR number (GitHub) or MR iid (GitLab)
    url: str
    source_branch: str
    target_branch: str


# Pipeline statuses are normalised to GitLab's vocabulary on every forge:
# created | pending | running | success | failed | canceled | skipped | manual.
PIPELINE_TERMINAL = {"success", "failed", "canceled", "skipped"}


class Forge(Protocol):
    """What the orchestrator needs from a forge. Implementations: GitHubForge, GitLabForge."""

    kind: str                 # "github" | "gitlab"
    repo: str                 # "owner/repo" or "group/project"

    # ---- the repository itself (greenfield outcomes)
    def repository_exists(self) -> bool: ...
    def create_repository(self, private: bool = True, description: str = "") -> dict[str, Any]: ...

    # ---- commits (seeding a repository: `ensure_repository`)
    def commit_files(self, branch: str, message: str, files: dict[str, bytes]) -> str: ...
    def file_exists(self, branch: str, path: str) -> bool: ...

    # ---- change requests (Gate 3)
    def create_change_request(self, *, source_branch: str, target_branch: str, title: str, description: str,
                              labels: list[str], reviewers: list[str], draft: bool = False) -> ChangeRequest: ...
    def change_request_state(self, cr_id: int) -> dict[str, Any]: ...
    def change_request_approved(self, cr_id: int) -> bool: ...
    def add_labels(self, cr_id: int, labels: list[str]) -> None: ...

    # ---- pipelines / workflows and deployments (read-only; Stage 3 preview, Stage 5 dev)
    def latest_pipeline(self, ref: str, sha: str | None = None) -> dict[str, Any] | None: ...
    def pipeline(self, pipeline_id: int) -> dict[str, Any]: ...
    def failed_job_log_excerpt(self, pipeline_id: int, max_chars: int = 6000) -> str: ...
    def environment(self, name: str) -> dict[str, Any] | None: ...
    def environment_url_for_branch(self, branch: str, preview_prefix: str = "preview/") -> str | None: ...

    # ---- sessions: how a sandbox gets the repository
    def session_repository_resource(self, repo_url: str, branch: str) -> dict[str, Any] | None: ...
    def sandbox_git_instructions(self, repo_url: str, base_branch: str, branch: str, read_only: bool = False) -> str: ...
    # read_only: the clause for reviewers, the merge judge and the integrator, who read the branch and never commit or push
    def authenticated_url(self, repo_url: str) -> str: ...   # for the orchestrator's own clone (LocalVerifier)

    # ---- media in a change request description without media in its diff (post-mortem 2026-10-11)
    def publish_media(self, folder: str, items: list[Media]) -> str: ...
    # Markdown that shows `items` in a description: inline images, and the demo inline where the forge plays it (GitLab
    # uploads) or as a link where it does not (a video on GitHub outside its own attachments). Nothing lands on the
    # change's branch or the default branch.


def make_forge(cfg: "Config", timeout: float = 30.0) -> Forge | None:
    """Build the configured forge, or None when no token is configured (local mode)."""
    if not cfg.forge_token or not cfg.forge_repo:
        return None
    if cfg.forge == "github":
        from .github import GitHubForge
        return GitHubForge(cfg.forge_url, cfg.forge_token, cfg.forge_repo, timeout=timeout,
                           workflow=cfg.deploy_workflow)
    if cfg.forge == "gitlab":
        from .gitlab import GitLabForge
        return GitLabForge(cfg.forge_url, cfg.forge_token, cfg.forge_repo, timeout=timeout)
    raise ValueError(f"unknown forge {cfg.forge!r}; set FACTORY_FORGE=github|gitlab")


def repo_from_url(url: str) -> str:
    """`https://github.com/owner/name(.git)` -> `owner/name`; `https://gitlab.example/group/sub/proj` -> `group/sub/proj`."""
    from urllib.parse import urlparse
    path = urlparse(url).path.strip("/")
    return path[:-4] if path.endswith(".git") else path


WORKFLOW_SCOPE_HINT = ("the forge refused to write {path}: on GitHub, writing under .github/workflows/ needs a token with the "
                       "`workflow` scope (for a gh token: `gh auth refresh -s workflow`), and the Git Data API reports the "
                       "refusal as 404. The repository is usable; with FACTORY_PREVIEW=local nothing needs the workflow. "
                       "Add it by hand or re-run with a token that has the scope.")


SKILL_PATH = ".claude/skills/platform-contract/SKILL.md"


def ensure_repository(forge: Forge, repo: dict[str, Any], workflow_template: Path | None = None, *,
                      skill_file: Path | None = None, sleep=None, attempts: int = 4, log=print) -> dict[str, Any] | None:
    """Create the outcome's repository when it asks for one (`create: true`) and it does not exist yet, and seed it
    with the platform-contract skill (`skill_file`) and the preview workflow template.

    Only a repository marked `create: true` is ever written to: an existing repository may require every change
    through a pull request, and every session already receives the skill as an upload. A new repository is private
    unless the outcome says `visibility: public`, initialised so the default branch exists, and seeded with the
    preview workflow (the platform's job elsewhere; on a personal GitHub the template is the platform). GitHub can
    answer 404 to writes for a few seconds after a repository is created, so each seed commit is retried with
    backoff. When a `create: true` repository already exists (an earlier attempt died between creation and
    seeding), whichever of the two files is missing is seeded. A seed the forge keeps refusing is a warning, not a
    failure: the usual cause is a token without GitHub's `workflow` scope, and a run with local previews never needs
    the file. Returns what was created (with `workflow_seeded`), or None when nothing was created."""
    import time
    sleep = sleep or time.sleep
    if not repo.get("create"):
        return None
    created = None
    if not forge.repository_exists():
        created = forge.create_repository(private=repo.get("visibility", "private") != "public",
                                          description=repo.get("description", "Created by the software factory"))
    branch = (created or {}).get("default_branch") or repo.get("default_branch", "main")
    # The skill is seeded first and on its own: the agents read it from the repository's .claude/skills/ and its path
    # needs no special token scope. The workflow follows with its own retries.
    if (skill_file and skill_file.is_file() and forge.kind == "github"
            and (created is not None or not forge.file_exists(branch, SKILL_PATH))):
        _seed(forge, branch, "factory: platform contract skill", {SKILL_PATH: skill_file.read_bytes()},
              sleep=sleep, attempts=attempts, log=log, hint="the forge refused to write {path}")
    path = ".github/workflows/preview.yml"
    if workflow_template and workflow_template.is_file() and forge.kind == "github":
        seeded = True if (created is None and forge.file_exists(branch, path)) else _seed(
            forge, branch, "factory: preview workflow for factory branches", {path: workflow_template.read_bytes()},
            sleep=sleep, attempts=attempts, log=log, hint=WORKFLOW_SCOPE_HINT)
        if created is not None:
            created["workflow_seeded"] = seeded
    return created


def _seed(forge: Forge, branch: str, message: str, files: dict[str, bytes], *, sleep, attempts: int, log, hint: str) -> bool:
    """One commit with `files`, retried with backoff while the forge answers a transient or refusing status.
    Returns whether it landed; a refusal that outlasts the retries is logged with `hint` and swallowed."""
    import time
    sleep = sleep or time.sleep
    delay = 2.0
    for attempt in range(attempts):
        try:
            forge.commit_files(branch, message, files)
            return True
        except Exception as e:  # noqa: BLE001  (httpx status errors)
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status not in (403, 404, 409, 422, 500, 502, 503):
                raise
            if attempt == attempts - 1:
                log("[forge] " + hint.format(path=", ".join(files)) + f" (last response: {status})")
                return False
            sleep(delay)
            delay = min(delay * 2, 20.0)
    return False


def ref_slug(ref: str) -> str:
    """Lowercase, [a-z0-9-], max 63 chars, no leading or trailing dash (GitLab's CI_COMMIT_REF_SLUG)."""
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", ref.lower()).strip("-")
    return slug[:63].rstrip("-")


__all__ = ["ChangeRequest", "Forge", "Media", "PIPELINE_TERMINAL", "ensure_repository", "make_forge",
           "ref_slug", "repo_from_url"]
