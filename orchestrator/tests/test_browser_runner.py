"""The factory's browser runner, solved once (post-mortem 2026-10-11 §1): journey validation, blocked versus failed, and
credential hygiene, through a scripted browser. The runner's report must be one the controller's `visual.collect` accepts."""
import json
from pathlib import Path

import pytest
from support.scripted_browser import ScriptedDriver

from factory import visual
from factory.sandbox_tools import browser_runner as br

SECRET = "sk-or-v1-0123456789abcdef"
STEPS = ["Open the app", "Pick Claude Haiku 5.5", "Send and watch the reply stream"]


def journey(**changes):
    j = {"version": 1, "name": "pick-and-chat", "input": "pointer", "steps": [
        {"name": STEPS[0], "actions": [{"do": "goto", "path": "/"}, {"do": "expect_visible", "role": "button", "name": "Select model"}]},
        {"name": STEPS[1], "actions": [{"do": "click", "role": "button", "name": "Select model"},
                                       {"do": "fill", "label": "Search models", "value": "Haiku 5.5"},
                                       {"do": "click", "text": "Claude Haiku 5.5"}, {"do": "expect_text", "text": "Current model: Claude Haiku 5.5"}]},
        {"name": STEPS[2], "actions": [{"do": "watch", "id": "chat", "method": "POST", "url_contains": "/api/chat"},
                                       {"do": "fill", "label": "Message", "value": "Add 2 and 3"},
                                       {"do": "press", "key": "Enter"},
                                       {"do": "expect_request", "id": "chat", "status": 200, "content_type": "text/event-stream",
                                        "body_contains": {"model": "anthropic/claude-haiku-5.5"}},
                                       {"do": "expect_text", "text": "The sum is 5"}, {"do": "expect_fits_viewport"}]}]}
    j.update(changes)
    return j


def page(**changes):
    p = {"texts": ["Evergreen"], "targets": ["role button Select model", "label Search models", "label Message"],
         "on_click": {"role button Select model": {"targets": ["text Claude Haiku 5.5"]},
                      "text Claude Haiku 5.5": {"texts": ["Current model: Claude Haiku 5.5"]},
                      "key Enter": {"texts": ["The sum is 5"], "responses": [
                          {"method": "POST", "url": "http://localhost:3000/api/chat", "status": 200, "content_type": "text/event-stream",
                           "body": json.dumps({"model": "anthropic/claude-haiku-5.5"})}]}}}
    p.update(changes)
    return p


def run(tmp_path, j=None, p=None, secrets=None, viewport=None):
    entry = {"command": "factory:journey pick-and-chat", "description": "Pick and chat", "covers_ac": ["AC-9"],
             "visual": {"persona": "Member", "viewport": viewport or {"width": 1440, "height": 900}, "steps": STEPS,
                        "environment": {"name": "Hosted app", "data": "synthetic", "authentication": "test-identity"}}}
    directory, request = visual.new_request(tmp_path, tmp_path, "a" * 40, "http://localhost:3000", entry)
    driver = ScriptedDriver(p or page())
    report = br.Runner(driver, j or journey(), request, "http://localhost:3000", directory, secrets or {}).run()
    (directory / "result.json").write_text(json.dumps(report))
    return report, driver, directory, request


def test_journey_validation_names_every_problem():
    assert br.validate_journey(journey(), STEPS) == []
    bad = journey(version=2, input="mouse")
    bad["steps"][0]["actions"].append({"do": "goto", "path": "https://elsewhere.example/"})
    bad["steps"][1]["actions"].append({"do": "click"})
    bad["steps"][2]["actions"].append({"do": "teleport"})
    problems = br.validate_journey(bad, STEPS + ["extra"])
    for expected in ("version must be 1", "input must be one of", "not another site", "click needs a target", "`do` must be one of",
                     "the journey has 3 steps but the spec's visual contract has 4"):
        assert any(expected in p for p in problems), expected
    # a goto stays on the app: no scheme, host or backslash; wrong types are refused before any browser starts
    for path in ("data:text/html,<p>ok</p>", "file:///etc/hostname", "https:evil.example/x", "/\\evil.example", "//evil.example/"):
        assert any("not another site" in p for p in br.validate_journey({**journey(), "steps": [
            {"name": "x", "actions": [{"do": "goto", "path": path}]}]})), path
    assert br.validate_journey({**journey(), "steps": [{"name": "x", "actions": [{"do": "goto", "path": "chat?x=1"}]}]}) == []
    typed = br.validate_journey({**journey(), "steps": [{"name": "x", "actions": [
        {"do": "wait", "ms": "1s"}, {"do": "tab", "count": True}, {"do": "expect_url", "path_contains": 3}]}]})
    assert [p.split(": ")[1] for p in typed] == ["ms must be a whole number", "count must be a whole number", "path_contains must be text"]


