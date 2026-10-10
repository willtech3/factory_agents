"""Tracker gate surface: the board and the record of a run live in the issue tracker.

Mapping (ADR-0007):

| Factory                       | Tracker                                                        |
|-------------------------------|----------------------------------------------------------------|
| run                           | one project (or `LINEAR_PROJECT_ID`) plus one *run issue*      |
| work item                     | one issue, child of the run issue; state mirrors WorkItemStage |
| gate (1, 2, escalation)       | a comment on the run issue + label `factory:gate-open`         |
| DEPLOY_FAILURE gate           | the same, on the work item's issue                             |
| decision                      | label `factory:approve|request-changes|reject|approve-subset` + newest human comment |
| blocking question             | a `factory:question` comment; the human replies in its thread  |
| feedback.md                   | human comments after the gate comment, rendered                |

Every gate comment carries an HTML marker with the gate id and the artifact sha256 list. The decision
the surface returns copies those hashes from the comment it reads back, so `validate_decision` applies
exactly as it does for the CLI (ADR-0005): a decision on a superseded gate comment is stale.

`Tracker` is the tracker-neutral protocol (`LinearTracker` implements it with Linear's GraphQL API
through a thin client; `jira.py` is a stub with the same methods). `TrackerQueue` turns any `Tracker`
into an `ApprovalQueue` + `Notifier`. Nothing here is verified against the live Linear API; the
mapping is unit-tested with a fake client.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..gates import Answer, Decision, Gate, GateDecision, Question, validate_decision
from ..state import WorkItemStage

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config

LABEL_GATE_OPEN = "factory:gate-open"
LABEL_QUESTION = "factory:question"
LABEL_BLOCKED = "factory:blocked"
DECISION_LABELS = {
    "factory:approve": Decision.APPROVE,
    "factory:request-changes": Decision.REQUEST_CHANGES,
    "factory:reject": Decision.REJECT,
    "factory:approve-subset": Decision.APPROVE_SUBSET,
}
ALL_LABELS = (LABEL_GATE_OPEN, LABEL_QUESTION, LABEL_BLOCKED, *DECISION_LABELS)

GATE_MARKER = "<!-- factory-gate gate_id={gate_id} kind={kind} shas={shas} -->"
QUESTION_MARKER = "<!-- factory-question question_id={question_id} -->"
_GATE_RE = re.compile(r"<!-- factory-gate gate_id=(\S+) kind=(\S+) shas=(\S*) -->")
_QUESTION_RE = re.compile(r"<!-- factory-question question_id=(\S+) -->")
_SUBSET_RE = re.compile(r"^\s*(?:approve|approved|specs?)\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)

# WorkItemStage -> (preferred Linear state names, fallback state *type*). Linear's standard types are
# triage, backlog, unstarted, started, completed, canceled; names vary per team, so types are the fallback.
STAGE_TO_STATE: dict[str, tuple[tuple[str, ...], str]] = {
    "pending": (("Backlog",), "backlog"),
    "spec_drafting": (("Todo",), "unstarted"),
    "spec_judging": (("Todo",), "unstarted"),
    "spec_approved": (("Todo",), "unstarted"),
    "implementing": (("In Progress",), "started"),
    "reviewing": (("In Progress",), "started"),
    "responding": (("In Progress",), "started"),
    "merge_judging": (("In Progress",), "started"),
    "integrating": (("In Progress",), "started"),
    "cr_open": (("In Review",), "started"),
    "merged": (("Merged", "In Review"), "started"),
    "deploying_dev": (("Merged", "In Review"), "started"),
    "deployed_dev": (("Done",), "completed"),
    "closed": (("Canceled", "Cancelled"), "canceled"),
    "escalated": (("Blocked", "Todo"), "unstarted"),
    "blocked": (("Blocked", "Todo"), "unstarted"),
}


class Tracker(Protocol):
    """Tracker-neutral operations. Issue ids, comment ids, and label names are the tracker's own."""

    def ensure_project(self, run_id: str, name: str, description: str) -> str: ...
    def ensure_issue(self, project_id: str, key: str, title: str, description: str,
                     parent_id: str | None = None) -> str: ...
    def issue_url(self, issue_id: str) -> str: ...
    def set_issue_stage(self, issue_id: str, stage: str) -> None: ...
    def labels(self, issue_id: str) -> list[str]: ...
    def add_label(self, issue_id: str, label: str) -> None: ...
    def remove_label(self, issue_id: str, label: str) -> None: ...
    def post_comment(self, issue_id: str, body: str, parent_id: str | None = None) -> str: ...
    def comments(self, issue_id: str) -> list[dict[str, Any]]: ...
    """Oldest first: {id, body, author, created_at, parent_id, is_bot}."""


