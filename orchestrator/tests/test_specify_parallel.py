"""Spec writers run all at once by default; dependency-order waves only when the policy asks. No network."""
import json
import threading

from conftest import make_config
from support.sessions import stage_result

from factory.gates import InMemoryQueue, LogNotifier
from factory.stages import RunContext
from factory.stages import specify
from factory.state import Policy
from factory.store import RunStore


class Driver:
    """Records how many writer sessions overlap in time; the judge passes the set immediately."""

    def __init__(self, examples):
        self.examples = examples
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()
        self.gate = threading.Barrier(4, timeout=5)   # four writers must be in flight together to pass

    def run(self, **kw):
        self.uploads_seen = getattr(self, "uploads_seen", []); self.uploads_seen.append((kw["agent_key"], sorted(kw["uploads"])))
        self.calls = getattr(self, "calls", []); self.calls.append(kw)
        round_no = int(kw["title"].rsplit(" r", 1)[1])
        if kw["agent_key"] == "spec_writer":
            with self.lock:
                self.active += 1; self.peak = max(self.peak, self.active)
            try:
                self.gate.wait()
            except threading.BrokenBarrierError:
                pass
            with self.lock:
                self.active -= 1
            wid = kw["work_item_id"]
            spec = dict(self.examples["spec_smart_retries"], spec_id=f"spec_{wid.removeprefix('wi-')}", work_item_id=wid)
            return stage_result({"spec.json": json.dumps(spec).encode(), "spec.md": f"# {wid} as written in round {round_no}".encode()},
                                agent_id="w", cost=0.1)
        self.judge_uploads = sorted(kw["uploads"])
        verdict = dict(self.examples["verdict_round2"], round=round_no)
        revise = getattr(self, "revise_first", {})
        if revise and round_no == 1:                      # round 1: send the named specs back with one issue each
            import copy
            verdict = copy.deepcopy(verdict)
            verdict["overall"].update(result="revise", blocking_issue_count=len(revise))
            for ps in verdict["per_spec"]:
                if ps["spec_id"] in revise:
                    ps["issues"] = [{"issue_id": "I-1", "severity": "blocking", "location": ps["spec_id"],
                                     "description": "tighten AC-3", "suggested_resolution": "say which clock"}]
        return stage_result({"verdict.json": json.dumps(verdict).encode()}, sid="j", agent_id="j", cost=0.1,
                            outcome_result=None, iterations=0)


def _ctx(tmp_path, root, examples, policy):
    ctx = RunContext(cfg=make_config(root, tmp_path), store=RunStore(tmp_path), driver=Driver(examples), queue=InMemoryQueue(),
                     notifier=LogNotifier(), forge=None, policy=policy, run_id="run_parallel0001")
    ctx.event("run.created", {"policy": {"preview_mode": "local"}, "budget_usd": 50, "outcome_id": "o", "title": "t"})
    ctx.event("stage.entered", {"stage": "decomposing"})
    ctx.event("artifact.produced", {"artifact_type": "decomposition", "revision": 1, "work_items": examples["decomposition"]["work_items"]})
    ctx.event("stage.entered", {"stage": "gate_1_decomposition"})
    return ctx


def test_all_specs_are_written_at_once_by_default(tmp_path, root, examples):
    ctx = _ctx(tmp_path, root, examples, Policy())
    specs, verdict = specify.run_specify(ctx, examples["outcome"], examples["decomposition"])
    assert len(specs) == 4 and ctx.driver.peak == 4            # the chain of dependencies did not serialise the writers
    assert verdict["overall"]["result"] == "pass"
    # the judge gets both the normalised JSON and the writer's spec.md for every spec (rubric S10 reads the prose)
    assert "specs/spec_smart-retries.json" in ctx.driver.judge_uploads and "specs/spec_smart-retries.md" in ctx.driver.judge_uploads


def test_waves_policy_serialises_dependent_items(tmp_path, root, examples):
    ctx = _ctx(tmp_path, root, examples, Policy(spec_waves=True))
    ctx.driver.gate = threading.Barrier(1)                       # no overlap required
    specs, _ = specify.run_specify(ctx, examples["outcome"], examples["decomposition"])
    assert len(specs) == 4 and ctx.driver.peak < 4