def test_a_passing_journey_answers_the_controllers_request(tmp_path):
    report, _, directory, request = run(tmp_path)
    assert report["status"] == "passed" and report["secret_seen"] is False and report["viewport_observed"] == {"width": 1440, "height": 900}
    assert [s["status"] for s in report["steps"]] == ["passed"] * 3
    assert "/api/chat answered 200 text/event-stream" in report["steps"][2]["observation"]
    assert [m["role"] for m in report["media"]] == ["screenshot"] * 3 + ["demo"]
    evidence = visual.collect(directory, request, br.EXIT[report["status"]])
    assert evidence["status"] == "passed" and len(evidence["media"]) == 4 and evidence["viewport_observed"] == {"width": 1440, "height": 900}


def test_a_false_expectation_fails_and_later_steps_are_not_reached(tmp_path):
    p = page()
    p["on_click"]["key Enter"]["texts"] = ["The sum is 6"]
    report, *_ = run(tmp_path, p=p)
    assert report["status"] == "failed" and br.EXIT["failed"] == 1
    assert report["steps"][2]["status"] == "failed" and "'The sum is 5' did not appear" in report["steps"][2]["observation"]
    assert report["summary"].startswith("Step 3 (Send and watch the reply stream) failed")
    p = page(targets=["label Search models", "label Message"])
    report, *_ = run(tmp_path / "b", p=p)
    assert [s["status"] for s in report["steps"]] == ["failed", "blocked", "blocked"] and "not reached" in report["steps"][1]["observation"]


@pytest.mark.parametrize("status", [401, 402, 403, 407])
def test_a_refusal_from_the_app_is_blocked_not_failed(tmp_path, status):
    """evergreen_chat#120 F-7: a 403 redirect made an expectation miss; the cause is a credential, not the change."""
    p = page()
    p["on_click"]["key Enter"] = {"responses": [{"method": "POST", "url": "http://localhost:3000/api/chat", "status": status}],
                                  "url": "http://localhost:3000/onboarding"}
    report, *_ = run(tmp_path, p=p)
    assert report["status"] == "blocked" and report["steps"][2]["status"] == "blocked"
    assert f"answered {status}" in report["summary"]


def test_a_refusal_the_journey_asserts_is_a_pass_and_a_different_status_fails(tmp_path):
    p = page()
    p["on_click"]["key Enter"]["responses"][0]["status"] = 403
    j = journey()
    j["steps"][2]["actions"][3] = {"do": "expect_request", "id": "chat", "status": 403}
    assert run(tmp_path, j=j, p=p)[0]["status"] == "passed"
    j = journey()
    j["steps"][0]["actions"][0] = {"do": "goto", "path": "/admin", "status": 403}
    assert run(tmp_path / "g", j=j, p=page(goto_status=403))[0]["status"] == "passed"
    report, *_ = run(tmp_path / "h", j=j, p=page(goto_status=200))
    assert report["status"] == "failed" and "answered 200, expected 403" in report["summary"]


def test_a_refusal_from_another_origin_does_not_block(tmp_path):
    p = page()
    p["on_click"]["key Enter"]["responses"].append({"url": "https://fonts.example/css", "status": 403})
    p["on_click"]["key Enter"]["texts"] = ["nothing"]
    assert run(tmp_path, p=p)[0]["status"] == "failed"


