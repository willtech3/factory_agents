"""Recovering the same run while its controller was stopped idle at Gate 2.

Each test parks a real run: events, store artefacts and a file-queue gate opened by `open_gate` exactly as Gate 2 does,
then the `run.failed` the stop handler records. The recovery runs as a new process would: a fresh store, file queue and
context, with a session driver that refuses any agent. Offline only: no sessions, no forge, no app."""
import fcntl
import json
import threading

import pytest
from conftest import make_config
from typer.testing import CliRunner

from factory import cli, runner
from factory.gates import Decision, GateKind, LogNotifier, StaleDecision
from factory.stages import RunContext, common
from factory.stages.common import gate_decision, open_gate
from factory.state import IllegalTransition, Policy, RunStage
from factory.store import RunStore
from factory.surfaces.filequeue import FileQueue
from support.gate2 import context, DECOMPOSITION, GATE2_OPTIONS, OUTCOME, park, RefuseAgents, RUN, SHA, spec, SPECS, verdict  # noqa: F401


def kinds(ctx):
    return [e.kind for e in ctx.store.events(RUN)]


def after_resume(ctx, n_before):
    return ctx.store.events(RUN)[n_before:]


# ---------------------------------------------------------------------------------------------- the decisions

@pytest.mark.parametrize("result", ["pass", "fail"])
def test_an_approval_lands_on_the_same_gate_with_its_hashes_and_no_agent_runs(tmp_path, root, result):
    gate, shas = park(tmp_path, root, result)
    ctx = context(tmp_path, root)
    ctx.queue.record_decision(gate.gate_id, "approve", "will", "looks right")
    n = len(ctx.store.events(RUN))
    assert runner.resume(ctx) == RunStage.COMPLETED
    new = after_resume(ctx, n)
    assert [e.kind for e in new][:2] == ["run.resumed", "gate.decided"] and "gate.opened" not in [e.kind for e in new]
    resumed, decided = new[0].payload, new[1].payload
    assert resumed["gate_id"] == decided["gate_id"] == gate.gate_id and resumed["artifacts"] == dict(sorted(shas.items()))
    assert sorted(decided["artifact_shas"]) == sorted(shas.values()) and decided["kind"] == gate.kind.value
    assert {e.spec_id for e in new if e.kind == "work_item.stage"} == {"spec_a", "spec_b"}
    assert ctx.driver.calls == [] and ctx.store.state(RUN).stage == RunStage.COMPLETED


def test_approve_subset_recovery_hands_only_those_specs_to_implementation_and_records_the_managed_binding(
        tmp_path, root, monkeypatch):
    gate, _ = park(tmp_path, root, design_only=False)
    seen = {}

    def implement(ctx, outcome, specs, spec_md, spec_verdict, resume=None, guidance=None):
        seen.update(specs=sorted(specs), md=spec_md)
        return RunStage.COMPLETED
    monkeypatch.setattr(runner, "implement_and_integrate", implement)
    ctx = context(tmp_path, root, design_only=False, preview_mode="managed", app_id="app_evergreen",
                  test_profile_sha256="b" * 64, test_vault_ids=("vlt_model",))
    ctx.queue.record_decision(gate.gate_id, "approve_subset", "will", approved_subset=["spec_a"])
    assert runner.resume(ctx) == RunStage.COMPLETED
    assert seen["specs"] == ["spec_a"] and seen["md"]["spec_a"] == "# spec_a"      # spec_md is not filtered, as before
    resumed = next(e.payload for e in ctx.store.events(RUN) if e.kind == "run.resumed")
    assert resumed["preview_mode"] == "managed" and resumed["app_id"] == "app_evergreen" and resumed["test_vaults"] == 1
    approved = [e.spec_id for e in ctx.store.events(RUN) if e.kind == "work_item.stage" and e.payload["stage"] == "spec_approved"]
    assert approved == ["spec_a"]


def test_a_rejection_closes_the_run(tmp_path, root):
    gate, _ = park(tmp_path, root)
    ctx = context(tmp_path, root)
    ctx.queue.record_decision(gate.gate_id, "reject", "will", "not now")
    assert runner.resume(ctx) == RunStage.CLOSED and ctx.driver.calls == []


