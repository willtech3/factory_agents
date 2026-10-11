#!/usr/bin/env python3
"""The factory's browser runner: one shared, controller-owned way to drive a web app through a change's journey.

The controller uploads this file with the harness (`environment_runner.py`) to the hosted sandbox; it is never part of a
target repository. A change's *journey* is a small declarative JSON file the implementer writes as a run artifact
(`/mnt/session/outputs/journeys/<name>.json`) and the controller uploads, bound by its sha256, at verification time. The
runner opens BASE_URL, acts by role, label or text, waits for text, takes one screenshot per step, records a demo,
measures the viewport, watches network requests, and answers the controller's visual request
(FACTORY_VISUAL_REQUEST) with result.json in FACTORY_EVIDENCE_DIR.

Two things are solved once here instead of in every feature's test (post-mortem 2026-10-11 §1):
- blocked versus failed. Blocked means the app could not be exercised: the browser would not start, BASE_URL did not
  answer, a required test credential is missing, or the app answered 401/402/403/407 to a same-origin request during the
  step (unless the journey asserts that very status). Failed means the app was exercised and an expectation about it
  was false, or an action errored. Exit codes: 0 passed, 1 failed, 3 blocked (2 for an invalid journey, reported
  blocked).
- credential hygiene. Every value of a declared test secret is redacted from everything the runner writes. The screen
  (page text, form fields, open shadow roots, every frame) is checked after every action; if a credential ever appears,
  every screenshot and the recording are deleted and the check is blocked, and a check that could not read the screen
  keeps no recording: no evidence that showed a credential is ever kept or published.

Stdlib only at import; Playwright (preinstalled in the hosted sandbox) is imported by `PlaywrightDriver` alone, so the
logic above is tested in orchestrator/tests with a scripted driver.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

EXIT = {"passed": 0, "failed": 1, "blocked": 3}
INVALID = 2
INPUTS = ("pointer", "keyboard", "touch")
TARGET_KEYS = ("role", "label", "text", "placeholder", "test_id")
ACTIONS = {
    "goto": {"path"}, "click": set(), "fill": {"value"}, "press": {"key"}, "tab": set(), "reload": set(),
    "expect_text": {"text"}, "expect_no_text": {"text"}, "expect_visible": set(), "expect_hidden": set(),
    "expect_url": {"path_contains"}, "expect_fits_viewport": set(), "watch": {"id", "url_contains"},
    "expect_request": {"id"}, "expect_no_request": {"id"}, "wait": {"ms"},
}
TARGETED = {"click", "fill", "expect_visible", "expect_hidden"}
TYPES = {"ms": int, "count": int, "status": int, "key": str, "text": str, "path_contains": str, "url_contains": str,
         "id": str, "path": str}
BLOCKING_STATUS = {401, 402, 403, 407}
MAX_STEPS, MAX_ACTIONS, MAX_SHOTS = 11, 40, 11
DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S = 15, 240
REDACTED = "[redacted credential]"
SCREEN_TEXT_JS = """() => {
  const out = [document.body ? document.body.innerText : ""];
  const walk = (root) => {
    for (const e of root.querySelectorAll("*")) {
      if (typeof e.value === "string" && e.value) out.push(e.value);
      if (e.shadowRoot) { out.push(e.shadowRoot.textContent || ""); walk(e.shadowRoot); }
    }
  };
  walk(document);
  return out.join("\\n");
}"""
SECRET_NAME = re.compile(r"(?i)(api_?key|token|secret|password|passwd|credential)")


class Blocked(Exception):
    """The app could not be exercised; the change is not shown to be wrong."""


class Failed(Exception):
    """The app was exercised and an expectation about it was false."""


# ---------------------------------------------------------------------------------------------------- the journey

def validate_journey(journey: Any, steps: list[str] | None = None) -> list[str]:
    """Problems with a journey document, [] when it is runnable. `steps` are the spec's visual steps, which the
    journey must answer one for one (the controller's request lists them)."""
    if not isinstance(journey, dict):
        return ["the journey must be a JSON object"]
    problems = []
    if journey.get("version") != 1:
        problems.append("version must be 1")
    if journey.get("input", "pointer") not in INPUTS:
        problems.append(f"input must be one of {INPUTS}")
    planned = journey.get("steps")
    if not isinstance(planned, list) or not 1 <= len(planned) <= MAX_STEPS:
        return problems + [f"steps must be a list of 1 to {MAX_STEPS} steps"]
    if steps is not None and len(planned) != len(steps):
        problems.append(f"the journey has {len(planned)} steps but the spec's visual contract has {len(steps)}")
    total = 0
    for i, step in enumerate(planned, 1):
        if not isinstance(step, dict) or not isinstance(step.get("name"), str) or not step["name"].strip():
            problems.append(f"step {i} needs a name")
            continue
        actions = step.get("actions")
        if not isinstance(actions, list) or not actions:
            problems.append(f"step {i} needs at least one action")
            continue
        total += len(actions)
        for j, action in enumerate(actions, 1):
            where = f"step {i} action {j}"
            if not isinstance(action, dict) or action.get("do") not in ACTIONS:
                problems.append(f"{where}: `do` must be one of {sorted(ACTIONS)}")
                continue
            missing = sorted(k for k in ACTIONS[action["do"]] if k not in action)
            if missing:
                problems.append(f"{where}: {action['do']} needs {missing}")
            if action["do"] in TARGETED and not any(k in action for k in TARGET_KEYS):
                problems.append(f"{where}: {action['do']} needs a target ({', '.join(TARGET_KEYS)})")
            path = urlparse(str(action.get("path", "")))
            if action["do"] == "goto" and (path.scheme or path.netloc or "\\" in str(action.get("path", ""))):
                problems.append(f"{where}: goto takes a path on the app, not another site")
            for k, kind in TYPES.items():
                if k in action and not (isinstance(action[k], kind) and not isinstance(action[k], bool)):
                    problems.append(f"{where}: {k} must be {'a whole number' if kind is int else 'text'}")
            if "timeout_s" in action and not (isinstance(action["timeout_s"], (int, float)) and 0 < action["timeout_s"] <= MAX_TIMEOUT_S):
                problems.append(f"{where}: timeout_s must be between 0 and {MAX_TIMEOUT_S}")
    if total > MAX_ACTIONS:
        problems.append(f"{total} actions; at most {MAX_ACTIONS}")
    return problems


def describe(target: dict[str, Any]) -> str:
    for key in TARGET_KEYS:
        if key in target:
            name = f" {target['name']!r}" if key == "role" and target.get("name") else ""
            return f"{key} {target[key]!r}{name}"
    return "target"


# ---------------------------------------------------------------------------------------------------- the browser

class Driver(Protocol):
    """What the runner needs from a browser. `PlaywrightDriver` in the sandbox; a scripted double in tests."""

    def start(self, viewport: dict[str, int], touch: bool, video_dir: Path | None) -> None: ...
    def goto(self, url: str, timeout_s: float) -> int | None: ...
    def click(self, target: dict[str, Any], mode: str, timeout_s: float) -> None: ...
    def fill(self, target: dict[str, Any], value: str, timeout_s: float) -> None: ...
    def press(self, key: str) -> None: ...
    def visible(self, target: dict[str, Any], timeout_s: float) -> bool: ...
    def hidden(self, target: dict[str, Any], timeout_s: float) -> bool: ...
    def text_visible(self, text: str, timeout_s: float) -> bool: ...
    def reload(self, timeout_s: float) -> None: ...
    def url(self) -> str: ...
    def box(self, target: dict[str, Any]) -> dict[str, float] | None: ...
    def scroll_width(self) -> int: ...
    def viewport(self) -> dict[str, int]: ...
    def screen_text(self) -> str: ...
    def responses(self) -> list[dict[str, Any]]: ...
    def pump(self, ms: int) -> None: ...          # let the browser deliver events while the runner waits
    def screenshot(self, path: Path) -> None: ...
    def close(self) -> Path | None: ...


class PlaywrightDriver:
    """Chromium through the sandbox's preinstalled Python Playwright."""

    def __init__(self) -> None:
        self._pw = self._browser = self._context = self._page = None
        self._log: list[dict[str, Any]] = []

    def start(self, viewport: dict[str, int], touch: bool, video_dir: Path | None) -> None:
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        options: dict[str, Any] = {"viewport": viewport, "has_touch": touch, "is_mobile": touch}
        if video_dir is not None:
            options.update(record_video_dir=str(video_dir), record_video_size=viewport)
        self._context = self._browser.new_context(**options)
        self._page = self._context.new_page()
        self._page.on("response", self._record)

    def _record(self, response: Any) -> None:
        request = response.request
        try:
            body = request.post_data or ""
        except Exception:  # noqa: BLE001  (binary bodies)
            body = ""
        self._log.append({"method": request.method, "url": response.url, "status": response.status,
                          "content_type": (response.headers or {}).get("content-type", ""), "body": body[:4000], "at": time.monotonic()})

    def _locate(self, target: dict[str, Any]) -> Any:
        page, exact = self._page, bool(target.get("exact", False))
        if "role" in target:
            loc = page.get_by_role(target["role"], name=target.get("name"), exact=exact) if target.get("name") else page.get_by_role(target["role"])
        elif "label" in target:
            loc = page.get_by_label(target["label"], exact=exact)
        elif "text" in target:
            loc = page.get_by_text(target["text"], exact=exact)
        elif "placeholder" in target:
            loc = page.get_by_placeholder(target["placeholder"], exact=exact)
        else:
            loc = page.get_by_test_id(target["test_id"])
        return loc.nth(int(target.get("nth", 0)))

    def goto(self, url: str, timeout_s: float) -> int | None:
        response = self._page.goto(url, timeout=timeout_s * 1000, wait_until="load")
        return response.status if response is not None else None

    def click(self, target: dict[str, Any], mode: str, timeout_s: float) -> None:
        loc = self._locate(target)
        loc.wait_for(state="visible", timeout=timeout_s * 1000)
        if mode == "touch":
            loc.tap(timeout=timeout_s * 1000)
        elif mode == "keyboard":
            loc.focus(timeout=timeout_s * 1000)
            self._page.keyboard.press("Enter")
        else:
            loc.click(timeout=timeout_s * 1000)

    def fill(self, target: dict[str, Any], value: str, timeout_s: float) -> None:
        self._locate(target).fill(value, timeout=timeout_s * 1000)

    def press(self, key: str) -> None:
        self._page.keyboard.press(key)

    def _wait(self, loc: Any, state: str, timeout_s: float) -> bool:
        try:
            loc.wait_for(state=state, timeout=timeout_s * 1000)
            return True
        except Exception:  # noqa: BLE001  (Playwright's TimeoutError)
            return False

    def visible(self, target: dict[str, Any], timeout_s: float) -> bool:
        return self._wait(self._locate(target), "visible", timeout_s)

    def hidden(self, target: dict[str, Any], timeout_s: float) -> bool:
        return self._wait(self._locate(target), "hidden", timeout_s)

    def text_visible(self, text: str, timeout_s: float) -> bool:
        return self._wait(self._page.get_by_text(text).first, "visible", timeout_s)

    def reload(self, timeout_s: float) -> None:
        self._page.reload(timeout=timeout_s * 1000, wait_until="load")

    def url(self) -> str:
        return self._page.url

    def box(self, target: dict[str, Any]) -> dict[str, float] | None:
        return self._locate(target).bounding_box()

    def scroll_width(self) -> int:
        return int(self._page.evaluate("document.documentElement.scrollWidth"))

    def viewport(self) -> dict[str, int]:
        seen = self._page.evaluate("({width: window.innerWidth, height: window.innerHeight})")
        return {"width": int(seen["width"]), "height": int(seen["height"])}

    def screen_text(self) -> str:
        # Every frame, every field's value, and the text of open shadow roots; a frame that cannot be read is an error,
        # so the runner treats the scan as incomplete rather than clean.
        return "\n".join(str(frame.evaluate(SCREEN_TEXT_JS)) for frame in self._page.frames)

    def responses(self) -> list[dict[str, Any]]:
        return list(self._log)

    def pump(self, ms: int) -> None:
        # The sync API delivers events (the response log above) only while a Playwright call is running.
        self._page.wait_for_timeout(ms)

    def screenshot(self, path: Path) -> None:
        self._page.screenshot(path=str(path))

    def close(self) -> Path | None:
        video = self._page.video if self._page is not None else None
        path = None
        try:
            if self._context is not None:
                self._context.close()                 # finishes the recording
            path = Path(video.path()) if video is not None else None
        finally:
            if self._browser is not None:
                self._browser.close()
            if self._pw is not None:
                self._pw.stop()
        return path


# ---------------------------------------------------------------------------------------------------- the run

class Runner:
    def __init__(self, driver: Driver, journey: dict[str, Any], request: dict[str, Any], base_url: str, evidence: Path,
                 secrets: dict[str, str], record_demo: bool = True):
        self.driver, self.journey, self.request = driver, journey, request
        self.base_url = base_url.rstrip("/")
        self.origin = urlparse(self.base_url).netloc
        self.evidence, self.record_demo = evidence, record_demo
        self.secrets = {k: v for k, v in secrets.items() if v and len(v) >= 6}
        self.mode = journey.get("input", "pointer")
        self.watches: dict[str, dict[str, Any]] = {}
        self.secret_seen = False
        self.scan_gaps = 0            # scans that could not read the screen: the recording is not kept

    # ---- helpers
    def redact(self, text: str) -> str:
        for value in self.secrets.values():
            text = text.replace(value, REDACTED)
        return text

    def _timeout(self, action: dict[str, Any]) -> float:
        return float(action.get("timeout_s", DEFAULT_TIMEOUT_S))

    def _same_origin(self, url: str) -> bool:
        return urlparse(url).netloc == self.origin

    def _check_screen(self) -> bool:
        """Look for a credential on screen; False when the screen could not be read (a page navigating away, after
        one retry), which counts as a gap, never as clean."""
        for attempt in range(2):
            try:
                text = self.driver.screen_text()
                break
            except Exception:  # noqa: BLE001
                if attempt:
                    self.scan_gaps += 1
                    return False
                with contextlib.suppress(Exception):
                    self.driver.pump(200)
        if any(value in text for value in self.secrets.values()):
            self.secret_seen = True
        return True

    def _refusals(self, since: float) -> list[dict[str, Any]]:
        with contextlib.suppress(Exception):          # a page that is navigating away
            self.driver.pump(100)
        return [r for r in self.driver.responses()
                if r.get("at", 0) >= since and r.get("status") in BLOCKING_STATUS and self._same_origin(r.get("url", ""))]

    # ---- actions
    def act(self, action: dict[str, Any]) -> str:
        do, timeout = action["do"], self._timeout(action)
        if do == "goto":
            url = urljoin(self.base_url + "/", str(action["path"]).lstrip("/"))
            if not self._same_origin(url):
                raise Failed(f"{action['path']!r} is not a path on the app")
            try:
                status = self.driver.goto(url, timeout)
            except Exception as exc:  # noqa: BLE001
                raise Blocked(f"the app did not answer {url}: {str(exc)[:200]}") from None
            if status in BLOCKING_STATUS and action.get("status") != status:
                raise Blocked(f"the app answered {status} to {url}")
            if "status" in action and status != action["status"]:
                raise Failed(f"{urlparse(url).path or '/'} answered {status}, expected {action['status']}")
            return f"opened {urlparse(url).path or '/'} ({status})"
        if do == "click":
            try:
                self.driver.click(action, self.mode, timeout)
            except Exception as exc:  # noqa: BLE001
                raise Failed(f"could not {'tap' if self.mode == 'touch' else 'activate'} {describe(action)}: {str(exc)[:200]}") from None
            return f"{'tapped' if self.mode == 'touch' else 'activated' if self.mode == 'keyboard' else 'clicked'} {describe(action)}"
        if do == "fill":
            try:
                self.driver.fill(action, str(action["value"]), timeout)
            except Exception as exc:  # noqa: BLE001
                raise Failed(f"could not fill {describe(action)}: {str(exc)[:200]}") from None
            return f"typed {len(str(action['value']))} characters into {describe(action)}"
        if do == "press":
            self.driver.press(str(action["key"]))
            return f"pressed {action['key']}"
        if do == "tab":
            for _ in range(int(action.get("count", 1))):
                self.driver.press("Tab")
            return f"pressed Tab {int(action.get('count', 1))} time(s)"
        if do == "reload":
            self.driver.reload(timeout)
            return "reloaded the page"
        if do == "wait":
            self.driver.pump(min(int(action["ms"]), 5000))
            return f"waited {min(int(action['ms']), 5000)} ms"
        if do == "expect_text":
            if not self.driver.text_visible(str(action["text"]), timeout):
                raise Failed(f"text {action['text']!r} did not appear within {timeout:g}s")
            return f"saw {action['text']!r}"
        if do == "expect_no_text":
            if self.driver.text_visible(str(action["text"]), min(timeout, 2)):
                raise Failed(f"text {action['text']!r} is visible")
            return f"no {action['text']!r} on screen"
        if do == "expect_visible":
            if not self.driver.visible(action, timeout):
                raise Failed(f"{describe(action)} was not visible within {timeout:g}s")
            return f"saw {describe(action)}"
        if do == "expect_hidden":
            if not self.driver.hidden(action, timeout):
                raise Failed(f"{describe(action)} was still visible after {timeout:g}s")
            return f"{describe(action)} is gone"
        if do == "expect_url":
            if action["path_contains"] not in urlparse(self.driver.url()).path:
                raise Failed(f"the page is at {urlparse(self.driver.url()).path}, not a path containing {action['path_contains']!r}")
            return f"page path contains {action['path_contains']!r}"
        if do == "expect_fits_viewport":
            width = self.request.get("viewport", {}).get("width") or self.driver.viewport()["width"]
            scroll = self.driver.scroll_width()
            if scroll > width:
                raise Failed(f"the page scrolls sideways ({scroll}px wide in a {width}px viewport)")
            if any(k in action for k in TARGET_KEYS):
                box = self.driver.box(action)
                if box is None or box["x"] < 0 or box["x"] + box["width"] > width:
                    raise Failed(f"{describe(action)} is not inside the {width}px viewport")
            return f"fits the {width}px viewport"
        if do == "watch":
            self.watches[action["id"]] = {"since": time.monotonic(), "method": action.get("method", "").upper(),
                                          "url_contains": action["url_contains"]}
            return f"watching {action.get('method', 'any').upper()} requests to {action['url_contains']}"
        if do in ("expect_request", "expect_no_request"):
            return self._expect_request(action, timeout)
        raise Failed(f"unknown action {do}")       # validate_journey refuses this first

    def _matching(self, watch: dict[str, Any]) -> list[dict[str, Any]]:
        return [r for r in self.driver.responses() if r.get("at", 0) >= watch["since"] and watch["url_contains"] in r.get("url", "")
                and (not watch["method"] or r.get("method", "").upper() == watch["method"])]

    def _expect_request(self, action: dict[str, Any], timeout: float) -> str:
        watch = self.watches.get(action["id"])
        if watch is None:
            raise Failed(f"no watch named {action['id']!r} before this step")
        if action["do"] == "expect_no_request":
            self.driver.pump(int(min(timeout, 2) * 1000))     # a request the last action just sent is still in flight
            hits = self._matching(watch)
            if hits:
                raise Failed(f"{len(hits)} request(s) to {watch['url_contains']} were made")
            return f"no request to {watch['url_contains']}"
        deadline = time.monotonic() + timeout
        self.driver.pump(50)
        hits = self._matching(watch)
        while not hits and time.monotonic() < deadline:
            self.driver.pump(200)
            hits = self._matching(watch)
        if not hits:
            raise Failed(f"no {watch['method'] or ''} request to {watch['url_contains']} within {timeout:g}s".replace("  ", " "))
        last = hits[-1]
        if last.get("status") in BLOCKING_STATUS and action.get("status") != last.get("status"):
            raise Blocked(f"the app answered {last['status']} to {watch['url_contains']}")
        if "status" in action and last.get("status") != action["status"]:
            raise Failed(f"{watch['url_contains']} answered {last.get('status')}, expected {action['status']}")
        if "content_type" in action and action["content_type"] not in (last.get("content_type") or ""):
            raise Failed(f"{watch['url_contains']} answered {last.get('content_type') or 'no content type'}, expected {action['content_type']}")
        if "body_contains" in action:
            try:
                body = json.loads(last.get("body") or "{}")
            except ValueError:
                body = {}
            wrong = {k: body.get(k) for k, v in action["body_contains"].items() if not isinstance(body, dict) or body.get(k) != v}
            if wrong:
                raise Failed(f"the request to {watch['url_contains']} sent {wrong}, expected {action['body_contains']}")
        return f"{watch['url_contains']} answered {last.get('status')} {last.get('content_type') or ''}".strip()

    # ---- the whole journey
    def run(self) -> dict[str, Any]:
        steps = self.journey["steps"]
        observed: list[dict[str, Any]] = []
        media: list[dict[str, Any]] = []
        status, summary, seen = "passed", "", None
        video_dir = self.evidence / ".video" if self.record_demo else None
        try:
            touch = self.mode == "touch"
            try:
                self.driver.start(self.request["viewport"], touch, video_dir)
            except Exception as exc:  # noqa: BLE001
                raise Blocked(f"the browser did not start: {str(exc)[:200]}") from None
            for i, step in enumerate(steps, 1):
                since, notes = time.monotonic(), []
                try:
                    for action in step["actions"]:
                        notes.append(self.act(action))
                        self._check_screen()
                except Exception as exc:  # noqa: BLE001  (any error still writes result.json and cleans up media)
                    refused = self._refusals(since)
                    kind = "blocked" if isinstance(exc, Blocked) or refused else "failed"
                    reason = (str(exc) if isinstance(exc, (Blocked, Failed)) else f"{type(exc).__name__}: {str(exc)[:200]}") + (
                        f"; the app answered {refused[-1]['status']} to {urlparse(refused[-1]['url']).path}"
                        if refused and not isinstance(exc, Blocked) else "")
                    shot = self._check_screen() and self._shot(i, step["name"], media)
                    observed.append({"status": kind, "observation": self.redact(
                        f"{'; '.join(notes + [reason])}" + ("" if shot else " (no screenshot)"))[:1500]})
                    status, summary = kind, self.redact(f"Step {i} ({step['name']}) {kind}: {reason}")[:1500]
                    observed += [{"status": "blocked", "observation": f"not reached: step {i} {kind}"} for _ in steps[i:]]
                    break
                if self._check_screen():
                    self._shot(i, step["name"], media)
                observed.append({"status": "passed", "observation": self.redact("; ".join(notes))[:1500]})
            if status == "passed":
                summary = self.redact(f"All {len(steps)} steps passed: " + "; ".join(s["name"] for s in steps))[:1500]
            try:
                seen = self.driver.viewport()
            except Exception:  # noqa: BLE001
                seen = None
        except Blocked as exc:
            status, summary = "blocked", self.redact(str(exc))[:1500]
            observed = [{"status": "blocked", "observation": summary} for _ in steps]
        finally:
            video = None
            with contextlib.suppress(Exception):      # a browser that never started
                video = self.driver.close()
        demo_note = ""
        if self.record_demo and video is not None and Path(video).is_file() and not self.secret_seen and not self.scan_gaps:
            demo = self._demo(Path(video))
            if demo is not None:
                media.append(demo)
        if video_dir is not None:
            shutil.rmtree(video_dir, ignore_errors=True)
        if self.secret_seen:
            for m in media:
                (self.evidence / m["file"]).unlink(missing_ok=True)
            media = []
            status = "blocked"
            summary = "A test credential appeared on screen, so every screenshot and the recording were deleted. " + summary
            demo_note = "No media is kept from a run in which a credential was visible."
        elif not any(m["role"] == "demo" for m in media):
            demo_note = ("No recording was saved." if not self.record_demo or video is None else
                         "The recording was not kept: the screen could not be checked for credentials throughout." if self.scan_gaps
                         else "The recording could not be kept.")
        return {"request_id": self.request.get("id"), "head_sha": self.request.get("head_sha"), "status": status,
                "runner": "factory browser runner (Playwright, Chromium)", "journey": self.journey.get("name"),
                "input": self.mode, "summary": summary, "viewport_observed": seen, "steps": observed, "media": media,
                "secret_seen": self.secret_seen, "credentials_checked": sorted(self.secrets),
                **({"demo_note": demo_note} if demo_note else {})}

    def _shot(self, i: int, name: str, media: list[dict[str, Any]]) -> bool:
        if len([m for m in media if m["role"] == "screenshot"]) >= MAX_SHOTS:
            return False
        path = self.evidence / f"step-{i:02d}.png"
        try:
            self.driver.screenshot(path)
        except Exception:  # noqa: BLE001
            return False
        if not path.is_file():
            return False
        media.append({"file": path.name, "role": "screenshot", "caption": self.redact(name)[:200]})
        return True

    def _demo(self, video: Path) -> dict[str, Any] | None:
        """The recording as demo.webm, plus demo.gif when ffmpeg is present (a GIF is what a pull request on GitHub can
        show inline); the GIF is the demo when it fits, otherwise the WebM."""
        webm = self.evidence / "demo.webm"
        shutil.move(str(video), webm)
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            gif = self.evidence / "demo.gif"
            subprocess.run([ffmpeg, "-loglevel", "error", "-y", "-i", str(webm), "-vf",
                            "fps=6,scale='min(720,iw)':-1:flags=lanczos", str(gif)], timeout=120, check=False)
            if gif.is_file() and 0 < gif.stat().st_size <= 10 * 1024 * 1024:
                webm.unlink()
                return {"file": gif.name, "role": "demo", "caption": self.redact(self.journey.get("name") or "Demo")[:200]}
            gif.unlink(missing_ok=True)
        return {"file": webm.name, "role": "demo", "caption": self.redact(self.journey.get("name") or "Demo")[:200]}


def secrets_from_env(env: dict[str, str]) -> dict[str, str]:
    """The values to keep off screen and out of every report: the recipe's declared secrets (FACTORY_SECRET_NAMES), and
    any variable whose name says it holds a key, token, secret or password."""
    declared = {n.strip() for n in env.get("FACTORY_SECRET_NAMES", "").split(",") if n.strip()}
    named = {k for k in env if SECRET_NAME.search(k) and not k.startswith("FACTORY_") and len(env[k]) >= 12}  # short values are settings
    return {k: env[k] for k in sorted(declared | named) if env.get(k)}


def main(argv: list[str] | None = None, driver: Driver | None = None, env: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one change's journey against BASE_URL.")
    parser.add_argument("--journey", required=True, type=Path)
    args = parser.parse_args(argv)
    env = dict(os.environ if env is None else env)
    evidence = Path(env["FACTORY_EVIDENCE_DIR"])
    request = json.loads(Path(env["FACTORY_VISUAL_REQUEST"]).read_text())
    missing = [n.strip() for n in env.get("FACTORY_REQUIRED_SECRETS", "").split(",") if n.strip() and not env.get(n.strip())]
    try:
        journey = json.loads(args.journey.read_text())
        problems = validate_journey(journey, request.get("steps"))
    except (OSError, ValueError) as exc:
        journey, problems = {"steps": []}, [f"unreadable journey: {exc}"]
    if problems or missing:
        reason = (f"required test credentials are not attached: {missing}" if missing else "invalid journey: " + "; ".join(problems))
        report = {"request_id": request.get("id"), "head_sha": request.get("head_sha"), "status": "blocked",
                  "runner": "factory browser runner", "summary": reason[:1500], "media": [], "secret_seen": False,
                  "steps": [{"status": "blocked", "observation": reason[:1500]} for _ in request.get("steps", [])]}
        (evidence / "result.json").write_text(json.dumps(report, indent=2))
        return INVALID if problems else EXIT["blocked"]
    runner = Runner(driver or PlaywrightDriver(), journey, request, env.get("BASE_URL") or request.get("base_url", ""), evidence,
                    secrets_from_env(env), record_demo=env.get("FACTORY_RECORD_DEMO", "1") != "0")
    report = runner.run()
    (evidence / "result.json").write_text(json.dumps(report, indent=2))
    return EXIT[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
