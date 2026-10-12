"""Tracker gate surface against a fake Linear GraphQL client. No network.

The fake implements `execute(query, variables)` and dispatches on the operation name, keeping issues,
labels, and comments in memory, so the test exercises the real `LinearTracker` query/mutation shapes
and the whole `TrackerQueue` mapping (run -> project + issue, work item -> child issue with mirrored
state, gate -> comment + label, decision <- label + newest human comment with copied hashes).
"""
import re

import pytest

from factory.gates import ArtifactRef, Decision, Gate, GateKind, Question, StaleDecision
from factory.state import WorkItemStage
from factory.surfaces.tracker import (DECISION_LABELS, LABEL_GATE_OPEN, LABEL_QUESTION, LinearTracker, TrackerQueue)

_OP = re.compile(r"\s*(query|mutation)\s+(\w+)")


class FakeLinear:
    """Just enough of Linear: one team with standard states, labels created on demand, issues and threaded comments."""

    def __init__(self, me="factory-bot"):
        self.me = me
        self.states = [{"id": "st_backlog", "name": "Backlog", "type": "backlog"},
                       {"id": "st_todo", "name": "Todo", "type": "unstarted"},
                       {"id": "st_prog", "name": "In Progress", "type": "started"},
                       {"id": "st_review", "name": "In Review", "type": "started"},
                       {"id": "st_done", "name": "Done", "type": "completed"},
                       {"id": "st_cancel", "name": "Canceled", "type": "canceled"}]
        self.labels = {}          # id -> name
        self.projects = {}
        self.issues = {}          # id -> {title, description, parentId, stateId, labelIds}
        self.comments = {}        # id -> {issueId, body, parentId, user, createdAt}
        self.n = 0
        self.ops = []

    def _id(self, prefix):
        self.n += 1
        return f"{prefix}_{self.n}"

    # -- what a human does in the UI
    def human_label(self, issue_id, name, who="priya"):
        lid = next((i for i, n in self.labels.items() if n == name), None) or self._id("lbl")
        self.labels[lid] = name
        self.issues[issue_id]["labelIds"].append(lid)

    def human_comment(self, issue_id, body, who="priya", parent_id=None):
        cid = self._id("cmt")
        self.comments[cid] = {"issueId": issue_id, "body": body, "parentId": parent_id, "user": who, "createdAt": f"2026-10-10T00:00:{self.n:02d}Z"}
        return cid

    def issue_labels(self, issue_id):
        return [self.labels[i] for i in self.issues[issue_id]["labelIds"]]

    def state_name(self, issue_id):
        sid = self.issues[issue_id]["stateId"]
        return next(s["name"] for s in self.states if s["id"] == sid)

    # -- GraphQL
    def execute(self, query, variables=None):
        op = _OP.match(query).group(2)
        v = variables or {}
        self.ops.append(op)
        if op == "TeamContext":
            return {"team": {"id": v["teamId"], "key": "FAC", "states": {"nodes": self.states},
                             "labels": {"nodes": [{"id": i, "name": n} for i, n in self.labels.items()]}}}
        if op == "IssueLabelCreate":
            lid = self._id("lbl"); self.labels[lid] = v["input"]["name"]
            return {"issueLabelCreate": {"issueLabel": {"id": lid, "name": v["input"]["name"]}}}
        if op == "ProjectCreate":
            pid = self._id("prj"); self.projects[pid] = v["input"]
            return {"projectCreate": {"project": {"id": pid, "url": f"https://linear.app/t/project/{pid}"}}}
        if op == "IssueCreate":
            iid = self._id("iss")
            self.issues[iid] = {**v["input"], "stateId": "st_backlog", "labelIds": []}
            return {"issueCreate": {"issue": {"id": iid, "identifier": f"FAC-{self.n}", "url": f"https://linear.app/t/issue/FAC-{self.n}"}}}
        if op == "IssueUpdate":
            self.issues[v["id"]].update({k: val for k, val in v["input"].items()})
            return {"issueUpdate": {"success": True}}
        if op == "IssueLabels":
            iss = self.issues[v["id"]]
            return {"issue": {"url": f"https://linear.app/t/issue/{v['id']}",
                              "labels": {"nodes": [{"id": i, "name": self.labels[i]} for i in iss["labelIds"]]}}}
        if op == "CommentCreate":
            cid = self._id("cmt")
            self.comments[cid] = {"issueId": v["input"]["issueId"], "body": v["input"]["body"], "parentId": v["input"].get("parentId"),
                                  "user": self.me, "createdAt": f"2026-10-10T00:00:{self.n:02d}Z"}
            return {"commentCreate": {"comment": {"id": cid, "url": "u"}}}
        if op == "IssueComments":
            nodes = [{"id": cid, "body": c["body"], "createdAt": c["createdAt"], "parent": {"id": c["parentId"]} if c["parentId"] else None,
                      "user": {"name": c["user"], "displayName": c["user"], "isMe": c["user"] == self.me}}
                     for cid, c in self.comments.items() if c["issueId"] == v["id"]]
            return {"issue": {"comments": {"nodes": nodes}}}
        raise AssertionError(f"unexpected op {op}")


