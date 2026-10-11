"""Onboarding an app without a browser UI (an API, bot, webhook, worker, CLI or library).

The cases replay what a hosted run of a Slack-bot webhook recipe (emoji-smith) produced when run through the real
harness: a webhook whose start command backgrounds uvicorn and returns 0 while the process then dies on missing cloud
credentials, and a verify script whose checks fail. Before this, the controller reported the first as every phase
"passed" with status "blocked", and neither the person nor the repair discovery saw which checks failed or why."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from conftest import make_config
from factory import onboarding
from factory import sessions
from factory import test_environments as te
from factory.config import AgentRef
from factory.onboarding import AppOnboarding, validate_plan
from factory.test_environments import fingerprint

RECIPE = {"schema_version": 1, "name": "slack-webhook", "runtime": "linux",
          "commands": {"prepare": "bash verification/factory-onboarding/prepare.sh",
                       "start": "bash verification/factory-onboarding/start.sh", "seed": "true",
                       "verify": ".venv/bin/python verification/factory-onboarding/verify.py",
                       "stop": "bash verification/factory-onboarding/stop.sh"},
          "services": [{"id": "webhook", "port": 8000, "health_path": "/health"}], "secrets": [],
          "data": {"kind": "synthetic", "authentication": "none-required"},
          "network": {"allowed_hosts": [], "allow_package_managers": True}, "timeout_seconds": 900,
          "verification": {"kind": "api", "checks": [
              {"id": "health", "description": "The webhook answers /health"},
              {"id": "rejects-unsigned", "description": "An unsigned Slack interaction is refused"},
              {"id": "signed-challenge", "description": "A signed url_verification request echoes its challenge"}]}}
PHASE = lambda name, status="passed", code=0: {"phase": name, "status": status, "exit_code": code, "seconds": 0.1}
STALLED = {"status": "blocked", "reason": "dependent services did not become ready",
           "phases": [PHASE("prepare"), PHASE("start"), PHASE("stop")],
           "services": [{"service": "webhook", "status": "blocked", "reason": "readiness timeout"}]}
CHECKS_FAILED = {"status": "failed", "reason": "verify did not pass",
                 "phases": [PHASE("prepare"), PHASE("start"), PHASE("seed"), PHASE("verify", "failed", 1), PHASE("stop")],
                 "services": [{"service": "webhook", "status": "passed"}]}
RESULTS = {"status": "failed", "checks": [{"id": "health", "status": "passed", "observation": '{"status": "healthy"}'},
                                          {"id": "rejects-unsigned", "status": "failed", "observation": "HTTP 500"}]}
DIAGNOSIS = {"phase": "services", "summary": "uvicorn exited at import: PubSubJobQueue() raised DefaultCredentialsError "
                                             "because no Pub/Sub emulator host is set."}


def committed(tmp_path, recipe=RECIPE):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_environment.json").write_text(json.dumps(recipe))
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"],
                 ["add", "."], ["commit", "-qm", "recipe"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    return repo


class Client:
    def __init__(self):
        self.beta = SimpleNamespace(environments=SimpleNamespace(create=lambda **kw: SimpleNamespace(id="env_test"),
                                                                 list=lambda **kw: SimpleNamespace(data=[], has_next_page=lambda: False)),
                                    sessions=SimpleNamespace(archive=lambda *a, **kw: None))


def hosted(monkeypatch, tmp_path, root, report, outputs=None, recipe=RECIPE):
    """run_hosted with the session played by a double that returns a harness report bound to the real request."""
    seen = {}

    class Driver:
        def __init__(self, cfg, client=None, on_session_started=None, on_session_finished=None):
            pass

        def run(self, **kw):
            seen.update(kw)
            request = json.loads(kw["uploads"]["environment-request.json"])
            files = {"environment-result.json": json.dumps({**request, **report}).encode()}
            files.update({k: json.dumps(v).encode() for k, v in (outputs or {}).items()})
            return SimpleNamespace(session_id="sesn_test", list_cost_usd=0.42, stop_reason="end_turn", outputs=files)

        def interrupt_active(self):
            return []

    monkeypatch.setattr(sessions, "SessionDriver", Driver)
    cfg = make_config(root, tmp_path / "runs")
    result = te.run_hosted(cfg, committed(tmp_path, recipe), tmp_path / "evidence", budget_usd=4, client=Client())
    return result, seen


def test_a_service_that_never_became_ready_is_named_not_reported_as_all_phases_passed(monkeypatch, tmp_path, root):
    result, _ = hosted(monkeypatch, tmp_path, root, STALLED, {"diagnosis.json": DIAGNOSIS})
    assert result["status"] == "blocked" and all(p["status"] == "passed" for p in result["phases"])
    assert result["services"] == [{"service": "webhook", "status": "blocked", "reason": "readiness timeout"}]
    assert result["harness_reason"] == "dependent services did not become ready"
    assert "webhook never answered its health check" in result["detail"]
    assert result["diagnosis"] == {"phase": "services", "summary": DIAGNOSIS["summary"]}
    assert "checks" not in result and "Checks not passed" not in result["detail"]      # verify never ran


def test_failing_and_unreported_checks_are_listed_with_their_observations(monkeypatch, tmp_path, root):
    result, _ = hosted(monkeypatch, tmp_path, root, CHECKS_FAILED, {"verification-result.json": RESULTS})
    assert [(c["id"], c["status"], c["observation"]) for c in result["checks"]] == [
        ("health", "passed", '{"status": "healthy"}'), ("rejects-unsigned", "failed", "HTTP 500"),
        ("signed-challenge", "not_reported", "")]
    assert result["detail"].startswith("The verify command did not pass (exit 1).")
    assert "Checks not passed: rejects-unsigned (failed); signed-challenge (not reported)." in result["detail"]
    assert "Raw logs stay in the sandbox" in result["detail"]


def test_a_passing_functional_recipe_says_so_and_ignores_any_diagnosis(monkeypatch, tmp_path, root):
    ok = {"status": "passed", "checks": [{"id": c["id"], "status": "passed", "observation": "seen"} for c in RECIPE["verification"]["checks"]]}
    report = {"status": "passed", "phases": [PHASE(p) for p in ("prepare", "start", "seed", "verify", "stop")],
              "services": [{"service": "webhook", "status": "passed"}]}
    result, _ = hosted(monkeypatch, tmp_path, root, report, {"verification-result.json": ok, "diagnosis.json": DIAGNOSIS})
    assert result["status"] == "passed" and "diagnosis" not in result
    assert result["detail"] == "Every lifecycle phase and every declared functional check passed in the hosted sandbox."


def test_an_unbound_report_contributes_no_rows(monkeypatch, tmp_path, root):
    result, _ = hosted(monkeypatch, tmp_path, root, {**CHECKS_FAILED, "head_sha": "0" * 40}, {"verification-result.json": RESULTS})
    assert result["status"] == "blocked" and "checks" not in result and "phases" not in result
    assert "not bound" in result["detail"]


def test_the_sandbox_prompt_mentions_a_browser_only_for_browser_recipes(monkeypatch, tmp_path, root):
    _, seen = hosted(monkeypatch, tmp_path, root, STALLED)
    assert "browser" not in seen["system_override"].lower() and "diagnosis.json" in seen["system_override"]
    browser = json.loads((root / "examples/greenfield-browser/test_environment.json").read_text())
    assert te.BROWSER_SYSTEM in te.onboarding_system(browser)
    assert te.BROWSER_SYSTEM in te.SYSTEM          # change runs keep it: their e2e entries may drive a browser


def test_functional_rows_are_bounded_and_malformed_results_are_not_trusted(tmp_path):
    (tmp_path / "verification-result.json").write_text(json.dumps({"status": "passed", "checks": [
        {"id": "health", "status": "ok", "observation": "x" * 5000}, {"id": "health", "status": "passed", "observation": "dup"},
        "not-a-row", {"id": "unknown", "status": "passed", "observation": "undeclared"}]}))
    rows = te.functional_checks(RECIPE, tmp_path)
    assert rows[0] == {"id": "health", "description": "The webhook answers /health", "status": "invalid", "observation": "x" * 600}
    assert [r["id"] for r in rows] == ["health", "rejects-unsigned", "signed-challenge"]
    assert te.functional_checks({"browser": {}}, tmp_path) is None
    (tmp_path / "diagnosis.json").write_text(json.dumps({"phase": "elsewhere", "summary": "y" * 4000}))
    assert te.read_diagnosis(tmp_path) == {"phase": None, "summary": "y" * 1500}
    (tmp_path / "diagnosis.json").write_text("[]")
    assert te.read_diagnosis(tmp_path) is None


def test_an_invalid_plan_says_where_without_echoing_values(tmp_path, root):
    plan = {"summary": "bot", "app_kind": "bot", "journeys": [], "capabilities": [{"id": "api", "label": "API", "available": True}],
            "questions": [{"id": "Has Key sk-live-123", "prompt": "?", "required": True}], "profile_path": None, "files": [],
            "limitations": []}
    with pytest.raises(ValueError) as err:
        validate_plan(plan, "existing", tmp_path, root / "schemas")
    text = str(err.value)
    assert "app_kind: invalid enum" in text and "capabilities/0: invalid required" in text and "questions/0/id: invalid pattern" in text
    assert "sk-live-123" not in text and "bot" not in text.split("(")[1]


def test_a_failed_verification_reaches_the_person_and_the_next_discovery(monkeypatch, tmp_path, root):
    """Verify fails on checks; the record says which; the repair discovery is sent those rows and the diagnosis."""
    cfg = make_config(root, tmp_path / "runs", gate_surface="web", agents={"app_onboarding": AgentRef(id="agent_onboard", version=1)})
    manager = AppOnboarding(cfg, client=Client())
    aid = manager.create(kind="existing", name="Bot", repository_url="https://github.com/example/bot", description="Slack bot",
                         budget_usd=10)["app_id"]
    plan = {"summary": "Slack bot", "app_kind": "api", "journeys": [], "capabilities": [], "questions": [],
            "profile_path": "test_environment.json", "files": [{"path": "test_environment.json", "content": json.dumps(RECIPE)}],
            "limitations": [], "profile": RECIPE}
    manager.update(aid, plan=plan, plan_sha256=fingerprint(plan), source_sha="abc", status="setup_ready")
    monkeypatch.setattr(manager, "_candidate", lambda r: tmp_path)
    failed = {"status": "failed", "detail": "The verify command did not pass (exit 1). Checks not passed: rejects-unsigned (failed).",
              "artifacts": [], "phases": CHECKS_FAILED["phases"], "checks": [{"id": "rejects-unsigned", "description": "d",
              "status": "failed", "observation": "HTTP 500"}], "diagnosis": {"phase": "verify", "summary": "UnauthorizedError is unhandled"}}
    monkeypatch.setattr(onboarding, "run_hosted", lambda cfg, repo, out, **kw: (out.mkdir(parents=True), dict(failed))[1])
    manager._verify(aid, 4.0)
    r = manager.get(aid)
    assert r["status"] == "failed" and "rejects-unsigned (failed)" in r["error"] and "discover again" in r["error"]
    assert r["verification"]["checks"][0]["observation"] == "HTTP 500"

    sent = {}

    class Driver:
        def __init__(self, cfg, client=None, on_session_started=None, on_session_finished=None):
            pass

        def run(self, **kw):
            sent.update(kw)
            return SimpleNamespace(session_id="sesn_d", list_cost_usd=0.5, stop_reason="end_turn",
                                   outputs={"onboarding-plan.json": json.dumps({k: v for k, v in plan.items() if k != "profile"}).encode()})

        def interrupt_active(self):
            return []

    monkeypatch.setattr(onboarding, "SessionDriver", Driver)
    monkeypatch.setattr(onboarding, "ensure_environment", lambda *a, **kw: "env_discovery")
    monkeypatch.setattr(manager, "source", lambda record: committed(tmp_path / "src"))
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(onboarding.threading, "Thread", lambda target, args, **kw: SimpleNamespace(start=lambda: target(*args)))
    manager.start(aid, "discover")
    request = json.loads(sent["uploads"]["onboarding-request.json"])
    previous = request["previous_attempt"]["verification"]
    assert previous["checks"][0]["observation"] == "HTTP 500" and previous["diagnosis"]["summary"] == "UnauthorizedError is unhandled"
    assert "previous_attempt.verification records the first failed phase" in sent["description"]
    assert "functional verification checks" in request["evidence"]
    assert manager.get(aid)["status"] == "setup_ready"

@pytest.mark.parametrize('bad', [[], {}, 7])
def test_malformed_diagnostic_fields_remain_visible_as_invalid(tmp_path, bad):
    (tmp_path / 'verification-result.json').write_text(json.dumps({
        'checks': [{'id': 'health', 'status': bad, 'observation': 'probe output'}]}))
    rows = te.functional_checks(RECIPE, tmp_path)
    assert rows[0]['status'] == 'invalid'
    (tmp_path / 'diagnosis.json').write_text(json.dumps({'phase': bad, 'summary': 'A diagnosis'}))
    assert te.read_diagnosis(tmp_path) == {'phase': None, 'summary': 'A diagnosis'}


def test_a_discovery_that_ends_without_a_plan_says_why_and_the_next_discovery_hears_it(monkeypatch, tmp_path, root):
    """Live emoji-smith: auto mode denied a draft write and the agent ended its turn asking in chat. The record said only
    that no plan came back "within its session limit", and the retry was sent no reason."""
    cfg = make_config(root, tmp_path / "runs", gate_surface="web", agents={"app_onboarding": AgentRef(id="agent_onboard", version=1)})
    manager = AppOnboarding(cfg, client=Client())
    aid = manager.create(kind="existing", name="Bot", repository_url="https://github.com/example/bot", description="Slack bot",
                         budget_usd=10)["app_id"]
    sent = []

    class Driver:
        def __init__(self, cfg, client=None, on_session_started=None, on_session_finished=None):
            pass

        def run(self, **kw):
            sent.append(kw)
            return SimpleNamespace(session_id="sesn_d", list_cost_usd=0.5, stop_reason="end_turn", outputs={},
                                   last_message="I was blocked writing start.sh.\n\nShould I finish it as described?")

        def interrupt_active(self):
            return []

    monkeypatch.setattr(onboarding, "SessionDriver", Driver)
    monkeypatch.setattr(onboarding, "ensure_environment", lambda *a, **kw: "env_discovery")
    (tmp_path / "src").mkdir()
    repo = committed(tmp_path / "src")
    monkeypatch.setattr(manager, "source", lambda record: repo)
    monkeypatch.setattr(onboarding.threading, "Thread", lambda target, args, **kw: SimpleNamespace(start=lambda: target(*args)))
    manager.start(aid, "discover")
    r = manager.get(aid)
    assert r["status"] == "failed" and "session limit" not in r["error"]
    assert "ended without writing a setup plan (stop: end_turn)" in r["error"]
    assert "Its last message: I was blocked writing start.sh. Should I finish it as described?" in r["error"]

    manager.start(aid, "discover")
    request = json.loads(sent[1]["uploads"]["onboarding-request.json"])
    assert "I was blocked writing start.sh" in request["previous_attempt"]["error"]


def test_a_plan_without_a_recipe_stays_in_needs_input_once_its_question_is_answered(monkeypatch, tmp_path, root):
    """A blocked step yields profile_path:null and a required question. Rediscovery may repeat the answered question;
    the app must not read setup_ready, because Verify would refuse it with nothing left to answer."""
    cfg = make_config(root, tmp_path / "runs", gate_surface="web", agents={"app_onboarding": AgentRef(id="agent_onboard", version=1)})
    manager = AppOnboarding(cfg, client=Client())
    aid = manager.create(kind="existing", name="Bot", repository_url="https://github.com/example/bot", description="Slack bot",
                         budget_usd=10)["app_id"]
    manager.update(aid, answers={"start-blocked": "Yes, finish it"})
    plan = {"summary": "Slack bot", "app_kind": "api", "journeys": [], "capabilities": [], "profile_path": None, "files": [],
            "limitations": [], "questions": [{"id": "start-blocked", "prompt": "start.sh was denied. Finish it?", "required": True}]}

    class Driver:
        def __init__(self, cfg, client=None, on_session_started=None, on_session_finished=None):
            pass

        def run(self, **kw):
            return SimpleNamespace(session_id="sesn_d", list_cost_usd=0.5, stop_reason="end_turn",
                                   outputs={"onboarding-plan.json": json.dumps(plan).encode()})

        def interrupt_active(self):
            return []

    monkeypatch.setattr(onboarding, "SessionDriver", Driver)
    monkeypatch.setattr(onboarding, "ensure_environment", lambda *a, **kw: "env_discovery")
    (tmp_path / "src").mkdir()
    repo = committed(tmp_path / "src")
    monkeypatch.setattr(manager, "source", lambda record: repo)
    monkeypatch.setattr(onboarding.threading, "Thread", lambda target, args, **kw: SimpleNamespace(start=lambda: target(*args)))
    manager.start(aid, "discover")
    r = manager.get(aid)
    assert r["status"] == "needs_input" and "No runnable recipe" in r["phase"]


def test_onboarding_drafts_skip_auto_mode_but_its_shell_does_not(root):
    """write/edit are always_allow (auto mode denied legitimate recipe drafts); bash keeps the server's risk check."""
    head = (root / "agents/app_onboarding.md").read_text().split("\n---\n", 1)[0]
    assert "default_config: {enabled: true, permission_policy: {type: auto}}" in head
    for tool in ("write", "edit"):
        assert f"- {{name: {tool}, enabled: true, permission_policy: {{type: always_allow}}}}" in head
    assert "name: bash" not in head


