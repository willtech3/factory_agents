"""GitHub forge (orchestrator side). The default `Forge` implementation.

REST calls only, through httpx, against api.github.com (or `<FORGE_URL>/api/v3` for GitHub
Enterprise Server). Pull requests, labels, and comments come from the Pulls and Issues APIs;
multi-file commits use the Git Data API (blobs, trees, commits, refs) so one commit carries every
file; pipelines are GitHub Actions workflow runs; preview and dev URLs come from the Deployments
API (`environment_url` on the latest deployment status).

Status: written against the documented REST API (2022-11-28), exercised only through a fake
transport in tests. Everything under "pipelines and deployments" is UNVERIFIED live; field names
are hypotheses until docs/07-personal-e2e-setup.md has been run once. Nothing here deploys.
"""
from __future__ import annotations

import base64
from typing import Any
from urllib.parse import urlparse

import httpx

from . import ChangeRequest, PIPELINE_TERMINAL, ref_slug, render_feedback_md

BOT_MARKER = "<!-- factory:bot -->"   # prefixed to every comment the orchestrator posts, so it can skip its own

GIT_SETUP = """Git access: the repository is mounted at /workspace/repo (branch {base_branch}) through the platform's
github_repository resource; clone and push are authenticated by Anthropic's git proxy and no token is visible in the
sandbox. Work on branch {branch}: `git fetch origin && git checkout {branch}` if it exists on origin, otherwise
`git checkout -b {branch} {base_branch}`. Commit as you go and `git push -u origin {branch}` when done."""