@pytest.fixture
def surface(tmp_path):
    fake = FakeLinear()
    q = TrackerQueue(LinearTracker(fake, "team_1"), state_path=tmp_path / "_tracker.json")
    return fake, q


def _gate(run_id="run_abc1234567", kind=GateKind.DECOMPOSITION_REVIEW, wid=None):
    return Gate(run_id=run_id, kind=kind,
                artifacts=(ArtifactRef("decomposition.json", "a" * 64, "file:///p#work-items"), ArtifactRef("decomposition.md", "b" * 64)),
                assignees=("priya.n",), summary="4 work items, all 3 metrics covered.",
                options=(Decision.APPROVE, Decision.REQUEST_CHANGES, Decision.REJECT, Decision.APPROVE_SUBSET), work_item_id=wid)


def test_run_and_work_items_map_to_project_and_issues(surface, examples):
    fake, q = surface
    run_issue = q.register_run("run_abc1234567", examples["outcome"])
    assert len(fake.projects) == 1 and fake.issues[run_issue]["title"].startswith("[factory] Cut involuntary churn")
    issues = q.register_work_items("run_abc1234567", examples["decomposition"])
    assert set(issues) == {w["work_item_id"] for w in examples["decomposition"]["work_items"]}
    for wid, iid in issues.items():
        assert fake.issues[iid]["parentId"] == run_issue and fake.state_name(iid) == "Backlog"
    # registering twice is idempotent
    assert q.register_work_items("run_abc1234567", examples["decomposition"]) == issues
    assert len(fake.issues) == 1 + len(issues)
    # state mirrors WorkItemStage
    iid = issues["wi-smart-retries"]
    for stage, expected in (("spec_drafting", "Todo"), ("implementing", "In Progress"), ("cr_open", "In Review"),
                            ("merged", "In Review"), ("deployed_dev", "Done"), ("closed", "Canceled")):
        q.mirror_work_item("run_abc1234567", "wi-smart-retries", stage)
        assert fake.state_name(iid) == expected, stage
    q.mirror_work_item("run_abc1234567", "wi-smart-retries", WorkItemStage.BLOCKED.value)
    assert fake.state_name(iid) == "Todo" and "factory:blocked" in fake.issue_labels(iid)
    q.mirror_work_item("run_abc1234567", "wi-smart-retries", "implementing")
    assert "factory:blocked" not in fake.issue_labels(iid)
    q.mirror_work_item("run_abc1234567", "wi-unknown", "implementing")   # unknown items are ignored
    # the mapping survives a restart
    q2 = TrackerQueue(LinearTracker(fake, "team_1"), state_path=q.state_path)
    assert q2.runs["run_abc1234567"].item_issues == issues


def test_gate_comment_carries_hashes_and_decision_reads_label_and_comment(surface, examples):
    fake, q = surface
    run_issue = q.register_run("run_abc1234567", examples["outcome"])
    gate = _gate()
    q.open(gate)
    body = next(c["body"] for c in fake.comments.values() if c["issueId"] == run_issue)
    assert f"gate_id={gate.gate_id}" in body and ("a" * 64 + "," + "b" * 64) in body
    assert "`decomposition.json` · sha256" in body and "[read](file:///p#work-items)" in body
    assert "`factory:approve`" in body and LABEL_GATE_OPEN in fake.issue_labels(run_issue)
    assert q.decision(gate.gate_id) is None                                    # nothing decided yet

    fake.human_comment(run_issue, "Suspension timing isn't here. When do we actually suspend?")
    fake.human_label(run_issue, "factory:request-changes")
    d = q.decision(gate.gate_id)
    assert d.decision == Decision.REQUEST_CHANGES and d.decided_by == "priya"
    assert "Suspension timing" in d.comments
    assert sorted(d.artifact_shas) == ["a" * 64, "b" * 64]                     # copied from the gate comment
    assert LABEL_GATE_OPEN not in fake.issue_labels(run_issue)
    assert not any(lb in fake.issue_labels(run_issue) for lb in DECISION_LABELS)
    assert q.decision(gate.gate_id) is d                                       # cached
    fb = q.render_feedback_md(gate.gate_id)
    assert fb.startswith("# Human feedback from the tracker") and "@priya" in fb and "Suspension timing" in fb
    assert q.gate_url(gate.gate_id).startswith("https://linear.app/")