def test_request_changes_goes_back_to_the_writers_with_routed_feedback_and_the_next_round(tmp_path, root, monkeypatch):
    gate, _ = park(tmp_path, root)
    seen = {}

    class Stop(Exception):
        pass

    def specify(ctx, outcome, decomposition, human_comments, prior_specs, previous_verdict=None, round_offset=0):
        seen.update(comments=human_comments, prior=prior_specs, verdict=previous_verdict, round_offset=round_offset)
        raise Stop
    monkeypatch.setattr(runner, "run_specify", specify)
    ctx = context(tmp_path, root)
    ctx.queue.record_decision(gate.gate_id, "request_changes", "will", "wi-a: name the new model\nkeep the tests in bun")
    with pytest.raises(Stop):
        runner.resume(ctx)
    assert seen["round_offset"] == 2 and seen["verdict"] == verdict()
    assert set(seen["prior"]) == {"wi-a", "wi-b"}
    assert "name the new model" in seen["comments"]["wi-a"] and "name the new model" not in seen["comments"]["wi-b"]
    assert "keep the tests in bun" in seen["comments"]["wi-b"]
    decided = [e.payload for e in ctx.store.events(RUN) if e.kind == "gate.decided"][-1]
    assert decided["gate_id"] == gate.gate_id and decided["decision"] == "request_changes"


def test_the_recovered_controller_waits_for_a_decision_made_later(tmp_path, root, monkeypatch):
    gate, _ = park(tmp_path, root)
    ctx = context(tmp_path, root)
    monkeypatch.setattr(common, "GATE_POLL_SECONDS", 0.05)
    threading.Timer(0.3, lambda: FileQueue(tmp_path / "runs" / "_queue").record_decision(gate.gate_id, "approve", "will")).start()
    assert runner.resume(ctx) == RunStage.COMPLETED and ctx.driver.calls == []


def test_a_decision_with_other_hashes_is_refused_by_the_gates_own_validation(tmp_path, root):
    gate, _ = park(tmp_path, root)
    decisions = tmp_path / "runs" / "_queue" / "decisions.json"
    decisions.write_text(json.dumps({gate.gate_id: {"decision": "approve", "decided_by": "someone", "artifact_shas": ["f" * 64]}}))
    with pytest.raises(StaleDecision):
        runner.resume(context(tmp_path, root))
    assert "gate.decided" not in [e.kind for e in RunStore(tmp_path / "runs").events(RUN) if e.payload.get("gate_id") == gate.gate_id]


def test_a_second_stop_and_recovery_of_the_same_gate_works(tmp_path, root, monkeypatch):
    gate, _ = park(tmp_path, root)

    class Stopped(Exception):
        pass
    monkeypatch.setattr(common.time, "sleep", lambda s: (_ for _ in ()).throw(Stopped()))
    ctx = context(tmp_path, root)
    with pytest.raises(Stopped):
        runner.resume(ctx)
    ctx.event("run.failed", {"reason": "stopped by signal 15", "interrupted_sessions": []})
    monkeypatch.undo()
    ctx = context(tmp_path, root)
    ctx.queue.record_decision(gate.gate_id, "approve", "will")
    assert runner.resume(ctx) == RunStage.COMPLETED
    assert [e.payload["gate_id"] for e in ctx.store.events(RUN) if e.kind == "run.resumed"] == [gate.gate_id, gate.gate_id]


# ---------------------------------------------------------------------------------------------- refusals, before anything is written

def refused(tmp_path, root, match, **cfg):
    ctx = context(tmp_path, root, **cfg)
    n = len(ctx.store.events(RUN))
    with pytest.raises(RuntimeError, match=match):
        runner.resume(ctx)
    assert len(ctx.store.events(RUN)) == n, "a refusal writes nothing"
    return ctx


def test_a_missing_presented_artifact_is_refused(tmp_path, root):
    park(tmp_path, root)
    (tmp_path / "runs" / RUN / "artifacts" / "specify" / "final-spec_b.json").unlink()
    refused(tmp_path, root, "final-spec_b.json, presented at Gate 2 .* is missing")


@pytest.mark.parametrize("stage, name", [("specify", "final-spec_a.json"), ("judge", "final-verdict.json")])
def test_a_changed_presented_artifact_is_refused(tmp_path, root, stage, name):
    park(tmp_path, root)
    path = tmp_path / "runs" / RUN / "artifacts" / stage / name
    path.write_bytes(path.read_bytes().replace(b"\n", b"\n ", 1))
    refused(tmp_path, root, "no longer matches the sha256")


def test_a_gate_record_that_differs_from_the_log_is_refused(tmp_path, root):
    gate, _ = park(tmp_path, root)
    gates = tmp_path / "runs" / "_queue" / "gates.json"
    record = json.loads(gates.read_text())
    record[gate.gate_id]["artifacts"][0]["sha256"] = "0" * 64
    gates.write_text(json.dumps(record))
    refused(tmp_path, root, "differs from the gate the run opened")


