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

    def _user_id(self, username: str) -> int | None:
        r = self.http.get(f"{self.base}/users", params={"username": username})
        if r.status_code != 200 or not r.json():
            return None
        return r.json()[0]["id"]


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
