"""ADR-0013 phase 2b: each implement/review round is a step and so is the merge judgement. A controller that dies at any
point of a spec's build leaves rounds the next controller gets back from their steps: no implementer, reviewer or judge
is asked twice, and the verdict is the one an uninterrupted build reaches. The bookkeeping harness's fakes; no network."""
import dataclasses
import threading
from types import SimpleNamespace

import pytest
from test_implement_bookkeeping import TEST_ID, Driver, _ctx, _spec

from factory.stages.implement import run_implement_one

URL = "https://github.com/w/app"


class Platform(Driver):
    """The bookkeeping harness's agents on a platform whose sessions a later controller can follow by id. As in
    `SessionDriver.run`, the step gets a new session's id before the controller records `session.started`."""

    def __init__(self, ctx, examples, mapping):
        super().__init__(ctx, examples, mapping)
        self.sessions, self.attached, self.lock = {}, [], threading.Lock()

    def run(self, **kw):
        if kw.get("attach_session_id"):
            self.attached.append(kw["attach_session_id"])
            return self.sessions[kw["attach_session_id"]]
        on_created = kw.pop("on_created", None)
        with self.lock:
            controller, self.ctx = self.ctx, SimpleNamespace(event=lambda *a, **k: None)   # the platform writes no events
            try:
                res = super().run(**kw)
            finally:
                self.ctx = controller
            self.sessions[res.session_id] = res
        if on_created:
            on_created(res.session_id)
        controller.event("session.started", {"session_id": res.session_id, "agent_id": res.agent_id, "agent_key": kw["agent_key"],
                                             "title": kw["title"]}, stage=kw["stage"], actor="platform", work_item_id=kw.get("work_item_id"))
        return res


def build(tmp_path, root, examples):
    """A two-round build: round 1's checks fail on the host (the test did not pass there), the implementer answers the
    deterministic finding, round 2 goes to the judge."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    ctx, verifier = _ctx(tmp_path, root, examples, mapping=[TEST_ID], passed=())
    ctx.driver = Platform(ctx, examples, [TEST_ID])
    return ctx, verifier


def dying_at(ctx, n, written):
    seen, lock, append = {"n": 0}, threading.Lock(), ctx.event

    def event(*args, **kwargs):
        with lock:
            seen["n"] += 1
            mine = seen["n"]
            if mine == n and not written:
                raise SystemExit(f"controller died before event {n}")
            ev = append(*args, **kwargs)
        if mine == n and written:
            raise SystemExit(f"controller died after event {n}")
        return ev
    ctx.event = event
    return ctx


@pytest.fixture
def reference(tmp_path_factory, root, examples):
    ctx, verifier = build(tmp_path_factory.mktemp("reference"), root, examples)
    before = len(ctx.store.events(ctx.run_id))
    verdict = run_implement_one(ctx, _spec(examples), URL, "main", verifier, {})
    return {"verdict": verdict["result"], "round": verdict["round"], "agents": [c["agent_key"] for c in ctx.driver.calls],
            "events": len(ctx.store.events(ctx.run_id)) - before}


def test_the_reference_build_takes_two_rounds_and_records_each(tmp_path, root, examples, reference):
    ctx, verifier = build(tmp_path, root, examples)
    run_implement_one(ctx, _spec(examples), URL, "main", verifier, {})
    keys = sorted(s.key for s in ctx.store.steps(ctx.run_id) if s.key.startswith("implement/"))
    sid = _spec(examples)["spec_id"]
    assert keys == [f"implement/{sid}/judge#0", f"implement/{sid}/judge#0/name", f"implement/{sid}/r1", f"implement/{sid}/r1/author",
                    f"implement/{sid}/r2", f"implement/{sid}/r2/author"]
    assert reference["agents"] == ["implementer", "reviewer", "implementer", "merge_judge"] and reference["round"] == 2


@pytest.mark.parametrize("written", [False, True], ids=["before-the-write", "after-the-write"])
def test_a_build_interrupted_at_any_event_is_finished_without_asking_anyone_twice(tmp_path, root, examples, reference, written):
    for n in range(1, reference["events"] + 1):
        ctx, verifier = build(tmp_path / f"{n}", root, examples)
        with pytest.raises(SystemExit):
            run_implement_one(dying_at(ctx, n, written), _spec(examples), URL, "main", verifier, {})
        resumed = dataclasses.replace(ctx)                           # a new controller: the same store and platform
        verdict = run_implement_one(resumed, _spec(examples), URL, "main", verifier, {})
        assert (verdict["result"], verdict["round"]) == (reference["verdict"], reference["round"]), n
        assert [c["agent_key"] for c in ctx.driver.calls] == reference["agents"], f"event {n}: an agent was asked twice"


def test_a_finished_build_replayed_asks_nobody_and_returns_its_verdict(tmp_path, root, examples):
    ctx, verifier = build(tmp_path, root, examples)
    first = run_implement_one(ctx, _spec(examples), URL, "main", verifier, {})
    calls, events = len(ctx.driver.calls), len(ctx.store.events(ctx.run_id))
    again = run_implement_one(dataclasses.replace(ctx), _spec(examples), URL, "main", verifier, {})
    assert again == first and len(ctx.driver.calls) == calls and len(ctx.store.events(ctx.run_id)) == events


def test_a_request_for_changes_adds_one_round_with_the_owners_instructions(tmp_path, root, examples):
    """The owner sends the judged round back: round 3 answers their findings with their instructions, gets one more
    review, and a new judge decides; rounds 1 and 2 come back from their steps."""
    ctx, verifier = build(tmp_path, root, examples)
    spec = _spec(examples)
    run_implement_one(ctx, spec, URL, "main", verifier, {})
    verifier.passed = [TEST_ID]
    owner = {**examples["findings_round1"]["findings"][0], "finding_id": "OWNER-1", "severity": "blocking", "status": "open",
             "claim": "the retry window is wrong"}
    corrective = {"decision": "request_changes", "round": 2, "comments": "Use a 48 hour window.",
                  "review_findings": {"findings": [owner]}}
    ctx.event("build.recovery", corrective, spec_id=spec["spec_id"])
    before = len(ctx.driver.calls)
    verdict = run_implement_one(dataclasses.replace(ctx), spec, URL, "main", verifier, {}, recovery=corrective, corrective=corrective)
    asked = ctx.driver.calls[before:]
    assert [c["agent_key"] for c in asked] == ["implementer", "reviewer", "merge_judge"]
    assert b"Use a 48 hour window." in asked[0]["uploads"]["gate2_guidance.md"] and b"OWNER-1" in asked[0]["uploads"]["findings.json"]
    assert verdict["round"] == 3 and verdict["result"] == "ready"