# ----------------------------------------------------------------------------------------------- Linear

class LinearClient:
    """The whole Linear surface the factory uses is one POST per operation; keep it this thin so the fake
    in tests and a Jira client can mirror it. UNVERIFIED live."""

    def __init__(self, api_key: str, url: str = "https://api.linear.app/graphql", timeout: float = 30.0):
        import httpx
        self.http = httpx.Client(headers={"Authorization": api_key, "Content-Type": "application/json"}, timeout=timeout)
        self.url = url

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        r = self.http.post(self.url, json={"query": query, "variables": variables or {}})
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise RuntimeError(f"Linear GraphQL error: {json.dumps(body['errors'])[:500]}")
        return body["data"]


Q_TEAM = """query TeamContext($teamId: String!) {
  team(id: $teamId) { id key states { nodes { id name type } } labels { nodes { id name } } } }"""
M_LABEL_CREATE = """mutation IssueLabelCreate($input: IssueLabelCreateInput!) {
  issueLabelCreate(input: $input) { issueLabel { id name } } }"""
M_PROJECT_CREATE = """mutation ProjectCreate($input: ProjectCreateInput!) {
  projectCreate(input: $input) { project { id url } } }"""
M_ISSUE_CREATE = """mutation IssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) { issue { id identifier url } } }"""
M_ISSUE_UPDATE = """mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success } }"""
Q_ISSUE_LABELS = """query IssueLabels($id: String!) { issue(id: $id) { url labels { nodes { id name } } } }"""
M_COMMENT_CREATE = """mutation CommentCreate($input: CommentCreateInput!) {
  commentCreate(input: $input) { comment { id url } } }"""
Q_ISSUE_COMMENTS = """query IssueComments($id: String!) {
  issue(id: $id) { comments(first: 250) { nodes { id body createdAt parent { id } user { name displayName isMe } } } } }"""


