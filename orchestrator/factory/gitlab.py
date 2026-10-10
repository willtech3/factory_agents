"""GitLab integration (orchestrator side).

Two uses:
1. The **design MR**: decomposition and specs are committed under `.factory/runs/<run_id>/`
   on a branch and opened as a draft MR, so Gate 1 and Gate 2 happen in GitLab's own review
   UI with inline comments. Approval = MR approval; comments are fed back to agents verbatim.
2. The **code MR** per spec, created by the `open_merge_request` custom tool handler.

The sandbox never holds this token. Only the REST calls the factory needs are implemented.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


@dataclass
class MergeRequest:
    iid: int
    web_url: str
    source_branch: str
    target_branch: str


class GitLabClient:
    def __init__(self, base_url: str, token: str, project: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/") + "/api/v4"
        self.project = quote(project, safe="")
        self.http = httpx.Client(headers={"PRIVATE-TOKEN": token}, timeout=timeout)

    def _p(self, path: str) -> str:
        return f"{self.base}/projects/{self.project}{path}"

    # ---- branches & commits
    def create_branch(self, branch: str, ref: str) -> None:
        r = self.http.post(self._p("/repository/branches"), data={"branch": branch, "ref": ref})
        if r.status_code not in (201, 400):   # 400 when it already exists
            r.raise_for_status()

    def commit_files(self, branch: str, message: str, files: dict[str, bytes]) -> str:
        """Create or update several files in one commit. Returns the commit sha."""
        existing = self._existing_paths(branch, list(files))
        actions = [{
            "action": "update" if path in existing else "create",
            "file_path": path,
            "content": base64.b64encode(content).decode(),
            "encoding": "base64",
        } for path, content in files.items()]
        r = self.http.post(self._p("/repository/commits"),
                           json={"branch": branch, "commit_message": message, "actions": actions})
        r.raise_for_status()
        return r.json()["id"]

    def _existing_paths(self, branch: str, paths: list[str]) -> set[str]:
        found = set()
        for p in paths:
            r = self.http.head(self._p(f"/repository/files/{quote(p, safe='')}"), params={"ref": branch})
            if r.status_code == 200:
                found.add(p)
        return found

    # ---- merge requests
    def create_mr(self, *, source_branch: str, target_branch: str, title: str, description: str,
                  labels: list[str], reviewer_usernames: list[str], draft: bool = False) -> MergeRequest:
        reviewer_ids = [self._user_id(u) for u in reviewer_usernames]
        payload: dict[str, Any] = {
            "source_branch": source_branch, "target_branch": target_branch,
            "title": ("Draft: " if draft else "") + title, "description": description,
            "labels": ",".join(labels), "remove_source_branch": True,
        }
        if reviewer_ids:
            payload["reviewer_ids"] = [i for i in reviewer_ids if i]
        r = self.http.post(self._p("/merge_requests"), json=payload)
        r.raise_for_status()
        d = r.json()
        return MergeRequest(iid=d["iid"], web_url=d["web_url"], source_branch=source_branch, target_branch=target_branch)

    def mr_state(self, iid: int) -> dict[str, Any]:
        r = self.http.get(self._p(f"/merge_requests/{iid}"))
        r.raise_for_status()
        d = r.json()
        return {"state": d["state"], "merged_at": d.get("merged_at"), "sha": d.get("sha"),
                "labels": d.get("labels", []), "detailed_merge_status": d.get("detailed_merge_status")}

    def mr_approved(self, iid: int) -> bool:
        r = self.http.get(self._p(f"/merge_requests/{iid}/approvals"))
        r.raise_for_status()
        d = r.json()
        return bool(d.get("approved")) or (d.get("approvals_left", 1) == 0 and d.get("approved_by"))

    def mr_notes(self, iid: int) -> list[dict[str, Any]]:
        """Human comments on the MR, oldest first, excluding system notes. Fed back to agents verbatim."""
        r = self.http.get(self._p(f"/merge_requests/{iid}/notes"), params={"sort": "asc", "per_page": 100})
        r.raise_for_status()
        return [{"author": n["author"]["username"], "body": n["body"], "created_at": n["created_at"],
                 "position": n.get("position")} for n in r.json() if not n.get("system")]

    def mr_discussions(self, iid: int) -> list[dict[str, Any]]:
        """Inline discussions with file/line positions, for feedback that points at a spec line."""
        r = self.http.get(self._p(f"/merge_requests/{iid}/discussions"), params={"per_page": 100})
        r.raise_for_status()
        out = []
        for d in r.json():
            for n in d.get("notes", []):
                if n.get("system"):
                    continue
                pos = n.get("position") or {}
                out.append({"author": n["author"]["username"], "body": n["body"], "resolved": n.get("resolved"),
                            "path": pos.get("new_path"), "line": pos.get("new_line")})
        return out

    def add_labels(self, iid: int, labels: list[str]) -> None:
        r = self.http.put(self._p(f"/merge_requests/{iid}"), json={"add_labels": ",".join(labels)})
        r.raise_for_status()

    def post_note(self, iid: int, body: str) -> None:
        r = self.http.post(self._p(f"/merge_requests/{iid}/notes"), json={"body": body})
        r.raise_for_status()

    # ---- pipelines and environments (Stage 5 observation; read-only)
    # UNVERIFIED against the user's self-hosted instance. Shapes follow the GitLab REST docs
    # (Pipelines API, Jobs API, Environments API, Deployments API) for GitLab 16/17; field names
    # are hypotheses until the roadmap spike runs them against the real host. Nothing here writes.

    def latest_pipeline(self, ref: str, sha: str | None = None) -> dict[str, Any] | None:
        """Most recent pipeline for a branch (optionally a specific sha). GET /projects/:id/pipelines."""
        params: dict[str, Any] = {"ref": ref, "order_by": "id", "sort": "desc", "per_page": 1}
        if sha:
            params["sha"] = sha
        r = self.http.get(self._p("/pipelines"), params=params)
        r.raise_for_status()
        items = r.json()
        return _pipeline_summary(items[0]) if items else None

    def pipeline(self, pipeline_id: int) -> dict[str, Any]:
        """GET /projects/:id/pipelines/:pipeline_id. `status` is one of created, waiting_for_resource, preparing,
        pending, running, success, failed, canceled, skipped, manual, scheduled."""
        r = self.http.get(self._p(f"/pipelines/{pipeline_id}"))
        r.raise_for_status()
        return _pipeline_summary(r.json())

    def pipeline_jobs(self, pipeline_id: int) -> list[dict[str, Any]]:
        """GET /projects/:id/pipelines/:pipeline_id/jobs. Includes each job's stage, status, and environment (if deploy)."""
        r = self.http.get(self._p(f"/pipelines/{pipeline_id}/jobs"), params={"per_page": 100})
        r.raise_for_status()
        return [{"id": j["id"], "name": j["name"], "stage": j.get("stage"), "status": j["status"],
                 "web_url": j.get("web_url"), "environment": (j.get("environment") or {}).get("name"),
                 "failure_reason": j.get("failure_reason")} for j in r.json()]

    def job_log_excerpt(self, job_id: int, max_chars: int = 6000) -> str:
        """Tail of a job's trace. GET /projects/:id/jobs/:job_id/trace (plain text). Used for DEPLOY_FAILURE gates."""
        r = self.http.get(self._p(f"/jobs/{job_id}/trace"))
        if r.status_code != 200:
            return f"(trace unavailable: HTTP {r.status_code})"
        return r.text[-max_chars:]

    def failed_job_log_excerpt(self, pipeline_id: int, max_chars: int = 6000) -> str:
        failed = [j for j in self.pipeline_jobs(pipeline_id) if j["status"] == "failed"]
        if not failed:
            return "(no failed job in pipeline)"
        j = failed[0]
        return f"job {j['name']} ({j.get('failure_reason') or 'failed'}) {j.get('web_url') or ''}\n" + \
            self.job_log_excerpt(j["id"], max_chars)

    def environment(self, name: str) -> dict[str, Any] | None:
        """Find an environment by exact name. GET /projects/:id/environments?name=... returns `external_url`,
        which is the preview or dev URL the platform's deploy job registered via `environment:url` in CI."""
        r = self.http.get(self._p("/environments"), params={"name": name})
        r.raise_for_status()
        items = r.json()
        if not items:
            return None
        e = items[0]
        return {"id": e["id"], "name": e["name"], "external_url": e.get("external_url"), "state": e.get("state"),
                "last_deployment": _deployment_summary(e.get("last_deployment"))}

    def environment_url_for_branch(self, branch: str, preview_prefix: str = "preview/") -> str | None:
        """The per-branch preview URL. The platform's CI names review environments `<prefix><branch>`
        (GitLab's `review/$CI_COMMIT_REF_SLUG` convention); the prefix is configurable because the real
        template may differ. Returns None when no environment exists yet (deploy not run)."""
        slug = _ref_slug(branch)
        for candidate in (f"{preview_prefix}{branch}", f"{preview_prefix}{slug}"):
            env = self.environment(candidate)
            if env and env.get("external_url"):
                return env["external_url"]
        # Fallback: search by prefix and match the slug (GET /environments?search=).
        r = self.http.get(self._p("/environments"), params={"search": slug, "per_page": 50})
        if r.status_code == 200:
            for e in r.json():
                if e.get("external_url") and slug in e.get("name", ""):
                    return e["external_url"]
        return None

    def deployment_for_sha(self, environment_name: str, sha: str) -> dict[str, Any] | None:
        """Whether `sha` has been deployed to `environment_name`. GET /projects/:id/deployments?environment=...
        Deployment `status` is one of created, running, success, failed, canceled, blocked."""
        r = self.http.get(self._p("/deployments"), params={"environment": environment_name, "order_by": "id",
                                                            "sort": "desc", "per_page": 20})
        r.raise_for_status()
        for d in r.json():
            if d.get("sha") == sha:
                return _deployment_summary(d)
        return None

    def _user_id(self, username: str) -> int | None:
        r = self.http.get(f"{self.base}/users", params={"username": username})
        if r.status_code != 200 or not r.json():
            return None
        return r.json()[0]["id"]


