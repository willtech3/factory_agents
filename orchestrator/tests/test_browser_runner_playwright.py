"""The factory's browser runner in a real Chromium, against a small local app: the Playwright driver behind the logic
test_browser_runner.py covers with a scripted browser. Skipped where Python Playwright and a Chromium are not installed
(the hosted sandbox has both; so does a developer machine after `pip install playwright`)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from factory import visual
from factory.sandbox_tools import browser_runner as br

playwright = pytest.importorskip("playwright.sync_api")

SECRET = "sk-test-0123456789abcdef"
PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width"><title>Chat</title></head><body>
<button aria-label="Select model" onclick="document.getElementById('list').hidden=false">Model</button>
<ul id="list" hidden><li><button onclick="document.getElementById('cur').textContent='Current model: Claude Haiku 5.5'">Claude Haiku 5.5</button></li></ul>
<p id="cur">Current model: none</p>
<label>Message <textarea id="m"></textarea></label>
<button onclick="send()">Send</button><p id="out"></p>
<script>
async function send() {
  const r = await fetch('/api/chat', {method: 'POST', headers: {'content-type': 'application/json'},
                                      body: JSON.stringify({model: 'anthropic/claude-haiku-5.5', text: document.getElementById('m').value})});
  document.getElementById('out').textContent = r.status === 200 ? 'The sum is 5' : 'Refused ' + r.status;
}
</script></body></html>"""


class App(BaseHTTPRequestHandler):
    status = 200
    leak = False

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = PAGE.replace("<p id=\"out\">", f"<p>{SECRET}</p><p id=\"out\">") if App.leak else PAGE
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        self.send_response(App.status)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b"data: 5\n\n")


@pytest.fixture
def app():
    App.status, App.leak = 200, False
    server = ThreadingHTTPServer(("127.0.0.1", 0), App)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://localhost:{server.server_address[1]}"
    server.shutdown()


JOURNEY = {"version": 1, "name": "pick-and-chat", "steps": [
    {"name": "Open the app", "actions": [{"do": "goto", "path": "/"}, {"do": "expect_visible", "role": "button", "name": "Select model"}]},
    {"name": "Pick Claude Haiku 5.5", "actions": [{"do": "click", "role": "button", "name": "Select model"},
                                                  {"do": "click", "role": "button", "name": "Claude Haiku 5.5"},
                                                  {"do": "expect_text", "text": "Current model: Claude Haiku 5.5"}]},
    {"name": "Send", "actions": [{"do": "watch", "id": "chat", "method": "POST", "url_contains": "/api/chat"},
                                 {"do": "fill", "label": "Message", "value": "Add 2 and 3"},
                                 {"do": "click", "role": "button", "name": "Send", "exact": True},
                                 {"do": "expect_request", "id": "chat", "status": 200, "content_type": "text/event-stream",
                                  "body_contains": {"model": "anthropic/claude-haiku-5.5"}},
                                 {"do": "expect_text", "text": "The sum is 5"}, {"do": "expect_fits_viewport"}]}]}


def _run(tmp_path, base_url, viewport, journey=JOURNEY, secrets=None):
    entry = {"command": "factory:journey pick-and-chat", "description": "Pick and chat", "covers_ac": ["AC-1"],
             "visual": {"persona": "Member", "viewport": viewport, "steps": [s["name"] for s in journey["steps"]],
                        "environment": {"name": "local", "data": "synthetic", "authentication": "none-required"}}}
    directory, request = visual.new_request(tmp_path, tmp_path, "a" * 40, base_url, entry)
    path = tmp_path / "journey.json"
    path.write_text(json.dumps(journey))
    env = {"FACTORY_EVIDENCE_DIR": str(directory), "FACTORY_VISUAL_REQUEST": str(directory / "request.json"), "BASE_URL": base_url,
           **(secrets or {}), "FACTORY_SECRET_NAMES": ",".join(secrets or {})}
    try:
        code = br.main(["--journey", str(path)], env=env)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no runnable Chromium for Playwright here: {exc}")
    report = json.loads((directory / "result.json").read_text())
    if report["status"] == "blocked" and "browser did not start" in report["summary"]:
        pytest.skip(report["summary"])
    return code, report, visual.collect(directory, request, code)


@pytest.mark.parametrize("viewport", [{"width": 1440, "height": 900}, {"width": 390, "height": 844}])
def test_a_real_browser_passes_with_a_screenshot_per_step_and_a_demo(tmp_path, app, viewport):
    code, report, evidence = _run(tmp_path, app, viewport)
    assert code == 0 and report["status"] == "passed", report["summary"]
    assert report["viewport_observed"] == viewport
    assert evidence["status"] == "passed" and [m["role"] for m in evidence["media"]] == ["screenshot"] * 3 + ["demo"]


def test_a_real_403_is_blocked(tmp_path, app):
    App.status = 403
    code, report, _ = _run(tmp_path, app, {"width": 1440, "height": 900})
    assert code == 3 and report["status"] == "blocked" and "answered 403" in report["summary"]


def test_a_real_credential_on_screen_deletes_the_media(tmp_path, app):
    App.leak = True
    code, report, evidence = _run(tmp_path, app, {"width": 1440, "height": 900}, secrets={"OPENROUTER_API_KEY": SECRET})
    assert code == 3 and report["secret_seen"] and report["media"] == [] and evidence["credential_seen"]
    assert not list(tmp_path.rglob("*.png")) and not list(tmp_path.rglob("demo.*"))