class GitHubForge:
    kind = "github"

    def __init__(self, base_url: str, token: str, repo: str, timeout: float = 30.0, workflow: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.url = base_url.rstrip("/") or "https://github.com"
        host = urlparse(self.url).netloc
        self.api = "https://api.github.com" if host in ("github.com", "www.github.com") else f"{self.url}/api/v3"
        self.repo = repo.strip("/")
        self.token = token
        self.workflow = workflow            # optional workflow file name (e.g. preview.yml) to scope pipeline lookups
        self.http = httpx.Client(
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            timeout=timeout, follow_redirects=True, transport=transport)

    def _r(self, path: str) -> str:
        return f"{self.api}/repos/{self.repo}{path}"

    def _get(self, path: str, **params: Any) -> Any:
        r = self.http.get(self._r(path), params=params or None)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        r = self.http.post(self._r(path), json=payload)
        r.raise_for_status()
        return r.json()

    # ---- branches & commits (Git Data API)
    def _ref_sha(self, ref: str) -> str:
        return self._get(f"/git/ref/heads/{ref}")["object"]["sha"]

    def create_branch(self, branch: str, ref: str) -> None:
        """POST /git/refs; a 422 means the branch already exists, which is fine."""
        sha = self._ref_sha(ref)
        r = self.http.post(self._r("/git/refs"), json={"ref": f"refs/heads/{branch}", "sha": sha})
        if r.status_code not in (201, 422):
            r.raise_for_status()

    def commit_files(self, branch: str, message: str, files: dict[str, bytes]) -> str:
        """One commit with every file (create or update) on top of the branch head. Returns the new sha."""
        head = self._ref_sha(branch)
        base_tree = self._get(f"/git/commits/{head}")["tree"]["sha"]
        entries = []
        for path, content in files.items():
            blob = self._post("/git/blobs", {"content": base64.b64encode(content).decode(), "encoding": "base64"})
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = self._post("/git/trees", {"base_tree": base_tree, "tree": entries})
        commit = self._post("/git/commits", {"message": message, "tree": tree["sha"], "parents": [head]})
        r = self.http.patch(self._r(f"/git/refs/heads/{branch}"), json={"sha": commit["sha"], "force": False})
        r.raise_for_status()
        return commit["sha"]

    def file_url(self, branch: str, path: str) -> str:
        return f"{self.url}/{self.repo}/blob/{branch}/{path}"

    # ---- pull requests
    def create_change_request(self, *, source_branch: str, target_branch: str, title: str, description: str,
                              labels: list[str], reviewers: list[str], draft: bool = False) -> ChangeRequest:
        d = self._post("/pulls", {"title": title, "head": source_branch, "base": target_branch,
                                  "body": description, "draft": draft})
        number = d["number"]
        if labels:
            self.add_labels(number, labels)
        if reviewers:
            # A personal repo's owner cannot review their own PR: GitHub answers 422. Not an error for us.
            r = self.http.post(self._r(f"/pulls/{number}/requested_reviewers"), json={"reviewers": reviewers})
            if r.status_code not in (201, 422):
                r.raise_for_status()
        return ChangeRequest(id=number, url=d["html_url"], source_branch=source_branch, target_branch=target_branch)

    def change_request_state(self, cr_id: int) -> dict[str, Any]:
        """GET /pulls/{n}. `state` is normalised to opened | merged | closed (GitLab's vocabulary)."""
        d = self._get(f"/pulls/{cr_id}")
        state = "merged" if d.get("merged") else ("opened" if d.get("state") == "open" else "closed")
        return {"state": state, "merged_at": d.get("merged_at"), "sha": (d.get("head") or {}).get("sha"),
                "merge_commit_sha": d.get("merge_commit_sha"), "labels": [lb["name"] for lb in d.get("labels", [])],
                "detailed_merge_status": d.get("mergeable_state")}

    def change_request_approved(self, cr_id: int) -> bool:
        """Latest non-comment review per user: approved when at least one APPROVED and no CHANGES_REQUESTED stands."""
        latest: dict[str, str] = {}
        for rev in self._get(f"/pulls/{cr_id}/reviews", per_page=100):
            state = rev.get("state")
            if state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                latest[(rev.get("user") or {}).get("login", "?")] = state
        states = set(latest.values())
        return "APPROVED" in states and "CHANGES_REQUESTED" not in states

    def change_request_comments(self, cr_id: int) -> list[dict[str, Any]]:
        """Top-level (issue) comments, oldest first, excluding the orchestrator's own."""
        out = []
        for c in self._get(f"/issues/{cr_id}/comments", per_page=100):
            if BOT_MARKER in (c.get("body") or ""):
                continue
            out.append({"author": (c.get("user") or {}).get("login", "?"), "body": c.get("body", ""),
                        "created_at": c.get("created_at"), "position": None})
        return out

    def change_request_discussions(self, cr_id: int) -> list[dict[str, Any]]:
        """Inline review comments with path and line (GET /pulls/{n}/comments)."""
        out = []
        for c in self._get(f"/pulls/{cr_id}/comments", per_page=100):
            if BOT_MARKER in (c.get("body") or ""):
                continue
            out.append({"author": (c.get("user") or {}).get("login", "?"), "body": c.get("body", ""),
                        "resolved": None, "path": c.get("path"), "line": c.get("line") or c.get("original_line")})
        return out

    def add_labels(self, cr_id: int, labels: list[str]) -> None:
        self._post(f"/issues/{cr_id}/labels", {"labels": labels})

    def post_comment(self, cr_id: int, body: str) -> None:
        self._post(f"/issues/{cr_id}/comments", {"body": f"{BOT_MARKER}\n{body}"})

    # ---- pipelines (GitHub Actions workflow runs) and deployments. Read-only. UNVERIFIED live.

    def latest_pipeline(self, ref: str, sha: str | None = None) -> dict[str, Any] | None:
        """Most recent workflow run for a branch (optionally one head sha).
        GET /actions/runs?branch=&head_sha= or, with `workflow` set, GET /actions/workflows/{file}/runs."""
        params: dict[str, Any] = {"branch": ref, "per_page": 1}
        if sha:
            params["head_sha"] = sha
        path = f"/actions/workflows/{self.workflow}/runs" if self.workflow else "/actions/runs"
        runs = self._get(path, **params).get("workflow_runs", [])
        return _run_summary(runs[0]) if runs else None

    def pipeline(self, pipeline_id: int) -> dict[str, Any]:
        return _run_summary(self._get(f"/actions/runs/{pipeline_id}"))

    def pipeline_jobs(self, pipeline_id: int) -> list[dict[str, Any]]:
        jobs = self._get(f"/actions/runs/{pipeline_id}/jobs", per_page=100).get("jobs", [])
        return [{"id": j["id"], "name": j["name"], "stage": None, "status": _normalise_status(j),
                 "web_url": j.get("html_url"), "environment": None, "failure_reason": j.get("conclusion")} for j in jobs]

    def job_log_excerpt(self, job_id: int, max_chars: int = 6000) -> str:
        """GET /actions/jobs/{id}/logs redirects to a plain-text download; httpx follows it."""
        r = self.http.get(self._r(f"/actions/jobs/{job_id}/logs"))
        if r.status_code != 200:
            return f"(log unavailable: HTTP {r.status_code})"
        return r.text[-max_chars:]

    def failed_job_log_excerpt(self, pipeline_id: int, max_chars: int = 6000) -> str:
        failed = [j for j in self.pipeline_jobs(pipeline_id) if j["status"] == "failed"]
        if not failed:
            return "(no failed job in workflow run)"
        j = failed[0]
        return f"job {j['name']} ({j.get('failure_reason') or 'failed'}) {j.get('web_url') or ''}\n" + \
            self.job_log_excerpt(j["id"], max_chars)

    def _latest_status(self, deployment_id: int) -> dict[str, Any] | None:
        statuses = self._get(f"/deployments/{deployment_id}/statuses", per_page=1)
        return statuses[0] if statuses else None

    def environment(self, name: str) -> dict[str, Any] | None:
        """The latest deployment to environment `name` and its latest status's `environment_url`
        (GET /deployments?environment=, GET /deployments/{id}/statuses)."""
        deps = self._get("/deployments", environment=name, per_page=1)
        if not deps:
            return None
        d = deps[0]
        st = self._latest_status(d["id"]) or {}
        return {"id": d["id"], "name": name, "external_url": st.get("environment_url"),
                "state": _deployment_state(st.get("state")), "last_deployment": _deployment_summary(d, st)}

    def environment_url_for_branch(self, branch: str, preview_prefix: str = "preview/") -> str | None:
        """Per-branch preview URL: the newest successful deployment whose `ref` is the branch, else an environment
        named `<prefix><branch>` or `<prefix><slug>` (templates/github-actions-preview.yml uses the first)."""
        for d in self._get("/deployments", ref=branch, per_page=5):
            st = self._latest_status(d["id"]) or {}
            if st.get("environment_url"):
                return st["environment_url"]
        for candidate in (f"{preview_prefix}{branch}", f"{preview_prefix}{ref_slug(branch)}"):
            env = self.environment(candidate)
            if env and env.get("external_url"):
                return env["external_url"]
        return None

    def deployment_for_sha(self, environment_name: str, sha: str) -> dict[str, Any] | None:
        deps = self._get("/deployments", environment=environment_name, sha=sha, per_page=1)
        if not deps:
            return None
        return _deployment_summary(deps[0], self._latest_status(deps[0]["id"]) or {})

    # ---- sessions
    def session_repository_resource(self, repo_url: str, branch: str) -> dict[str, Any] | None:
        """The native `github_repository` resource: Anthropic's git proxy injects the token for clone and push."""
        url = repo_url[:-4] if repo_url.endswith(".git") else repo_url
        return {"type": "github_repository", "url": url, "mount_path": "/workspace/repo",
                "authorization_token": self.token, "checkout": {"type": "branch", "name": branch}}

    def sandbox_git_instructions(self, repo_url: str, base_branch: str, branch: str) -> str:
        return GIT_SETUP.format(base_branch=base_branch, branch=branch)

    def authenticated_url(self, repo_url: str) -> str:
        """Clone URL for the orchestrator's own checkout. `x-access-token:<token>` is GitHub's documented form."""
        return repo_url.replace("https://", f"https://x-access-token:{self.token}@", 1)

    def render_feedback_md(self, comments: list[dict[str, Any]], discussions: list[dict[str, Any]]) -> str:
        return render_feedback_md(comments, discussions, title="Human feedback from the design PR")


def _normalise_status(d: dict[str, Any]) -> str:
    """GitHub `status` + `conclusion` -> GitLab-style pipeline status (PIPELINE_TERMINAL applies)."""
    status, conclusion = d.get("status"), d.get("conclusion")
    if status == "completed":
        return {"success": "success", "neutral": "success", "failure": "failed", "timed_out": "failed",
                "startup_failure": "failed", "cancelled": "canceled", "stale": "canceled", "skipped": "skipped",
                "action_required": "manual"}.get(conclusion or "", "failed")
    return {"queued": "pending", "waiting": "pending", "requested": "pending", "pending": "pending"}.get(status or "", "running")


def _run_summary(d: dict[str, Any]) -> dict[str, Any]:
    return {"id": d["id"], "status": _normalise_status(d), "ref": d.get("head_branch"), "sha": d.get("head_sha"),
            "web_url": d.get("html_url"), "created_at": d.get("created_at"), "updated_at": d.get("updated_at"),
            "workflow": d.get("name")}


def _deployment_state(state: str | None) -> str | None:
    return {"success": "success", "failure": "failed", "error": "failed", "inactive": "canceled",
            "in_progress": "running", "queued": "created", "pending": "created"}.get(state or "", state)


def _deployment_summary(d: dict[str, Any], st: dict[str, Any]) -> dict[str, Any]:
    return {"id": d.get("id"), "status": _deployment_state(st.get("state")), "sha": d.get("sha"), "ref": d.get("ref"),
            "created_at": d.get("created_at"), "environment": d.get("environment"),
            "environment_url": st.get("environment_url")}


def parse_workflow_run_webhook(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise a GitHub `workflow_run` webhook to the shape `pipeline()` returns. UNVERIFIED live."""
    run = payload.get("workflow_run")
    if not run:
        return None
    out = _run_summary(run)
    out["project_path"] = (payload.get("repository") or {}).get("full_name")
    out["environments"] = []   # the Deployments API, not the webhook, says where a run deployed
    return out


__all__ = ["BOT_MARKER", "GIT_SETUP", "GitHubForge", "PIPELINE_TERMINAL", "parse_workflow_run_webhook"]