def _pipeline_summary(d: dict[str, Any]) -> dict[str, Any]:
    return {"id": d["id"], "status": d["status"], "ref": d.get("ref"), "sha": d.get("sha"), "web_url": d.get("web_url"),
            "created_at": d.get("created_at"), "updated_at": d.get("updated_at")}


def _deployment_summary(d: dict[str, Any] | None) -> dict[str, Any] | None:
    if not d:
        return None
    return {"id": d.get("id"), "status": d.get("status"), "sha": d.get("sha"), "ref": d.get("ref"),
            "created_at": d.get("created_at"), "deployable_id": (d.get("deployable") or {}).get("id")}


def _ref_slug(ref: str) -> str:
    """Approximates GitLab's CI_COMMIT_REF_SLUG: lowercase, [a-z0-9-], max 63 chars, no leading/trailing dash."""
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", ref.lower()).strip("-")
    return slug[:63].rstrip("-")


PIPELINE_TERMINAL = {"success", "failed", "canceled", "skipped"}


def parse_pipeline_webhook(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise a GitLab Pipeline Hook payload (`object_kind: pipeline`) to the shape `pipeline()` returns.

    Returns None for other hook kinds. The webhook endpoint that receives this is part of the Factory
    Console (not in this repository); the orchestrator reconciles by polling `pipeline()` regardless,
    because webhooks are not a durable log. UNVERIFIED against the self-hosted instance.
    """
    if payload.get("object_kind") != "pipeline":
        return None
    a = payload.get("object_attributes") or {}
    return {"id": a.get("id"), "status": a.get("status"), "ref": a.get("ref"), "sha": a.get("sha"),
            "web_url": a.get("url"), "created_at": a.get("created_at"), "updated_at": a.get("finished_at"),
            "project_path": (payload.get("project") or {}).get("path_with_namespace"),
            "environments": sorted({(b.get("environment") or {}).get("name") for b in payload.get("builds", [])
                                    if b.get("environment")})}


def render_feedback_md(notes: list[dict[str, Any]], discussions: list[dict[str, Any]]) -> str:
    """Turn GitLab review comments into the feedback.md an agent reads on a re-run."""
    lines = ["# Human feedback from the design MR", ""]
    for d in discussions:
        where = f" (`{d['path']}:{d['line']}`)" if d.get("path") else ""
        lines.append(f"- **@{d['author']}**{where}: {d['body'].strip()}")
    for n in notes:
        if n.get("position"):
            continue  # already covered as a discussion
        lines.append(f"- **@{n['author']}**: {n['body'].strip()}")
    lines += ["", "Address every item explicitly in your summary: either apply it, or explain why not."]
    return "\n".join(lines)