def test_gate_2_send_back_keeps_every_round_and_hands_each_consumer_this_rounds_markdown(tmp_path, root, examples):
    """Post-mortem issue 1: a Gate 2 request-changes re-enters run_specify with a round offset and the previous verdict.
    Round numbers keep counting (cycle B never overwrites cycle A's artefacts), the judge reads the spec.md the writer
    produced in that round (not a stale one from the store), and the first judge after the send-back sees the previous
    verdict beside the owner's comments."""
    ctx = _ctx(tmp_path, root, examples, Policy())
    ctx.driver.gate = threading.Barrier(1)
    outcome, decomposition = examples["outcome"], examples["decomposition"]
    sid, wid = "spec_smart-retries", "wi-smart-retries"

    specs_a, verdict_a = specify.run_specify(ctx, outcome, decomposition)                       # cycle A
    judge_a = [c for c in ctx.driver.calls if c["agent_key"] == "spec_judge"][-1]
    assert verdict_a["round"] == 1 and specs_a[sid]["_markdown"] == f"# {wid} as written in round 1"
    assert judge_a["uploads"][f"specs/{sid}.md"] == specs_a[sid]["_markdown"].encode()
    assert "previous_verdict.json" not in judge_a["uploads"]
    assert "_markdown" not in json.loads(judge_a["uploads"][f"specs/{sid}.json"])                # private key never uploaded

    # the owner sends the set back: the runner passes the drafts, the notes, the verdict and the round offset
    prior = {s["work_item_id"]: s for s in specs_a.values()}
    notes = {w: "# Notes\n- overdue means due before today" for w in prior}
    specs_b, verdict_b = specify.run_specify(ctx, outcome, decomposition, notes, prior,
                                             previous_verdict=verdict_a, round_offset=verdict_a["round"])
    assert verdict_b["round"] == 2 and all(s["revision"] == 2 for s in specs_b.values())
    judge_b = [c for c in ctx.driver.calls if c["agent_key"] == "spec_judge"][-1]
    assert judge_b["uploads"][f"specs/{sid}.md"] == f"# {wid} as written in round 2".encode() != judge_a["uploads"][f"specs/{sid}.md"]
    assert json.loads(judge_b["uploads"]["previous_verdict.json"])["round"] == 1 and "human_comments.md" in judge_b["uploads"]
    assert "This is round 2" in judge_b["description"]
    writers_b = [c for c in ctx.driver.calls if c["agent_key"] == "spec_writer" and c["title"].endswith(" r2")]
    assert len(writers_b) == 4 and all("prior/spec.json" in c["uploads"] and "human_comments.md" in c["uploads"] for c in writers_b)

    # both rounds are in the store, under their own names, and spec.json holds no private keys
    d = tmp_path / ctx.run_id / "artifacts" / "specify"
    assert (d / f"{wid}-r1-spec.md").read_text() == f"# {wid} as written in round 1"
    assert (d / f"{wid}-r2-spec.md").read_text() == f"# {wid} as written in round 2"
    assert "_markdown" not in json.loads((d / f"{wid}-r2-spec.json").read_text())
    rounds = [e.payload["spec_round"] for e in ctx.store.events(ctx.run_id)
              if e.kind == "work_item.stage" and e.work_item_id == wid and "spec_round" in e.payload]
    assert rounds == [1, 2]


def test_revise_round_gives_a_writer_its_related_siblings_and_its_own_prior_draft(tmp_path, root, examples):
    """Post-mortem issue 15: the sibling filter compared spec ids with work item ids and leaked every spec (the writer's
    own included); the writer's previous draft reached it only on the first round after a Gate 2 send-back."""
    from factory.stages.specify import sibling_specs

    items = {w["work_item_id"]: w for w in examples["decomposition"]["work_items"]}        # everything depends on smart-retries
    specs = {f"spec_{w.removeprefix('wi-')}": {"spec_id": f"spec_{w.removeprefix('wi-')}", "work_item_id": w} for w in items}
    assert set(sibling_specs(items, "wi-dunning-emails", specs)) == {"spec_smart-retries"}             # its dependency, not itself
    assert set(sibling_specs(items, "wi-smart-retries", specs)) == {"spec_dunning-emails", "spec_at-risk-admin", "spec_suspension-timing"}
    assert sibling_specs(items, "wi-at-risk-admin", {"spec_at-risk-admin": specs["spec_at-risk-admin"]}) == {}

    ctx = _ctx(tmp_path, root, examples, Policy())
    ctx.driver.gate = threading.Barrier(1)
    ctx.driver.revise_first = {"spec_dunning-emails"}
    specs, verdict = specify.run_specify(ctx, examples["outcome"], examples["decomposition"])
    assert verdict["round"] == 2 and verdict["overall"]["result"] == "pass"
    r2 = {c["work_item_id"]: c for c in ctx.driver.calls if c["agent_key"] == "spec_writer" and c["title"].endswith(" r2")}
    assert "wi-dunning-emails" in r2 and "wi-suspension-timing" not in r2     # sent back (plus the example verdict's coherence routing)
    up = r2["wi-dunning-emails"]["uploads"]
    assert "sibling_specs/spec_smart-retries.json" in up and "sibling_specs/spec_dunning-emails.json" not in up   # its dependency, never itself
    assert not any(k.startswith("sibling_specs/spec_at-risk") or k.startswith("sibling_specs/spec_suspension") for k in up)
    prior = json.loads(up["prior/spec.json"])
    assert prior["work_item_id"] == "wi-dunning-emails" and prior["revision"] == 1 and "_markdown" not in prior
    assert json.loads(up["judge_issues.json"])[0]["issue_id"] == "I-1"
    root_up = r2["wi-smart-retries"]["uploads"]                                 # the root item sees every dependant, not itself
    assert {k for k in root_up if k.startswith("sibling_specs/")} == {"sibling_specs/spec_dunning-emails.json", "sibling_specs/spec_at-risk-admin.json",
                                                                       "sibling_specs/spec_suspension-timing.json"}
    assert json.loads(root_up["prior/spec.json"])["work_item_id"] == "wi-smart-retries"


