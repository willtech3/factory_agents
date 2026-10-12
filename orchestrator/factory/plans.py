"""The spec's test plan as the controller runs it (ADR-0014).

A structured command (`{"runner": "pytest" | "bun", "select": [...]}`) and a journey entry (`{"journey": "<name>", ...}`)
are rendered here, so the controller, not the spec writer, owns how a runner is invoked and which reporters it gets (the
harness adds them when it runs the command: `environment_runner.instrumented_command`). A plain string is the shell form
older specs and non-test commands (a build, a linter) use. Everything that reads a test plan's command text reads it
through `plan_of`: verification, the merge checks, Stage 5, the pull request and the spec's reading page.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

# How the controller runs a structured test_plan command. Its own reporters are added when it runs (`instrumented_command`).
RUNNERS = {"pytest": "python3 -m pytest -q", "bun": "bun test"}


def render_command(entry: str | dict[str, Any]) -> str:
    """The shell text of one test_plan command: a string is its own text; `{runner, select}` is the runner's own
    invocation with each selected path or test id quoted, so a selection can never become shell syntax."""
    if isinstance(entry, str):
        return entry
    return " ".join([RUNNERS[entry["runner"]], *(shlex.quote(str(x)) for x in entry.get("select") or [])])


def render_e2e(entry: dict[str, Any]) -> dict[str, Any]:
    """An e2e entry with its command: a `journey` entry is the factory's own browser runner on that journey."""
    if "command" in entry or "journey" not in entry:
        return dict(entry)
    return {**entry, "command": f"factory:journey {entry['journey']}"}


@dataclass(frozen=True)
class Plan:
    """What the controller runs: the test_plan commands as shell text and the e2e entries with their commands."""
    commands: list[str]
    e2e: list[dict[str, Any]]


def plan_of(spec: dict[str, Any]) -> Plan:
    test_plan = spec.get("test_plan") or {}
    return Plan([render_command(c) for c in test_plan.get("commands") or []], [render_e2e(e) for e in test_plan.get("e2e") or []])


def normalized(spec: dict[str, Any]) -> dict[str, Any]:
    """The spec with its test plan rendered (`plan_of`), for the checks and harnesses that read command text."""
    plan = plan_of(spec)
    return {**spec, "test_plan": {**(spec.get("test_plan") or {}), "commands": plan.commands, "e2e": plan.e2e}}