def test_an_unreachable_app_or_a_missing_browser_is_blocked(tmp_path):
    assert run(tmp_path, p=page(goto_error="net::ERR_CONNECTION_REFUSED"))[0]["status"] == "blocked"
    report, *_ = run(tmp_path / "b", p=page(start_error="Executable doesn't exist"))
    assert report["status"] == "blocked" and "browser did not start" in report["summary"] and len(report["steps"]) == 3


def test_a_credential_on_screen_deletes_every_screenshot_and_the_recording(tmp_path):
    """evergreen_chat#120 F-10, solved once: media from a run that showed a key are never kept."""
    p = page()
    p["on_click"]["text Claude Haiku 5.5"]["texts"].append(f"Your key: {SECRET}")
    report, _, directory, request = run(tmp_path, p=p, secrets={"OPENROUTER_API_KEY": SECRET})
    assert report["secret_seen"] is True and report["status"] == "blocked" and report["media"] == []
    assert not list(directory.glob("*.png")) and not list(directory.glob("demo.*"))
    assert SECRET not in json.dumps(report) and report["credentials_checked"] == ["OPENROUTER_API_KEY"]
    evidence = visual.collect(directory, request, 3)
    assert evidence["status"] == "blocked" and evidence["credential_seen"] is True and evidence["media"] == []


def test_a_credential_that_shows_and_goes_within_a_step_still_deletes_the_media(tmp_path):
    p = page()
    p["on_click"]["text Claude Haiku 5.5"]["texts"].append(f"Copied {SECRET}")
    p["on_click"]["key Escape"] = {"remove_texts": [f"Copied {SECRET}"]}
    j = journey()
    j["steps"][1]["actions"].insert(3, {"do": "press", "key": "Escape"})
    report, _, directory, _ = run(tmp_path, j=j, p=p, secrets={"OPENROUTER_API_KEY": SECRET})
    assert report["secret_seen"] is True and report["media"] == [] and not list(directory.glob("*.png"))


def test_an_action_that_errors_still_writes_the_report_and_cleans_up(tmp_path):
    """A journey mistake (Playwright calls Escape "Escape", not "Esc") is a failed step with its reason, and the
    credential cleanup still runs: no screenshot from the run survives a credential on screen."""
    p = page(press_errors=["Esc"])
    p["texts"] = ["Evergreen", f"key {SECRET}"]
    j = journey()
    j["steps"][1]["actions"].insert(0, {"do": "press", "key": "Esc"})
    report, _, directory, _ = run(tmp_path, j=j, p=p, secrets={"OPENROUTER_API_KEY": SECRET})
    assert report["status"] == "blocked" and report["media"] == [] and not list(directory.glob("*.png"))
    p["texts"] = ["Evergreen"]
    report, *_ = run(tmp_path / "b", j=j, p=p)
    assert report["status"] == "failed" and "ValueError: Unknown key" in report["summary"]


def test_a_screen_that_cannot_be_read_keeps_no_recording_or_screenshot(tmp_path):
    report, *_ = run(tmp_path, p=page(unreadable=True))
    assert report["secret_seen"] is False and report["media"] == [] and "could not be checked" in report["demo_note"]


def test_credentials_are_redacted_from_everything_written(tmp_path):
    j = journey()
    j["steps"][1]["actions"][3] = {"do": "expect_text", "text": SECRET}
    report, *_ = run(tmp_path, j=j, secrets={"OPENROUTER_API_KEY": SECRET})
    assert report["status"] == "failed" and SECRET not in json.dumps(report) and br.REDACTED in report["summary"]


def test_a_factory_request_without_the_hygiene_answer_keeps_no_media(tmp_path):
    report, _, directory, request = run(tmp_path)
    del report["secret_seen"]
    (directory / "result.json").write_text(json.dumps(report))
    assert visual.collect(directory, request, 0)["status"] == "blocked"