def test_owner_feedback_is_in_the_task_not_only_in_the_request(monkeypatch, tmp_path, root):
    """Live emoji-smith: feedback sat after a 30 kB previous plan in onboarding-request.json, the agent's first read was
    cut off, and the requested change was never seen. The feedback now leads the request and is in the task itself."""
    cfg = make_config(root, tmp_path / "runs", gate_surface="web", agents={"app_onboarding": AgentRef(id="agent_onboard", version=1)})
    manager = AppOnboarding(cfg, client=Client())
    aid = manager.create(kind="existing", name="Bot", repository_url="https://github.com/example/bot", description="Slack bot",
                         budget_usd=10)["app_id"]
    manager.feedback(aid, "Assert OPENAI_IMAGE_MODEL instead of the literal model name")
    sent = {}

    class Driver:
        def __init__(self, cfg, client=None, on_session_started=None, on_session_finished=None):
            pass

        def run(self, **kw):
            sent.update(kw)
            return SimpleNamespace(session_id="sesn_d", list_cost_usd=0.5, stop_reason="end_turn", outputs={}, last_message="")

        def interrupt_active(self):
            return []

    monkeypatch.setattr(onboarding, "SessionDriver", Driver)
    monkeypatch.setattr(onboarding, "ensure_environment", lambda *a, **kw: "env_discovery")
    (tmp_path / "src").mkdir()
    repo = committed(tmp_path / "src")
    monkeypatch.setattr(manager, "source", lambda record: repo)
    monkeypatch.setattr(onboarding.threading, "Thread", lambda target, args, **kw: SimpleNamespace(start=lambda: target(*args)))
    manager.start(aid, "discover")
    assert "Assert OPENAI_IMAGE_MODEL instead of the literal model name" in sent["description"]
    request = json.loads(sent["uploads"]["onboarding-request.json"])
    keys = list(request)
    assert keys.index("feedback") < keys.index("previous_attempt")