def test_a_controller_killed_at_gate_2_before_it_recorded_anything_is_taken_over(tmp_path, root):
    """No run.failed: the process was killed. Nobody holds the run's lock (`factory resume` takes it), so the resumed
    controller waits on that same gate."""
    gate, _ = park(tmp_path, root, stop=False)
    ctx = context(tmp_path, root)
    ctx.queue.record_decision(gate.gate_id, "approve", "will")
    n = len(ctx.store.events(RUN))
    assert runner.resume(ctx) == RunStage.COMPLETED and ctx.driver.calls == []
    assert "gate.opened" not in [e.kind for e in after_resume(ctx, n)]


def test_a_decision_the_dead_controller_had_not_acted_on_is_acted_on(tmp_path, root):
    gate, _ = park(tmp_path, root, stop=False)
    ctx = context(tmp_path, root)
    ctx.queue.record_decision(gate.gate_id, "approve", "will")
    gate_decision(ctx, gate.gate_id, gate.kind)                 # decided, then the controller died before acting on it
    ctx.event("run.failed", {"reason": "crash", "interrupted_sessions": []})
    assert runner.resume(ctx) == RunStage.COMPLETED and ctx.driver.calls == []
    events = ctx.store.events(RUN)
    assert [e.payload["gate_id"] for e in events if e.kind == "gate.decided"].count(gate.gate_id) == 1
    assert {e.spec_id for e in events if e.kind == "work_item.stage" and e.payload["stage"] == "spec_approved"} == {"spec_a", "spec_b"}


@pytest.mark.parametrize("after", [("session.started", {"session_id": "s", "agent_key": "spec_writer"}),
                                   ("run.failed", {"reason": "stopped by signal 15", "interrupted_sessions": ["sesn_live"]})])
def test_anything_but_an_idle_stop_after_the_gate_is_refused(tmp_path, root, after):
    park(tmp_path, root, stop=False)
    ctx = context(tmp_path, root)
    ctx.event(after[0], after[1])
    if after[0] != "run.failed":
        ctx.event("run.failed", {"reason": "stopped by signal 15", "interrupted_sessions": []})
    refused(tmp_path, root, "follows Gate 2")


def test_a_run_that_stopped_elsewhere_takes_no_gate_2_path(tmp_path, root):
    ctx = context(tmp_path, root)
    ctx.event("run.created", {"outcome_id": "o", "title": "t", "policy": {"preview_mode": "local"}, "budget_usd": 50}, stage="intake")
    ctx.store.put_artifact(RUN, "intake", "outcome.json", json.dumps(OUTCOME).encode())
    ctx.event("stage.entered", {"stage": "decomposing"}, stage="decompose")
    ctx.event("stage.entered", {"stage": "gate_1_decomposition"}, stage="gate_1")
    open_gate(ctx, GateKind.DECOMPOSITION_REVIEW, {"decomposition.json": SHA}, "plan", ["will"], (Decision.APPROVE, Decision.REJECT))
    ctx.event("run.failed", {"reason": "stopped by signal 15", "interrupted_sessions": []})
    refused(tmp_path, root, "stopped before Gate 1 approved its decomposition")


def test_a_held_controller_lock_is_refused(tmp_path, root):
    park(tmp_path, root)
    with (tmp_path / "runs" / RUN / "controller.lock").open("a") as other:      # another process's lock
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ctx = context(tmp_path, root)
        n = len(ctx.store.events(RUN))
        with pytest.raises(runner.RunLocked):
            runner.resume(ctx)
        assert len(ctx.store.events(RUN)) == n


def test_managed_recovery_needs_the_approved_recipe_fingerprint(tmp_path, root):
    park(tmp_path, root, design_only=False)
    refused(tmp_path, root, "approved recipe fingerprint", design_only=False, preview_mode="managed", test_vault_ids=("vlt_model",))
    refused(tmp_path, root, "FACTORY_MANAGED_VERIFY_BUDGET_USD", design_only=False, preview_mode="managed",
            test_profile_sha256="b" * 64, managed_verify_budget_usd=0.0)


