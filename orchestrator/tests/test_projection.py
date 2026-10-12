"""ADR-0017: the Console reads the run as its steps table shows it: pipeline steps with their sessions and cost, the
sessions still live, and what a resume would continue, follow, start again and reuse. No network."""
import dataclasses

import pytest
from conftest import make_config
from fastapi.testclient import TestClient
from test_implement_bookkeeping import RUN, TEST_ID, _ctx, _spec
from test_round_replay import build, dying_at
from typer.testing import CliRunner

from factory import cli
from factory.projection import project, render
from factory.stages.implement import run_implement_one
from factory.web.app import create_app

URL = "https://github.com/w/app"


def _view(ctx, alive=False):
    return project(ctx.store.steps(ctx.run_id), ctx.store.state(ctx.run_id).finished_sessions, ctx.store.events(ctx.run_id),
                   controller_alive=alive)


def test_a_finished_build_is_a_tree_of_steps_with_their_sessions_and_cost(tmp_path, root, examples):
    ctx, verifier = _ctx(tmp_path, root, examples, mapping=[TEST_ID])
    run_implement_one(ctx, _spec(examples), URL, "main", verifier, {})
    v = _view(ctx)
    sid = _spec(examples)["spec_id"]
    rows = {r["key"]: r for r in v["pipeline"]}
    assert set(rows) == {f"implement/{sid}/r1", f"implement/{sid}/r1/author", f"implement/{sid}/judge#0"}   # no /name bookkeeping
    assert rows[f"implement/{sid}/r1/author"]["parent"] == f"implement/{sid}/r1" and rows[f"implement/{sid}/r1/author"]["depth"] == 1
    author, round1, judge = rows[f"implement/{sid}/r1/author"], rows[f"implement/{sid}/r1"], rows[f"implement/{sid}/judge#0"]
    assert len(author["sessions"]) == 1 and len(round1["sessions"]) == 1 and len(judge["sessions"]) == 1   # implementer; reviewer; judge
    assert author["cost_usd"] == 3.3 and round1["own_cost_usd"] == 2.6 and round1["cost_usd"] == 5.9 and judge["cost_usd"] == 0.9
    assert v["cost"] == {"total_usd": 6.8, "by_stage": {"implement": 3.3, "review": 2.6, "merge_judge": 0.9}}
    assert v["cost"]["total_usd"] == round(ctx.store.state(ctx.run_id).list_cost_usd, 2)
    assert v["live"] == [] and v["resume"] == {"continues_at": [], "follows": [], "restarts": [], "reuses": 3}
    agents = {s["agent_key"] for s in v["sessions"]}
    assert agents == {"implementer", "reviewer", "merge_judge"}


@pytest.mark.parametrize("alive", [False, True], ids=["no-controller", "controller-alive"])
def test_a_crashed_build_shows_its_live_session_and_where_a_resume_continues(tmp_path, root, examples, alive):
    """The controller died right after the reviewer's session started: that session works on, its round is unfinished,
    and the projection says a resume follows that session and continues at that round."""
    ctx, verifier = build(tmp_path, root, examples)
    started = [n for n, e in enumerate(_reference_events(tmp_path, root, examples), 1) if e == "session.started"]
    with pytest.raises(SystemExit):
        run_implement_one(dying_at(ctx, started[1], True), _spec(examples), URL, "main", verifier, {})
    v = _view(ctx, alive)
    sid = _spec(examples)["spec_id"]
    [live] = v["live"]
    assert live["agent_key"] == "reviewer" and live["following"] is alive and live["pipeline_step"] == f"implement/{sid}/r1"
    assert v["resume"]["follows"] == [live["session_id"]] and v["resume"]["continues_at"] == [f"implement/{sid}/r1"]
    assert v["resume"]["reuses"] == 1 and v["resume"]["restarts"] == []
    text = "\n".join(render(v))
    assert "A resume continues at: implement/" in text and live["session_id"] in text
    # a stop interrupts it instead: the resume starts that work again
    ctx.event("session.finished", {"session_id": live["session_id"], "stop_reason": "interrupted", "list_cost_usd": 0.1,
                                   "agent_key": "reviewer"}, stage="review", actor="platform")
    v = _view(ctx, alive)
    assert v["live"] == [] and v["resume"]["restarts"] == [live["key"]] and v["resume"]["follows"] == []


def _reference_events(tmp_path, root, examples):
    ctx, verifier = build(tmp_path / "reference", root, examples)
    before = len(ctx.store.events(ctx.run_id))
    run_implement_one(ctx, _spec(examples), URL, "main", verifier, {})
    return [e.kind for e in ctx.store.events(ctx.run_id)[before:]]


def test_the_console_and_the_cli_read_the_projection(tmp_path, root, examples, monkeypatch):
    ctx, verifier = _ctx(tmp_path, root, examples, mapping=[TEST_ID])
    run_implement_one(dataclasses.replace(ctx), _spec(examples), URL, "main", verifier, {})
    cfg = make_config(root, tmp_path / "runs", gate_surface="web")
    with TestClient(create_app(cfg)) as client:
        detail = client.get(f"/api/runs/{RUN}").json()
    assert detail["steps"]["recorded"] and detail["steps"]["cost"]["total_usd"] == 6.8
    assert [r["key"].split("/")[-1] for r in detail["steps"]["pipeline"]] == ["r1", "author", "judge#0"]
    monkeypatch.setenv("FACTORY_RUNS_DIR", str(tmp_path / "runs"))
    out = CliRunner().invoke(cli.app, ["steps", RUN])
    assert out.exit_code == 0 and "judge#0" in out.output and "Finished sessions a replay reuses: 3" in out.output
    as_json = CliRunner().invoke(cli.app, ["steps", RUN, "--json"])
    assert as_json.exit_code == 0 and '"total_usd": 6.8' in as_json.output


def test_a_run_from_before_steps_says_so(tmp_path):
    v = project([], {}, [], controller_alive=False)
    assert not v["recorded"] and render(v) == ["This run has no steps (a controller before ADR-0013 wrote it)."]
