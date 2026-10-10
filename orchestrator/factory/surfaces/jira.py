"""Jira tracker: a stub with the `Tracker` protocol's methods, for the work setup (Jira + self-hosted GitLab).

Every method raises NotImplementedError. The docstrings say which Jira Cloud REST v3 call each one maps
to, so filling this in is mechanical; `TrackerQueue` needs nothing else. Keep the client as thin as
`LinearClient`: one `execute`-style helper, no SDK.

Mapping:

| Tracker method      | Jira                                                                                  |
|---------------------|---------------------------------------------------------------------------------------|
| ensure_project      | an Epic in the configured project (`POST /rest/api/3/issue`, issuetype Epic), or the  |
|                     | project key itself when `JIRA_PROJECT_KEY` pins one project per run                    |
| ensure_issue        | `POST /rest/api/3/issue` (issuetype Task/Story) with `parent` = the run's Epic         |
| issue_url           | `<JIRA_URL>/browse/<key>`                                                              |
| set_issue_stage     | `GET /rest/api/3/issue/{key}/transitions`, pick by name/category, then `POST` it      |
|                     | (Jira status categories: new, indeterminate, done mirror STAGE_TO_STATE's types)      |
| labels/add/remove   | the `labels` field via `PUT /rest/api/3/issue/{key}` (`update: {labels: [{add|remove}]}`) |
| post_comment        | `POST /rest/api/3/issue/{key}/comment` (body in ADF; wrap markdown in a codeBlock or    |
|                     | convert; the HTML marker must survive as text for `TrackerQueue` to find it)          |
| comments            | `GET /rest/api/3/issue/{key}/comment` -> {id, body (text), author.displayName,         |
|                     | created, parent: none (Jira has no threads; TrackerQueue falls back to "mentions id")} |

Env: `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` (basic auth), `JIRA_PROJECT_KEY`.
"""
from __future__ import annotations

from typing import Any


class JiraTracker:
    def __init__(self, base_url: str, email: str, api_token: str, project_key: str):
        self.base_url, self.email, self.api_token, self.project_key = base_url, email, api_token, project_key

    def ensure_project(self, run_id: str, name: str, description: str) -> str:
        """Create (or reuse) the Epic that represents the run; return its key."""
        raise NotImplementedError("JiraTracker.ensure_project: POST /rest/api/3/issue with issuetype Epic")

    def ensure_issue(self, project_id: str, key: str, title: str, description: str,
                     parent_id: str | None = None) -> str:
        raise NotImplementedError("JiraTracker.ensure_issue: POST /rest/api/3/issue with parent = run Epic")

    def issue_url(self, issue_id: str) -> str:
        raise NotImplementedError("JiraTracker.issue_url: <JIRA_URL>/browse/<key>")

    def set_issue_stage(self, issue_id: str, stage: str) -> None:
        raise NotImplementedError("JiraTracker.set_issue_stage: GET then POST /rest/api/3/issue/{key}/transitions")

    def labels(self, issue_id: str) -> list[str]:
        raise NotImplementedError("JiraTracker.labels: GET /rest/api/3/issue/{key}?fields=labels")

    def add_label(self, issue_id: str, label: str) -> None:
        raise NotImplementedError("JiraTracker.add_label: PUT /rest/api/3/issue/{key} update.labels add")

    def remove_label(self, issue_id: str, label: str) -> None:
        raise NotImplementedError("JiraTracker.remove_label: PUT /rest/api/3/issue/{key} update.labels remove")

    def post_comment(self, issue_id: str, body: str, parent_id: str | None = None) -> str:
        raise NotImplementedError("JiraTracker.post_comment: POST /rest/api/3/issue/{key}/comment (ADF body)")

    def comments(self, issue_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError("JiraTracker.comments: GET /rest/api/3/issue/{key}/comment")