def test_a_budget_change_at_a_gate_recovery_raises_the_cap_and_waits_on_the_same_gate(tmp_path, root):
    gate, _ = park(tmp_path, root)
    ctx = context(tmp_path, root)
    ctx.queue.record_decision(gate.gate_id, "approve", "will")
    assert runner.resume(ctx, budget_usd=99) == RunStage.COMPLETED
    assert ctx.store.state(RUN).budget_usd == 99 and "gate.opened" not in [e.kind for e in ctx.store.events(RUN)[-8:]]


def test_a_run_moved_to_managed_verification_never_resumes_without_it(tmp_path, root, monkeypatch):
    gate, _ = park(tmp_path, root, design_only=False)
    monkeypatch.setattr(runner, "implement_and_integrate", lambda ctx, *a, **k: RunStage.IMPLEMENTING)
    ctx = context(tmp_path, root, design_only=False, preview_mode="managed", test_profile_sha256="b" * 64)
    ctx.queue.record_decision(gate.gate_id, "approve", "will")
    runner.resume(ctx)
    ctx.event("stage.entered", {"stage": "implementing"}, stage="implement")
    ctx.event("run.failed", {"reason": "crash", "interrupted_sessions": []})
    refused(tmp_path, root, "never with local or platform execution", design_only=False, preview_mode="local")


def test_a_failed_run_still_cannot_jump_to_other_stages(tmp_path, root):
    park(tmp_path, root)
    ctx = context(tmp_path, root)
    before = ctx.store.state(RUN).last_seq
    with pytest.raises(IllegalTransition):                                       # refused when written, so nothing is
        ctx.event("run.resumed", {"stage": "completed"})                          # only a stage the run can stop in
    assert ctx.store.state(RUN).last_seq == before                                # appended and the run stays readable


def test_implementation_guard_remembers_a_hosted_upgrade(tmp_path, root):
    from factory.stages.implement import _require_recorded_preview_mode
    gate, _ = park(tmp_path, root)
    ctx = context(tmp_path, root, preview_mode="local")
    ctx.event("run.resumed", {"stage": "gate_2_spec_readiness", "gate_id": gate.gate_id,
                              "preview_mode": "managed"})
    with pytest.raises(RuntimeError, match="hosted sandbox"):
        _require_recorded_preview_mode(ctx)


# ---------------------------------------------------------------------------------------------- the command line

@pytest.fixture
def cli_env(tmp_path, root, monkeypatch):
    """`factory resume` against the parked run, with configuration and context built offline."""
    made = {}

    def load():
        return make_config(root, tmp_path / "runs")

    def ctx_for(cfg, outcome, run_id, **kw):
        made["ctx"] = RunContext(cfg=cfg, store=RunStore(cfg.runs_dir), driver=RefuseAgents(), queue=FileQueue(cfg.runs_dir / "_queue"),
                                 notifier=LogNotifier(), forge=None, policy=Policy(design_only=True), run_id=run_id)
        return made["ctx"]
    monkeypatch.setattr(cli.cfgmod, "load", load)
    monkeypatch.setattr(cli.cfgmod, "missing", lambda cfg, design_only=False: [])
    monkeypatch.setattr(cli, "_context", ctx_for)
    return made


def test_cli_resume_recovers_the_gate_with_the_managed_options(tmp_path, root, cli_env):
    gate, _ = park(tmp_path, root)
    FileQueue(tmp_path / "runs" / "_queue").record_decision(gate.gate_id, "approve", "will")
    out = CliRunner().invoke(cli.app, ["resume", RUN, "--no-forge", "--preview", "managed", "--app-id", "app_evergreen",
                                       "--test-profile-sha256", "b" * 64, "--test-vault", "vlt_model"])
    assert out.exit_code == 0, out.output
    assert "preview=managed" in out.output and f"run {RUN} finished in state completed" in out.output
    assert cli_env["ctx"].cfg.app_id == "app_evergreen" and cli_env["ctx"].cfg.test_vault_ids == ("vlt_model",)


def test_cli_refusals_write_nothing(tmp_path, root, cli_env):
    park(tmp_path, root)
    store = RunStore(tmp_path / "runs")
    n = len(store.events(RUN))
    out = CliRunner().invoke(cli.app, ["resume", RUN, "--no-forge", "--preview", "managed", "--test-vault", "vlt_model"])
    assert out.exit_code == 1 and "approved recipe fingerprint" in out.output
    with (tmp_path / "runs" / RUN / "controller.lock").open("a") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        out = CliRunner().invoke(cli.app, ["resume", RUN, "--no-forge"])
    assert out.exit_code == 1 and "another controller holds run" in out.output
    assert len(store.events(RUN)) == n