def test_keyboard_and_touch_inputs(tmp_path):
    _, driver, *_ = run(tmp_path, j=journey(input="keyboard"))
    assert ("click", "role button Select model", "keyboard") in driver.calls
    _, driver, *_ = run(tmp_path / "t", j=journey(input="touch"), viewport={"width": 390, "height": 844})
    assert driver.started_with == ({"width": 390, "height": 844}, True)
    assert ("click", "text Claude Haiku 5.5", "touch") in driver.calls


def test_request_and_viewport_expectations(tmp_path):
    p = page()
    p["on_click"]["key Enter"]["responses"][0]["body"] = json.dumps({"model": "deepseek/deepseek-v4"})
    report, *_ = run(tmp_path, p=p)
    assert report["status"] == "failed" and "deepseek" in report["steps"][2]["observation"]
    report, *_ = run(tmp_path / "w", p=page(scroll_width=1600))
    assert report["status"] == "failed" and "scrolls sideways" in report["summary"]
    j = journey()
    j["steps"][2]["actions"][3] = {"do": "expect_no_request", "id": "chat"}
    assert "1 request(s) to /api/chat" in run(tmp_path / "n", j=j)[0]["summary"]
    # a request still in flight when the assertion starts is waited for, not missed
    p = page()
    p["on_click"]["key Enter"]["late_responses"] = p["on_click"]["key Enter"].pop("responses")
    assert "1 request(s) to /api/chat" in run(tmp_path / "l", j=j, p=p)[0]["summary"]


def test_the_demo_is_a_gif_when_ffmpeg_can_make_one(tmp_path, monkeypatch):
    import shutil
    import subprocess
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)

    def fake_ffmpeg(argv, **kw):
        Path(argv[-1]).write_bytes(b"GIF89a" + b"\x00" * 32)
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(subprocess, "run", fake_ffmpeg)
    report, _, directory, request = run(tmp_path)
    assert report["media"][-1] == {"file": "demo.gif", "role": "demo", "caption": "pick-and-chat"}
    assert (directory / "demo.gif").is_file() and not (directory / "demo.webm").exists()
    assert visual.collect(directory, request, 0)["status"] == "passed"


def test_main_blocks_on_a_missing_credential_or_an_invalid_journey(tmp_path):
    entry = {"command": "factory:journey j", "visual": {"persona": "p", "viewport": {"width": 1, "height": 1}, "steps": STEPS,
                                                         "environment": {"name": "e", "data": "synthetic", "authentication": "none-required"}}}
    directory, _ = visual.new_request(tmp_path, tmp_path, "a" * 40, "http://localhost:3000", entry)
    path = tmp_path / "j.json"
    path.write_text(json.dumps(journey()))
    env = {"FACTORY_EVIDENCE_DIR": str(directory), "FACTORY_VISUAL_REQUEST": str(directory / "request.json"),
           "BASE_URL": "http://localhost:3000", "FACTORY_REQUIRED_SECRETS": "OPENROUTER_API_KEY"}
    assert br.main(["--journey", str(path)], driver=ScriptedDriver(page()), env=env) == 3
    assert "not attached" in json.loads((directory / "result.json").read_text())["summary"]
    path.write_text(json.dumps(journey(version=9)))
    env.pop("FACTORY_REQUIRED_SECRETS")
    assert br.main(["--journey", str(path)], driver=ScriptedDriver(page()), env=env) == br.INVALID
    env["OPENROUTER_API_KEY"] = SECRET
    path.write_text(json.dumps(journey()))
    assert br.main(["--journey", str(path)], driver=ScriptedDriver(page()), env=env) == 0


def test_secret_names_come_from_the_recipe_and_from_what_the_name_says():
    env = {"FACTORY_SECRET_NAMES": "OPENROUTER_API_KEY", "OPENROUTER_API_KEY": SECRET, "SESSION_TOKEN_TTL": "3600",
           "CONVEX_JWT_PRIVATE_KEY_SECRET": "x" * 40, "PATH": "/usr/bin"}
    assert set(br.secrets_from_env(env)) == {"OPENROUTER_API_KEY", "CONVEX_JWT_PRIVATE_KEY_SECRET"}
