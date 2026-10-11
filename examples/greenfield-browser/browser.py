"""Real browser interaction and recording in an Anthropic-hosted sandbox."""
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

evidence = Path(os.environ["FACTORY_EVIDENCE_DIR"])
profile = json.loads(Path(os.environ["FACTORY_TEST_PROFILE"]).read_text())
steps = []
report = {"status": "blocked", "steps": steps}
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for i, viewport in enumerate(profile["browser"]["viewports"]):
            context = browser.new_context(viewport=viewport, record_video_dir=str(evidence / "recordings"))
            page = context.new_page()
            page.goto(os.environ["BASE_URL"], wait_until="networkidle")
            page.get_by_label("Task name").fill("Synthetic task from the Factory")
            page.get_by_role("button", name="Add task").click()
            page.get_by_text("Synthetic task from the Factory", exact=True).wait_for()
            page.reload(wait_until="networkidle")
            page.get_by_text("Synthetic task from the Factory", exact=True).wait_for()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            shot = f"viewport-{viewport['width']}.png"
            page.screenshot(path=str(evidence / shot))
            steps.append({"description": "Add a task and verify it persists after reload without horizontal overflow", "status": "passed", "viewport": viewport, "screenshot": shot})
            video = page.video
            context.close()
            if i == 0:
                video.save_as(str(evidence / "enhancement.webm"))
                report["demo"] = "enhancement.webm"
        browser.close()
    report["status"] = "passed"
finally:
    (evidence / "browser-result.json").write_text(json.dumps(report, indent=2))