def test_approve_subset_parses_spec_ids_and_stale_hashes_are_refused(surface, examples):
    fake, q = surface
    run_issue = q.register_run("run_abc1234567", examples["outcome"])
    gate = _gate(kind=GateKind.SPEC_READINESS)
    q.open(gate)
    fake.human_comment(run_issue, "Looks good except the emails.\napprove: spec_smart-retries, spec_at-risk-admin")
    fake.human_label(run_issue, "factory:approve-subset")
    d = q.decision(gate.gate_id)
    assert d.decision == Decision.APPROVE_SUBSET and d.approved_subset == ("spec_smart-retries", "spec_at-risk-admin")

    # A second gate on the same issue: the human's stale label from before is cleared on open, and a decision
    # whose gate comment was edited to other hashes is refused.
    gate2 = _gate(kind=GateKind.SPEC_READINESS)
    q.open(gate2)
    assert not any(lb in fake.issue_labels(run_issue) for lb in DECISION_LABELS)
    cid = q.gates[gate2.gate_id].comment_id
    fake.comments[cid]["body"] = fake.comments[cid]["body"].replace("a" * 64, "c" * 64)
    fake.human_label(run_issue, "factory:approve")
    with pytest.raises(StaleDecision):
        q.decision(gate2.gate_id)


def test_deploy_failure_gate_goes_on_the_work_item_issue(surface, examples):
    fake, q = surface
    q.register_run("run_abc1234567", examples["outcome"])
    issues = q.register_work_items("run_abc1234567", examples["decomposition"])
    gate = _gate(kind=GateKind.DEPLOY_FAILURE, wid="wi-smart-retries")
    q.open(gate)
    assert q.gates[gate.gate_id].issue_id == issues["wi-smart-retries"]
    fake.human_label(issues["wi-smart-retries"], "factory:reject")
    assert q.decision(gate.gate_id).decision == Decision.REJECT


def test_questions_are_threaded_comments(surface, examples):
    fake, q = surface
    run_issue = q.register_run("run_abc1234567", examples["outcome"])
    qu = Question(run_id="run_abc1234567", asked_by="decomposer", question="Stripe Smart Retries or our own schedule?",
                  why_it_matters="adds a week", default_if_unanswered="Stripe", blocking=True, options=("Stripe", "own"))
    q.ask(qu)
    assert LABEL_QUESTION in fake.issue_labels(run_issue)
    assert q.answer(qu.question_id) is None
    cid = q.questions[qu.question_id][2]
    fake.human_comment(run_issue, "Stripe Smart Retries", parent_id=cid)
    a = q.answer(qu.question_id)
    assert a.answer == "Stripe Smart Retries" and a.answered_by == "priya"
    assert LABEL_QUESTION not in fake.issue_labels(run_issue)
    # a non-threaded reply that mentions the id also counts
    qu2 = Question(run_id="run_abc1234567", asked_by="spec_writer", question="Cascade on delete?", why_it_matters="",
                   default_if_unanswered="yes", blocking=True)
    q.ask(qu2)
    fake.human_comment(run_issue, f"{qu2.question_id}: yes, cascade")
    assert q.answer(qu2.question_id).answer.endswith("yes, cascade")
    # notifier hooks: a change request is noted on its work item's issue, an escalation on the run issue
    issues = q.register_work_items("run_abc1234567", examples["decomposition"])
    q.change_request_opened("run_abc1234567", "spec_smart-retries", "https://github.com/x/pull/1")
    q.escalated("run_abc1234567", "budget")
    notes = [(c["issueId"], c["body"]) for c in fake.comments.values() if "factory-note" in c["body"]]
    assert [i for i, b in notes if "https://github.com/x/pull/1" in b] == [issues["wi-smart-retries"]]
    assert [i for i, b in notes if "Escalated" in b] == [run_issue]
