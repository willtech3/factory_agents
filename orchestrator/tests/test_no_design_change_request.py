"""Gates 1 and 2 never reach the target repository (ADR-0011): no design branch, commit or change request, and no
comments read back from one, even when a forge is bound."""
from conftest import make_config

from factory import runner
from factory.gates import Decision, GateDecision, LogNotifier
from factory.stages import RunContext
from factory.state import Policy
from factory.store import RunStore
from factory.surfaces.artifact_pages import LocalHtmlPublisher
from factory.surfaces.filequeue import FileQueue

RUN = "run_nodesign"


class TrapForge:
    """A bound forge that fails the test on any use: a gate must not touch the target repository."""
    kind = "github"
    repo = "x/evergreen"

    def __getattr__(self, name):
        raise AssertionError(f"the forge was used before Gate 3: {name}")


def context(tmp_path, root):
    runs = tmp_path / "runs"
    return RunContext(cfg=make_config(root, runs, gate_surface="web"), store=RunStore(runs), driver=None,
                      queue=FileQueue(runs / "_queue"), notifier=LogNotifier(), forge=TrapForge(), policy=Policy(),
                      run_id=RUN, pages=LocalHtmlPublisher(runs))


def test_presenting_a_gate_publishes_the_page_and_leaves_the_forge_alone(tmp_path, root):
    ctx = context(tmp_path, root)
    files = {"decomposition.md": b"# plan", "decomposition.json": b"{}"}
    urls = runner._present(ctx, files, "gate_1", "<html></html>", {"decomposition.md": "full"})
    assert set(urls) == set(files) and all(u.startswith("file://") and "/pages/gate_1.html#" in u for u in urls.values())
    assert urls["decomposition.md"].endswith("#full") and urls["decomposition.json"].endswith("#summary")
    assert [e.kind for e in ctx.store.events(RUN)] == ["artifact.produced"]


def test_feedback_is_the_decision_and_never_reads_the_forge(tmp_path, root):
    ctx = context(tmp_path, root)
    d = GateDecision(gate_id="g1", decision=Decision.REQUEST_CHANGES, decided_by="will", comments="Split the catalog work")
    assert runner._feedback(ctx, d) == "Split the catalog work"
