"""A scripted stand-in for the browser behind `browser_runner.Runner`: a tiny page model that answers the runner's
driver calls, so the runner's logic (actions, blocked versus failed, credential hygiene, media) is tested without
Playwright. Run as a script it plays the factory runner inside the hosted-harness test double:
`python scripted_browser.py --journey <path>` with SCRIPTED_PAGE (a JSON page model) in the environment."""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/k9sAAAAASUVORK5CYII=")
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 64


class ScriptedDriver:
    """`page`: {"texts": [...] visible text, "targets": [...] visible target descriptions (e.g. "role button Send"),
    "on_click": {target: {"texts": [...], "responses": [...]}} what activating a target adds, "goto_status": 200,
    "goto_error": str, "start_error": str, "viewport": {...}, "scroll_width": int, "url": str}."""

    def __init__(self, page: dict[str, Any] | None = None):
        self.page = {"texts": [], "targets": [], "on_click": {}, "goto_status": 200, **(page or {})}
        self.log: list[dict[str, Any]] = []
        self.calls: list[tuple] = []
        self.video_dir: Path | None = None
        self.started_with: tuple | None = None

    @staticmethod
    def key(target: dict[str, Any]) -> str:
        for k in ("role", "label", "text", "placeholder", "test_id"):
            if k in target:
                return f"{k} {target[k]}" + (f" {target['name']}" if k == "role" and target.get("name") else "")
        return "?"

    def start(self, viewport, touch, video_dir):
        if self.page.get("start_error"):
            raise RuntimeError(self.page["start_error"])
        self.started_with, self.video_dir = (viewport, touch), video_dir
        self.page.setdefault("viewport", viewport)

    def goto(self, url, timeout_s):
        self.calls.append(("goto", url))
        if self.page.get("goto_error"):
            raise RuntimeError(self.page["goto_error"])
        self.page["url"] = url
        return self.page["goto_status"]

    def _apply(self, effect: dict[str, Any]) -> None:
        self.page["texts"] = self.page["texts"] + effect.get("texts", [])
        self.page["targets"] = self.page["targets"] + effect.get("targets", [])
        for r in effect.get("responses", []):
            self.log.append({"method": "GET", "content_type": "", "body": "", **r, "at": time.monotonic()})
        if "url" in effect:
            self.page["url"] = effect["url"]

    def click(self, target, mode, timeout_s):
        self.calls.append(("click", self.key(target), mode))
        if self.key(target) not in self.page["targets"]:
            raise RuntimeError(f"{self.key(target)} not found")
        self._apply(self.page["on_click"].get(self.key(target), {}))

    def fill(self, target, value, timeout_s):
        self.calls.append(("fill", self.key(target), value))
        if self.key(target) not in self.page["targets"]:
            raise RuntimeError(f"{self.key(target)} not found")
        self.page.setdefault("inputs", []).append(value)

    def press(self, key):
        self.calls.append(("press", key))
        self._apply(self.page["on_click"].get(f"key {key}", {}))

    def visible(self, target, timeout_s):
        return self.key(target) in self.page["targets"]

    def hidden(self, target, timeout_s):
        return self.key(target) not in self.page["targets"]

    def text_visible(self, text, timeout_s):
        return any(text in t for t in self.page["texts"])

    def reload(self, timeout_s):
        self.calls.append(("reload",))

    def url(self):
        return self.page.get("url", "")

    def box(self, target):
        return self.page.get("boxes", {}).get(self.key(target), {"x": 0, "y": 0, "width": 10, "height": 10})

    def scroll_width(self):
        return self.page.get("scroll_width", self.page["viewport"]["width"])

    def viewport(self):
        return dict(self.page["viewport"])

    def screen_text(self):
        return "\n".join(self.page["texts"] + self.page.get("inputs", []))

    def responses(self):
        return list(self.log)

    def pump(self, ms):
        self.calls.append(("pump", ms)) if ms >= 1000 else None

    def screenshot(self, path):
        path.write_bytes(PNG)

    def close(self):
        if self.video_dir is None:
            return None
        self.video_dir.mkdir(parents=True, exist_ok=True)
        video = self.video_dir / "page.webm"
        video.write_bytes(WEBM)
        return video


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from factory.sandbox_tools import browser_runner

    sys.exit(browser_runner.main(sys.argv[1:], driver=ScriptedDriver(json.loads(os.environ.get("SCRIPTED_PAGE") or "{}"))))
