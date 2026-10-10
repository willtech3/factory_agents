"""The forge: where the code lives and where change requests are reviewed.

A *change request* (CR) is a GitHub pull request or a GitLab merge request. The orchestrator
only ever needs the small slice of forge behaviour listed on the `Forge` protocol: branches and
commits for the design CR and the `.factory/runs/<run_id>/` record, change requests for Gate 3,
comments and labels for feedback, and read-only pipeline/deployment lookups for preview and dev
URLs. Everything forge-specific lives in `forge/github.py` and `forge/gitlab.py`.

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

    # ---- branches and commits (design CR, run record)
    def create_branch(self, branch: str, ref: str) -> None: ...
    def commit_files(self, branch: str, message: str, files: dict[str, bytes]) -> str: ...
    def file_exists(self, branch: str, path: str) -> bool: ...
    def file_url(self, branch: str, path: str) -> str: ...

    # ---- change requests (Gate 3, and the design CR when the gate surface is the forge)
    def create_change_request(self, *, source_branch: str, target_branch: str, title: str, description: str,
                              labels: list[str], reviewers: list[str], draft: bool = False) -> ChangeRequest: ...
    def change_request_state(self, cr_id: int) -> dict[str, Any]: ...
    def change_request_approved(self, cr_id: int) -> bool: ...
    def change_request_comments(self, cr_id: int) -> list[dict[str, Any]]: ...
    def change_request_discussions(self, cr_id: int) -> list[dict[str, Any]]: ...
    def add_labels(self, cr_id: int, labels: list[str]) -> None: ...
    def post_comment(self, cr_id: int, body: str) -> None: ...

    # ---- pipelines / workflows and deployments (read-only; Stage 3 preview, Stage 5 dev)
    def latest_pipeline(self, ref: str, sha: str | None = None) -> dict[str, Any] | None: ...
    def pipeline(self, pipeline_id: int) -> dict[str, Any]: ...
    def failed_job_log_excerpt(self, pipeline_id: int, max_chars: int = 6000) -> str: ...
    def environment(self, name: str) -> dict[str, Any] | None: ...
    def environment_url_for_branch(self, branch: str, preview_prefix: str = "preview/") -> str | None: ...
    def deployment_for_sha(self, environment_name: str, sha: str) -> dict[str, Any] | None: ...

    # ---- sessions: how a sandbox gets the repository
    def session_repository_resource(self, repo_url: str, branch: str) -> dict[str, Any] | None: ...
    def sandbox_git_instructions(self, repo_url: str, base_branch: str, branch: str) -> str: ...
    def authenticated_url(self, repo_url: str) -> str: ...   # for the orchestrator's own clone (LocalVerifier)

    # ---- feedback rendering
    def render_feedback_md(self, comments: list[dict[str, Any]], discussions: list[dict[str, Any]]) -> str: ...


def render_feedback_md(comments: list[dict[str, Any]], discussions: list[dict[str, Any]],
                       title: str = "Human feedback from the design change request") -> str:
    """Turn change-request comments into the feedback.md an agent reads on a re-run.

    `discussions` are inline comments with `path`/`line`; `comments` are top-level. Both carry
    `author` and `body`. Shared by every forge so the agent sees one shape.
    """
    lines = [f"# {title}", ""]
    for d in discussions:
        where = f" (`{d['path']}:{d['line']}`)" if d.get("path") else ""
        lines.append(f"- **@{d['author']}**{where}: {d['body'].strip()}")
    for n in comments:
        if n.get("position"):
            continue  # already covered as a discussion
        lines.append(f"- **@{n['author']}**: {n['body'].strip()}")
    lines += ["", "Address every item explicitly in your summary: either apply it, or explain why not."]
    return "\n".join(lines)


def parse_pipeline_webhook(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise a forge CI webhook to the shape `Forge.pipeline()` returns, or None for other hooks.

    GitLab Pipeline Hooks carry `object_kind: pipeline`; GitHub `workflow_run` events carry a
    `workflow_run` object. Both are unverified against live payloads (see the roadmap spikes).
    """
    from .github import parse_workflow_run_webhook
    from .gitlab import parse_pipeline_webhook as parse_gitlab

    if payload.get("object_kind") == "pipeline":
        return parse_gitlab(payload)
    if "workflow_run" in payload:
        return parse_workflow_run_webhook(payload)
    return None


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


def ensure_repository(forge: Forge, repo: dict[str, Any], workflow_template: Path | None = None, *,
                      sleep=None, attempts: int = 6) -> dict[str, Any] | None:
    """Create the outcome's repository when it asks for one (`create: true`) and it does not exist yet, and make
    sure it carries the preview workflow.

    A new repository is private unless the outcome says `visibility: public`, initialised so the default branch
    exists, and seeded with the preview workflow template (the platform's job elsewhere; on a personal GitHub
    the template is the platform). GitHub's git database answers 404 to writes for a few seconds after a
    repository is created, so the seed commit is retried with backoff. A repository that already exists is left
    alone except for that one file: when `create: true` and the workflow is missing (an earlier attempt died
    between creation and seeding), it is added. Returns what was created, or None when it already existed."""
    import time
    sleep = sleep or time.sleep
    if not repo.get("create"):
        return None
    created = None
    if not forge.repository_exists():
        created = forge.create_repository(private=repo.get("visibility", "private") != "public",
                                          description=repo.get("description", "Created by the software factory"))
    branch = (created or {}).get("default_branch") or repo.get("default_branch", "main")
    path = ".github/workflows/preview.yml"
    if workflow_template and workflow_template.is_file() and forge.kind == "github":
        files = {path: workflow_template.read_bytes()}
        delay = 2.0
        for attempt in range(attempts):
            try:
                if created is None and forge.file_exists(branch, path):
                    break                               # existing repository, already seeded
                forge.commit_files(branch, "factory: preview workflow for factory branches", files)
                break
            except Exception as e:  # noqa: BLE001  (httpx status errors; the forge is not ready yet)
                status = getattr(getattr(e, "response", None), "status_code", None)
                if attempt == attempts - 1 or status not in (404, 409, 422, 500, 502, 503):
                    raise
                sleep(delay)
                delay = min(delay * 2, 20.0)
    return created


def ref_slug(ref: str) -> str:
    """Lowercase, [a-z0-9-], max 63 chars, no leading or trailing dash (GitLab's CI_COMMIT_REF_SLUG)."""
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", ref.lower()).strip("-")
    return slug[:63].rstrip("-")


__all__ = ["ChangeRequest", "Forge", "PIPELINE_TERMINAL", "ensure_repository", "make_forge", "parse_pipeline_webhook",
           "ref_slug", "render_feedback_md", "repo_from_url"]
