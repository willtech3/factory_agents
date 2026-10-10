"""One module per pipeline stage. Each exposes a function that takes a `RunContext`,
drives the session(s) for that stage, persists artifacts, appends events, and returns
what the orchestrator needs to decide the next transition. Stages never call each other.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..gates import ApprovalQueue, Notifier
from ..gitlab import GitLabClient
from ..sessions import SessionDriver
from ..state import Policy
from ..store import RunStore


@dataclass
class RunContext:
    cfg: Config
    store: RunStore
    driver: SessionDriver
    queue: ApprovalQueue
    notifier: Notifier
    gitlab: GitLabClient | None
    policy: Policy
    run_id: str

    def event(self, kind: str, payload: dict[str, Any] | None = None, actor: str = "orchestrator", **extra: Any):
        return self.store.append(self.run_id, kind, {"type": actor if actor in ("human", "agent", "platform") else "orchestrator",
                                                     "id": actor}, payload, **extra)

    def rubric(self, name: str) -> str:
        return (self.cfg.rubrics_dir / f"{name}.md").read_text()