class LinearTracker:
    """`Tracker` over Linear's GraphQL API. One project per run unless `project_id` pins a shared project."""

    def __init__(self, client: Any, team_id: str, project_id: str | None = None):
        self.client = client
        self.team_id = team_id
        self.project_id = project_id
        self._team: dict[str, Any] | None = None
        self._label_ids: dict[str, str] = {}
        self._urls: dict[str, str] = {}

    # -- team context (states and labels), cached
    def _team_ctx(self) -> dict[str, Any]:
        if self._team is None:
            self._team = self.client.execute(Q_TEAM, {"teamId": self.team_id})["team"]
            self._label_ids = {lb["name"]: lb["id"] for lb in self._team["labels"]["nodes"]}
        return self._team

    def _state_id(self, stage: str) -> str | None:
        names, typ = STAGE_TO_STATE.get(stage, ((), "unstarted"))
        states = self._team_ctx()["states"]["nodes"]
        for n in names:
            for st in states:
                if st["name"].lower() == n.lower():
                    return st["id"]
        for st in states:
            if st["type"] == typ:
                return st["id"]
        return None

    def _label_id(self, name: str) -> str:
        self._team_ctx()
        if name not in self._label_ids:
            d = self.client.execute(M_LABEL_CREATE, {"input": {"teamId": self.team_id, "name": name}})
            lb = d["issueLabelCreate"]["issueLabel"]
            self._label_ids[lb["name"]] = lb["id"]
        return self._label_ids[name]

    # -- Tracker protocol
    def ensure_project(self, run_id: str, name: str, description: str) -> str:
        if self.project_id:
            return self.project_id
        d = self.client.execute(M_PROJECT_CREATE, {"input": {"name": name, "description": description[:2000],
                                                             "teamIds": [self.team_id]}})
        return d["projectCreate"]["project"]["id"]

    def ensure_issue(self, project_id: str, key: str, title: str, description: str,
                     parent_id: str | None = None) -> str:
        payload: dict[str, Any] = {"teamId": self.team_id, "title": title[:255], "description": description,
                                   "projectId": project_id}
        if parent_id:
            payload["parentId"] = parent_id
        d = self.client.execute(M_ISSUE_CREATE, {"input": payload})
        issue = d["issueCreate"]["issue"]
        self._urls[issue["id"]] = issue.get("url", "")
        return issue["id"]

    def issue_url(self, issue_id: str) -> str:
        if issue_id not in self._urls:
            d = self.client.execute(Q_ISSUE_LABELS, {"id": issue_id})
            self._urls[issue_id] = d["issue"].get("url", "")
        return self._urls[issue_id]

    def set_issue_stage(self, issue_id: str, stage: str) -> None:
        sid = self._state_id(stage)
        if sid:
            self.client.execute(M_ISSUE_UPDATE, {"id": issue_id, "input": {"stateId": sid}})
        if stage in ("escalated", "blocked"):
            self.add_label(issue_id, LABEL_BLOCKED)
        elif LABEL_BLOCKED in self.labels(issue_id):
            self.remove_label(issue_id, LABEL_BLOCKED)

    def _issue_label_ids(self, issue_id: str) -> dict[str, str]:
        d = self.client.execute(Q_ISSUE_LABELS, {"id": issue_id})
        self._urls[issue_id] = d["issue"].get("url", self._urls.get(issue_id, ""))
        return {lb["name"]: lb["id"] for lb in d["issue"]["labels"]["nodes"]}

    def labels(self, issue_id: str) -> list[str]:
        return list(self._issue_label_ids(issue_id))

    def add_label(self, issue_id: str, label: str) -> None:
        current = self._issue_label_ids(issue_id)
        if label in current:
            return
        ids = list(current.values()) + [self._label_id(label)]
        self.client.execute(M_ISSUE_UPDATE, {"id": issue_id, "input": {"labelIds": ids}})

    def remove_label(self, issue_id: str, label: str) -> None:
        current = self._issue_label_ids(issue_id)
        if label not in current:
            return
        ids = [i for n, i in current.items() if n != label]
        self.client.execute(M_ISSUE_UPDATE, {"id": issue_id, "input": {"labelIds": ids}})

    def post_comment(self, issue_id: str, body: str, parent_id: str | None = None) -> str:
        payload: dict[str, Any] = {"issueId": issue_id, "body": body}
        if parent_id:
            payload["parentId"] = parent_id
        d = self.client.execute(M_COMMENT_CREATE, {"input": payload})
        return d["commentCreate"]["comment"]["id"]

    def comments(self, issue_id: str) -> list[dict[str, Any]]:
        d = self.client.execute(Q_ISSUE_COMMENTS, {"id": issue_id})
        out = []
        for c in d["issue"]["comments"]["nodes"]:
            user = c.get("user") or {}
            body = c.get("body") or ""
            out.append({"id": c["id"], "body": body, "created_at": c.get("createdAt"),
                        "author": user.get("displayName") or user.get("name") or "?",
                        "parent_id": (c.get("parent") or {}).get("id"),
                        "is_bot": bool(user.get("isMe")) or body.startswith("<!-- factory-")})
        out.sort(key=lambda c: c.get("created_at") or "")
        return out


# ----------------------------------------------------------------------------------------------- queue

@dataclass
class _RunRecord:
    project_id: str
    run_issue: str
    item_issues: dict[str, str] = field(default_factory=dict)
    item_titles: dict[str, str] = field(default_factory=dict)


