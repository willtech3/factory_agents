"""A dependent work item is built on its dependency's branch while that change request is unmerged."""
from conftest import make_config

from factory.gates import InMemoryQueue, LogNotifier
from factory.runner import base_branch_for
from factory.stages import RunContext
from factory.state import Policy
from factory.store import RunStore


class Forge:
    def __init__(self, states):
        self.states = states

    def change_request_state(self, cr_id):
        return {"state": self.states[cr_id]}


def _ctx(tmp_path, root, forge):
    return RunContext(cfg=make_config(root, tmp_path), store=RunStore(tmp_path), driver=None, queue=InMemoryQueue(),
                      notifier=LogNotifier(), forge=forge, policy=Policy(), run_id="run_stack000001")


def _spec(sid, deps):
    return {"spec_id": sid, "work_item_id": sid.replace("spec_", "wi-"), "dependencies": {"specs": deps}}


def test_base_branch_follows_the_dependency_change_request(tmp_path, root):
    ctx = _ctx(tmp_path, root, Forge({1: "opened", 2: "merged", 3: "closed", 4: "opened"}))
    crs = {"spec_a": 1, "spec_b": 2, "spec_c": 3, "spec_d": 4}
    assert base_branch_for(ctx, _spec("spec_x", []), crs, "main") == ("main", None)
    assert base_branch_for(ctx, _spec("spec_x", ["spec_b"]), crs, "main") == ("main", None)                      # merged
    assert base_branch_for(ctx, _spec("spec_x", ["spec_a"]), crs, "main") == ("factory/run_stack000001/spec_a", None)  # stacked
    assert base_branch_for(ctx, _spec("spec_x", ["spec_outside"]), crs, "main") == ("main", None)                # not ours
    base, why = base_branch_for(ctx, _spec("spec_x", ["spec_c"]), crs, "main")
    assert base == "main" and "closed without merging" in why
    base, why = base_branch_for(ctx, _spec("spec_x", ["spec_a", "spec_d"]), crs, "main")
    assert base == "main" and "2 unmerged" in why


def test_without_a_forge_an_open_dependency_still_stacks(tmp_path, root):
    ctx = _ctx(tmp_path, root, None)
    assert base_branch_for(ctx, _spec("spec_x", ["spec_a"]), {"spec_a": 7}, "main") == ("factory/run_stack000001/spec_a", None)
