"""Repository access clauses for design sessions, and the decompose retry staying in its stage. No network."""
from types import SimpleNamespace

from conftest import make_config

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
    ctx.event("run.created", {"outcome_id": "o", "title": "t"})
    outcome = examples["outcome"]
    bad = dict(examples["decomposition"], coverage=[])            # fails metric_coverage once
    good = examples["decomposition"]
    produced = iter([bad, good])

    class Driver:
        def run(self, **kw):
            d = next(produced)
            import json
            return SimpleNamespace(session_id="s", agent_id="a", agent_version=1, outcome_result="satisfied", outcome_iterations=1,
                                   list_cost_usd=1.0, stop_reason="end_turn", events_seen=1, custom_tool_calls=[],
                                   outputs={"decomposition.json": json.dumps(d).encode(), "decomposition.md": b"# d"})

    ctx.driver = Driver()
    decomposition, shas = dec.run_decompose(ctx, outcome)
    assert decomposition["coverage"]
    kinds = [e.kind for e in ctx.store.events(ctx.run_id)]
    assert kinds.count("stage.entered") == 1 and ctx.store.state(ctx.run_id).stage == RunStage.DECOMPOSING
