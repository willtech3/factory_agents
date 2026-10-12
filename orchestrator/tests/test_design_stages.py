"""Repository access clauses for design sessions, and the decompose retry staying in its stage. No network."""
from types import SimpleNamespace

from conftest import make_config
from support.sessions import stage_result

from factory.forge.github import GitHubForge
from factory.forge.gitlab import GitLabForge
from factory.gates import InMemoryQueue, LogNotifier
from factory.stages import RunContext
from factory.stages.common import NO_REPO_RUBRIC_NOTE, rubric_text
from factory.stages.decompose import REPO_ABSENT, REPO_MOUNTED, repository_access
from factory.state import Policy, RunStage
from factory.store import RunStore

OUTCOME = {"target_repositories": [{"url": "https://github.com/w/app", "default_branch": "main"}]}


def _ctx(tmp_path, root, forge):
    return RunContext(cfg=make_config(root, tmp_path), store=RunStore(tmp_path), driver=None, queue=InMemoryQueue(),
                      notifier=LogNotifier(), forge=forge, policy=Policy(), run_id="run_design00001")


def test_repository_access_per_forge(tmp_path, root):
    ctx = _ctx(tmp_path, root, None)
    assert repository_access(ctx, OUTCOME) == (None, REPO_ABSENT, False)
    assert rubric_text(ctx, "decomposition", False).startswith(NO_REPO_RUBRIC_NOTE)
    assert not rubric_text(ctx, "decomposition", True).startswith(NO_REPO_RUBRIC_NOTE)
    ctx.forge = GitHubForge("https://github.com", "ghp", "w/app")
    res, clause, ok = repository_access(ctx, OUTCOME)
    assert ok and clause == REPO_MOUNTED and res["type"] == "github_repository" and res["checkout"] == {"type": "branch", "name": "main"}
    ctx.forge = GitLabForge("https://gitlab.example.internal", "glpat", "g/app")
    res, clause, ok = repository_access(ctx, OUTCOME)
    assert ok and res is None and "Authorization: Bearer $GITLAB_TOKEN" in clause and "do not modify" in clause


def test_decompose_retry_does_not_reenter_the_stage(tmp_path, root, examples):
    """The automatic deterministic-check retry calls run_decompose again while the run is already DECOMPOSING;
    a second stage.entered would be an illegal DECOMPOSING -> DECOMPOSING transition on replay."""
    from factory.stages import decompose as dec

    ctx = _ctx(tmp_path, root, None)
    ctx.event("run.created", {"policy": {"preview_mode": "local"}, "budget_usd": 50, "outcome_id": "o", "title": "t"})
    outcome = examples["outcome"]
    bad = dict(examples["decomposition"], coverage=[])            # fails metric_coverage once
    good = examples["decomposition"]
    produced = iter([bad, good])

    class Driver:
        def run(self, **kw):
            d = next(produced)
            import json
            return stage_result({"decomposition.json": json.dumps(d).encode(), "decomposition.md": b"# d"}, cost=1.0)

    ctx.driver = Driver()
    decomposition, shas = dec.run_decompose(ctx, outcome)
    assert decomposition["coverage"]
    kinds = [e.kind for e in ctx.store.events(ctx.run_id)]
    assert kinds.count("stage.entered") == 1 and ctx.store.state(ctx.run_id).stage == RunStage.DECOMPOSING


def test_decompose_retries_once_after_owner_feedback(tmp_path, root, examples):
    """A re-run after the owner's feedback still gets its one automatic retry; the retry carries both texts."""
    from factory.stages import decompose as dec

    ctx = _ctx(tmp_path, root, None)
    ctx.event("run.created", {"policy": {"preview_mode": "local"}, "budget_usd": 50, "outcome_id": "o", "title": "t"})
    bad = dict(examples["decomposition"], coverage=[])
    produced, feedback = iter([bad, examples["decomposition"]]), []

    class Driver:
        def run(self, **kw):
            import json
            feedback.append(kw["uploads"].get("prior/feedback.md", b"").decode())
            return stage_result({"decomposition.json": json.dumps(next(produced)).encode(), "decomposition.md": b"# d"}, cost=1.0)

    ctx.driver = Driver()
    decomposition, _ = dec.run_decompose(ctx, examples["outcome"], "Split the importer.", examples["decomposition"])
    assert decomposition["coverage"] and len(feedback) == 2
    assert feedback[1].startswith("Split the importer.") and "metric_coverage" in feedback[1]