def test_a_re_judgement_gets_what_changed_and_re_scores_only_that(tmp_path, root, examples):
    """Post-mortem issue 9: round-2 judges re-judged every criterion from scratch and cost more than round 1."""
    ctx = _ctx(tmp_path, root, examples, Policy())
    ctx.driver.gate = threading.Barrier(1)
    ctx.driver.revise_first = {"spec_dunning-emails"}
    specify.run_specify(ctx, examples["outcome"], examples["decomposition"])
    judges = [c for c in ctx.driver.calls if c["agent_key"] == "spec_judge"]
    rewritten = {c["work_item_id"].removeprefix("wi-") for c in ctx.driver.calls
                 if c["agent_key"] == "spec_writer" and c["title"].endswith(" r2")}
    assert not any(k.startswith("changes/") for k in judges[0]["uploads"]) and "re-judgement" not in judges[0]["description"]
    changes = {k for k in judges[1]["uploads"] if k.startswith("changes/")}
    assert changes == {f"changes/spec_{w}.diff" for w in rewritten} and "changes/spec_dunning-emails.diff" in changes
    assert "re-judgement" in judges[1]["description"] and "Re-score only" in judges[1]["description"]
    untouched = sorted(f"spec_{w.removeprefix('wi-')}" for w in (i["work_item_id"] for i in examples["decomposition"]["work_items"])
                       if w.removeprefix("wi-") not in rewritten)
    assert f"unchanged: {', '.join(untouched)}" in judges[1]["description"]
    diff = judges[1]["uploads"]["changes/spec_dunning-emails.diff"].decode()
    assert diff.startswith("--- previous/spec_dunning-emails.json") and '"revision": 2' in diff


def test_a_user_visible_criterion_without_an_e2e_entry_is_sent_back_before_gate_2(tmp_path, root, examples):
    """e2e_coverage is a property of the spec: checked only at merge, an approved spec missing an entry failed a check
    no implementer round could fix."""
    import copy
    uncovered = copy.deepcopy(examples)
    uncovered["spec_smart_retries"]["test_plan"]["e2e"] = [e for e in uncovered["spec_smart_retries"]["test_plan"]["e2e"]
                                                           if "AC-8" not in e["covers_ac"]]
    ctx = _ctx(tmp_path, root, uncovered, Policy(spec_judge_max_rounds=1))
    ctx.driver.gate = threading.Barrier(1)
    _, verdict = specify.run_specify(ctx, examples["outcome"], examples["decomposition"])
    assert verdict["overall"]["result"] == "revise"
    issues = [i for ps in verdict["per_spec"] for i in ps["issues"] if i["issue_id"] == "DET-e2e_coverage"]
    assert issues and all(i["severity"] == "blocking" and "AC-8" in i["description"] for i in issues)


def test_spec_changes_lists_changed_and_unchanged_specs_and_skips_new_ones():
    previous = {"spec_a": {"spec_id": "spec_a", "goal": "x", "_markdown": "old"}, "spec_b": {"spec_id": "spec_b", "goal": "y"}}
    current = {"spec_a": {"spec_id": "spec_a", "goal": "x", "_markdown": "new prose only"},
               "spec_b": {"spec_id": "spec_b", "goal": "z"}, "spec_c": {"spec_id": "spec_c"}}
    changed, unchanged = specify.spec_changes(previous, current)
    assert unchanged == ["spec_a"] and set(changed) == {"spec_b"} and '+  "goal": "z"' in changed["spec_b"]