@dataclass
class _OpenGate:
    gate: Gate
    issue_id: str
    comment_id: str


class TrackerQueue:
    """`ApprovalQueue` + `Notifier` over a `Tracker`. Also mirrors work-item stages onto issues.

    `state_path` (optional) persists the run -> project/issue mapping so a restarted orchestrator finds its
    issues again. Decisions are cached once read, and the decision label plus `factory:gate-open` are removed
    from the issue so the next gate on the same issue starts clean.
    """

    def __init__(self, tracker: Tracker, state_path: Path | None = None, page_urls: dict[str, str] | None = None):
        self.tracker = tracker
        self.state_path = state_path
        self.runs: dict[str, _RunRecord] = {}
        self.gates: dict[str, _OpenGate] = {}
        self.decisions: dict[str, GateDecision] = {}
        self.questions: dict[str, tuple[Question, str, str]] = {}   # question_id -> (q, issue_id, comment_id)
        self.answers: dict[str, Answer] = {}
        self._load()

    # -- persistence of the mapping (not of decisions: those are read from the tracker)
    def _load(self) -> None:
        if self.state_path and self.state_path.exists():
            raw = json.loads(self.state_path.read_text())
            for run_id, r in raw.items():
                self.runs[run_id] = _RunRecord(**r)

    def _save(self) -> None:
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps({k: v.__dict__ for k, v in self.runs.items()}, indent=2))

    # -- run and work-item mirroring
    def register_run(self, run_id: str, outcome: dict[str, Any]) -> str:
        if run_id in self.runs:
            return self.runs[run_id].run_issue
        title = outcome.get("title", run_id)
        project = self.tracker.ensure_project(run_id, f"[factory] {title}", outcome.get("statement", ""))
        body = (f"Factory run `{run_id}` for outcome `{outcome.get('outcome_id', '')}`.\n\n{outcome.get('statement', '')}\n\n"
                "Gates for this run are posted as comments here. Decide with a label and a comment; see "
                "docs/07-personal-e2e-setup.md in the factory repository.")
        issue = self.tracker.ensure_issue(project, f"{run_id}/run", f"[factory] {title}", body)
        self.runs[run_id] = _RunRecord(project_id=project, run_issue=issue)
        self._save()
        return issue

    def register_work_items(self, run_id: str, decomposition: dict[str, Any]) -> dict[str, str]:
        rec = self.runs[run_id]
        for item in decomposition.get("work_items", []):
            wid = item["work_item_id"]
            rec.item_titles[wid] = item.get("title", wid)
            if wid in rec.item_issues:
                continue
            body = (f"{item.get('intent', '')}\n\nKind: {item.get('kind', '')} · size: {item.get('size', '')} · "
                    f"depends on: {', '.join(item.get('depends_on', [])) or 'nothing'}\n\n"
                    f"Run `{run_id}`, work item `{wid}`. State mirrors the factory's work-item stage.")
            rec.item_issues[wid] = self.tracker.ensure_issue(rec.project_id, f"{run_id}/{wid}",
                                                             f"{wid}: {item.get('title', '')}", body, parent_id=rec.run_issue)
            self.tracker.set_issue_stage(rec.item_issues[wid], WorkItemStage.PENDING.value)
        self._save()
        return dict(rec.item_issues)

    def mirror_work_item(self, run_id: str, work_item_id: str, stage: str) -> None:
        rec = self.runs.get(run_id)
        if rec is None or work_item_id not in rec.item_issues or not stage:
            return
        self.tracker.set_issue_stage(rec.item_issues[work_item_id], stage)

    def _issue_for(self, run_id: str, work_item_id: str | None) -> str:
        rec = self.runs[run_id]
        if work_item_id and work_item_id in rec.item_issues:
            return rec.item_issues[work_item_id]
        return rec.run_issue

    # -- ApprovalQueue
    def open(self, gate: Gate) -> None:
        issue = self._issue_for(gate.run_id, gate.work_item_id)
        shas = ",".join(a.sha256 for a in sorted(gate.artifacts, key=lambda a: a.name))
        lines = [GATE_MARKER.format(gate_id=gate.gate_id, kind=gate.kind.value, shas=shas),
                 f"## Gate: {gate.kind.value.replace('_', ' ')} (`{gate.gate_id}`)", "", gate.summary.strip(), "",
                 "### Artifacts (approve exactly these)"]
        for a in sorted(gate.artifacts, key=lambda a: a.name):
            link = f" · [read]({a.url})" if a.url else ""
            lines.append(f"- `{a.name}` · sha256 `{a.sha256}`{link}")
        opts = ", ".join(f"`{_label_for(o)}`" for o in gate.options)
        lines += ["", "### How to decide",
                  f"Apply one label on this issue: {opts}. For request-changes, add a comment with what to change "
                  "(it becomes the agent's feedback verbatim). For approve-subset, add a comment with a line "
                  "`approve: spec_a, spec_b`. The decision binds to the sha256 list above; a newer gate comment "
                  "supersedes this one.", f"Opened {gate.opened_at}; assignees: {', '.join(gate.assignees) or 'owner'}."]
        comment_id = self.tracker.post_comment(issue, "\n".join(lines))
        for lb in DECISION_LABELS:
            self.tracker.remove_label(issue, lb)
        self.tracker.add_label(issue, LABEL_GATE_OPEN)
        self.gates[gate.gate_id] = _OpenGate(gate=gate, issue_id=issue, comment_id=comment_id)

    def decision(self, gate_id: str) -> GateDecision | None:
        if gate_id in self.decisions:
            return self.decisions[gate_id]
        og = self.gates.get(gate_id)
        if og is None:
            return None
        labels = self.tracker.labels(og.issue_id)
        chosen = [DECISION_LABELS[lb] for lb in labels if lb in DECISION_LABELS]
        if not chosen:
            return None
        comments = self.tracker.comments(og.issue_id)
        gate_comment = next((c for c in comments if c["id"] == og.comment_id), None)
        m = _GATE_RE.search(gate_comment["body"]) if gate_comment else None
        if not m:
            return None   # our comment is gone; nothing to bind the decision to, so no decision
        shas = tuple(s for s in m.group(3).split(",") if s)
        after = [c for c in comments if not c["is_bot"] and (c.get("created_at") or "") >= (gate_comment.get("created_at") or "")
                 and c["id"] != og.comment_id and not _QUESTION_RE.search(c["body"])]
        newest = after[-1] if after else None
        comment_text = (newest or {}).get("body", "").strip()
        subset: tuple[str, ...] = ()
        if chosen[0] == Decision.APPROVE_SUBSET:
            sm = _SUBSET_RE.search(comment_text)
            subset = tuple(s.strip().strip("`") for s in sm.group(1).split(",") if s.strip()) if sm else ()
        d = GateDecision(gate_id=gate_id, decision=chosen[0], decided_by=(newest or {}).get("author") or "tracker",
                         comments=comment_text, approved_subset=subset, artifact_shas=shas)
        validate_decision(og.gate, d)
        self.decisions[gate_id] = d
        for lb in (LABEL_GATE_OPEN, *DECISION_LABELS):
            self.tracker.remove_label(og.issue_id, lb)
        return d

    def ask(self, q: Question) -> None:
        issue = self._issue_for(q.run_id, q.work_item_id)
        lines = [QUESTION_MARKER.format(question_id=q.question_id),
                 f"## Question from the {q.asked_by} (`{q.question_id}`)", "", q.question.strip(), "",
                 f"**Why it matters:** {q.why_it_matters}", f"**Default if unanswered:** {q.default_if_unanswered}"]
        if q.options:
            lines.append("**Options:** " + " · ".join(f"`{o}`" for o in q.options))
        lines += ["", "Reply in this thread (or as a comment mentioning the question id) to answer."
                  if q.blocking else "", "This question is blocking: the session is paused until you answer."
                  if q.blocking else "Non-blocking: the default was applied; the answer is recorded for the next gate."]
        comment_id = self.tracker.post_comment(issue, "\n".join(l for l in lines if l is not None))
        self.tracker.add_label(issue, LABEL_QUESTION)
        self.questions[q.question_id] = (q, issue, comment_id)

    def answer(self, question_id: str) -> Answer | None:
        if question_id in self.answers:
            return self.answers[question_id]
        entry = self.questions.get(question_id)
        if entry is None:
            return None
        q, issue, comment_id = entry
        comments = self.tracker.comments(issue)
        asked = next((c for c in comments if c["id"] == comment_id), None)
        asked_at = (asked or {}).get("created_at") or ""
        replies = [c for c in comments if not c["is_bot"] and c.get("parent_id") == comment_id]
        if not replies:
            replies = [c for c in comments if not c["is_bot"] and (c.get("created_at") or "") >= asked_at
                       and question_id in c["body"]]
        if not replies:
            return None
        r = replies[-1]
        a = Answer(question_id=question_id, answer=r["body"].strip(), answered_by=r["author"])
        self.answers[question_id] = a
        if not any(qid != question_id and iss == issue and qid not in self.answers
                   for qid, (_, iss, _) in self.questions.items()):
            self.tracker.remove_label(issue, LABEL_QUESTION)
        return a

    # -- Notifier (the queue already posted; these are no-ops or short notes)
    def gate_opened(self, gate: Gate) -> None:
        pass

    def question_asked(self, q: Question) -> None:
        pass

    def change_request_opened(self, run_id: str, spec_id: str, url: str) -> None:
        rec = self.runs.get(run_id)
        if rec is None:
            return
        wid = next((w for w, t in rec.item_titles.items() if spec_id.endswith(w.removeprefix("wi-"))), None)
        issue = rec.item_issues.get(wid, rec.run_issue) if wid else rec.run_issue
        self.tracker.post_comment(issue, f"<!-- factory-note -->\nChange request opened for `{spec_id}`: {url}\n"
                                         "Gate 3 is review and merge on the forge; this issue moves to In Review.")

    def escalated(self, run_id: str, reason: str) -> None:
        rec = self.runs.get(run_id)
        if rec is not None:
            self.tracker.post_comment(rec.run_issue, f"<!-- factory-note -->\n**Escalated:** {reason}")

    # -- feedback
    def render_feedback_md(self, gate_id: str) -> str:
        og = self.gates.get(gate_id)
        if og is None:
            return ""
        comments = self.tracker.comments(og.issue_id)
        gate_comment = next((c for c in comments if c["id"] == og.comment_id), None)
        since = (gate_comment or {}).get("created_at") or ""
        humans = [c for c in comments if not c["is_bot"] and (c.get("created_at") or "") >= since and c["id"] != og.comment_id]
        lines = [f"# Human feedback from the tracker ({og.gate.kind.value})", ""]
        for c in humans:
            lines.append(f"- **@{c['author']}**: {c['body'].strip()}")
        if not humans:
            lines.append("(no comments)")
        lines += ["", "Address every item explicitly in your summary: either apply it, or explain why not."]
        return "\n".join(lines)

    def gate_url(self, gate_id: str) -> str | None:
        og = self.gates.get(gate_id)
        return self.tracker.issue_url(og.issue_id) if og else None


def _label_for(d: Decision) -> str:
    return next(lb for lb, dd in DECISION_LABELS.items() if dd == d)


def make_tracker_queue(cfg: "Config") -> TrackerQueue | None:
    """The configured tracker queue, or None when the gate surface is not `tracker`."""
    if cfg.gate_surface != "tracker":
        return None
    if not cfg.linear_api_key or not cfg.linear_team_id:
        raise ValueError("FACTORY_GATE_SURFACE=tracker needs LINEAR_API_KEY and LINEAR_TEAM_ID")
    tracker = LinearTracker(LinearClient(cfg.linear_api_key), cfg.linear_team_id, cfg.linear_project_id)
    return TrackerQueue(tracker, state_path=cfg.runs_dir / "_tracker.json")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