def test_driver_interrupts_sessions_still_in_flight(tmp_path, root):
    """A session the driver started and has not finished gets user.interrupt when the orchestrator is stopped."""
    from factory.sessions import SHORT_CALL_TIMEOUT, SessionDriver

    sent, finished = [], []

    class Events:
        def send(self, session_id, events, timeout=None):
            assert timeout == SHORT_CALL_TIMEOUT          # a hung interrupt must not block the shutdown for ten minutes
            sent.append((session_id, events[0]["type"]))

    class Sessions:
        events = Events()

        def retrieve(self, session_id, timeout=None):
            return SimpleNamespace(usage=SimpleNamespace(list_cost=SimpleNamespace(amount="233")))

    class Client:
        beta = type("B", (), {"sessions": Sessions()})()

    d = SessionDriver(make_config(root, tmp_path), client=Client(), on_session_finished=finished.append)
    d.active["sesn_a"] = {"agent_id": "agent_x", "agent_version": 2, "agent_key": "spec_judge", "stage": "judge", "run_id": "run_1", "work_item_id": None, "title": "t"}
    d.active["sesn_b"] = {"agent_id": "agent_y", "agent_version": 1, "agent_key": "implementer", "stage": "implement", "run_id": "run_1", "work_item_id": "wi-x", "title": "u"}
    assert d.interrupt_active() == ["sesn_a", "sesn_b"]
    assert sent == [("sesn_a", "user.interrupt"), ("sesn_b", "user.interrupt")] and d.active == {}
    # an interrupted session still finished: its spend is reported so the run total counts it (post-mortem issue 16)
    assert [(f["session_id"], f["stop_reason"], f["list_cost_usd"], f["stage"], f["work_item_id"]) for f in finished] == \
        [("sesn_a", "interrupted", 2.33, "judge", None), ("sesn_b", "interrupted", 2.33, "implement", "wi-x")]


def test_design_sessions_get_the_platform_contract_skill_by_path(tmp_path, root, examples):
    """Issue 12: the brief said "read the skill first" with no location, so decomposers and judges searched the
    filesystem. Every stage now uploads skills/platform-contract/SKILL.md and names the path in the kickoff."""
    import json
    from factory.stages import decompose as dec, specify as spc
    from factory.stages.common import SKILL_UPLOAD_PATH

    ctx = _ctx(tmp_path, root, None)
    ctx.event("run.created", {"policy": {"preview_mode": "local"}, "budget_usd": 50, "outcome_id": "o", "title": "t"})
    calls = []

    class Driver:
        def run(self, **kw):
            calls.append(kw)
            d = examples["decomposition"] if kw["agent_key"] == "decomposer" else (
                examples["spec_smart_retries"] if kw["agent_key"] == "spec_writer" else examples["verdict_round2"])
            name = {"decomposer": "decomposition.json", "spec_writer": "spec.json", "spec_judge": "verdict.json"}[kw["agent_key"]]
            return stage_result({name: json.dumps(d).encode(), "decomposition.md": b"# d", "spec.md": b"# s"}, cost=1.0)

    ctx.driver = Driver()
    dec.run_decompose(ctx, examples["outcome"])
    item = examples["decomposition"]["work_items"][0]
    spc._write_one(ctx, item["work_item_id"], 1,
                   spc._writer_session(ctx, examples["outcome"], examples["decomposition"], item, {}, None, None, 1))
    spc._judge(ctx, examples["outcome"], examples["decomposition"], {"spec_x": examples["spec_smart_retries"]}, 1, None)
    assert [c["agent_key"] for c in calls] == ["decomposer", "spec_writer", "spec_judge"]
    expected = (root / "skills" / "platform-contract" / "SKILL.md").read_bytes()
    for c in calls:
        assert c["uploads"][SKILL_UPLOAD_PATH] == expected
        assert "/mnt/session/uploads/skills/platform-contract/SKILL.md" in c["description"] and "do not search the filesystem" in c["description"]
    # without a skill file the kickoff says so instead of sending agents looking
    from factory.stages.common import platform_skill
    ctx.cfg = make_config(root, tmp_path, skills_dir=tmp_path / "no-skills")
    uploads, clause = platform_skill(ctx)
    assert uploads == {} and "do not search" in clause and "No platform-contract skill" in clause
