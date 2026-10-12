"""One module per pipeline stage. Each exposes a function that takes a `RunContext`,
drives the session(s) for that stage, persists artifacts, appends events, and returns
what the orchestrator needs to decide the next transition. Stages never call each other.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..config import Config
from ..forge import Forge
from ..gates import ApprovalQueue, Notifier
from ..sessions import SessionDriver
from ..state import Policy
from ..store import RunStore

if TYPE_CHECKING:  # pragma: no cover
    from ..surfaces.artifact_pages import PagePublisher
    from ..surfaces.tracker import TrackerQueue


@dataclass
class RunContext:
    cfg: Config
    store: RunStore
    driver: SessionDriver
    queue: ApprovalQueue
    notifier: Notifier
    forge: Forge | None
    policy: Policy
    run_id: str
    tracker: "TrackerQueue | None" = None       # gate surface = tracker: mirrors work items, holds gates and questions
    pages: "PagePublisher | None" = None        # renders one self-contained HTML page per gate
    evaluation_target: tuple[str, str] | None = None  # owner-authorized comparison branch and pinned base SHA

    def event(self, kind: str, payload: dict[str, Any] | None = None, actor: str = "orchestrator", **extra: Any):
        ev = self.store.append(self.run_id, kind, {"type": actor if actor in ("human", "agent", "platform") else "orchestrator",
                                                   "id": actor}, payload, **extra)
        if self.tracker is not None and kind == "work_item.stage" and extra.get("work_item_id"):
            # The tracker board mirrors the event log; it never drives it.
            self.tracker.mirror_work_item(self.run_id, extra["work_item_id"], (payload or {}).get("stage", ""))
        return ev

    def rubric(self, name: str) -> str:
        return (self.cfg.rubrics_dir / f"{name}.md").read_text()

    def grader_rubric(self, name: str) -> str:
        """The rubric as the in-session outcome grader gets it: everything above the grader marker, where a rubric has
        one. The items below it are computed by the orchestrator after the session; a grader that sees them re-runs the
        whole test plan to anticipate them (~3 minutes a grader pass on live run 2, post-mortem issue 11)."""
        text = self.rubric(name)
        cut = text.find(GRADER_MARKER)
        return text if cut == -1 else text[:cut].rstrip() + "\n"


GRADER_MARKER = "<!-- grader copy ends here"
