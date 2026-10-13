#!/usr/bin/env python3
"""Run one stage over one dataset for one agent version, N times, and write a results bundle.

    python evals/run_eval.py --stage spec_judge --dataset spec-defects --runs 5 --agent-version 4
    python evals/run_eval.py --stage decomposer --dataset outcomes-golden --runs 5 --split test
    python evals/run_eval.py --stage reviewer --dataset review-mutations --runs 3
    python evals/run_eval.py --recorded evals/recordings/live --agent merge_judge --regrade
    python evals/run_eval.py --recorded evals/recordings/live --agent merge_judge --replay --runs 3 --receipt

`--recorded` grades a dataset of real sessions (`factory eval export <run_id> --out <dir>`, ADR-0016) instead of a
labelled one: `--regrade` grades the recorded outputs with today's checks, and `--replay` asks the agent version the
controller is configured with the same sessions again, grades its outputs the same way, and compares the two. With
`--receipt`, a replay that does not regress records the role's eval receipt (evals/receipts.json, which CI requires
for every graded role's prompt and rubric: evals/gate.py).

Shape of the loop for a labelled dataset (`produce` is to drive a Managed Agents session through the
orchestrator's SessionDriver with the same prompts production uses, pinned to the agent version under
test; it is still a stub, so `--from-artifacts` grades artifacts made elsewhere):

    for item in dataset[split]:
        for run in range(N):
            artifact = produce(stage, item, agent_version)        # one session, pinned version
            det = deterministic grade                            # graders/deterministic.py
            sto = stochastic grade (if the stage has a rubric)   # graders/stochastic.py
            record(item, run, det, sto, cost, duration, session_id)
    report: per-metric mean with bootstrap CI; paired discrimination; kappa vs human labels where present

Results go to evals/results/<timestamp>-<stage>-<dataset>-v<agent_version>/ as JSONL plus a summary.json.
Two agent versions are to be compared on the same items and split, with non-overlapping credible intervals as
the decision rule (no comparison script exists yet). This file deliberately does not try to be clever: an eval harness
that is hard to read is an eval harness nobody trusts.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from graders import deterministic as det  # noqa: E402
from graders import stochastic as sto  # noqa: E402


def load_items(dataset: str, split: str | None) -> list[dict]:
    items = [json.loads(l) for l in (HERE / "datasets" / dataset / "items.jsonl").read_text().splitlines() if l.strip()]
    if split:
        items = [i for i in items if i.get("split", "test") == split]
    return items


def produce(stage: str, item: dict, agent_version: int | None, run_idx: int) -> dict:
    """Drive one session for `stage` on `item`. Stub: wire to factory.sessions.SessionDriver in spike 2.

    Must return {"artifact": <dict>, "list_cost_usd": float, "duration_s": float, "session_id": str}.
    """
    raise NotImplementedError(
        f"produce({stage}) is not wired to the live driver yet; see docs/roadmap.md spike 2. "
        f"For grader development, pass --from-artifacts <dir> to grade pre-produced artifacts."
    )


# ---------------------------------------------------------------------- recorded sessions (ADR-0016)

def ask_human_default(tool_input: dict, _ctx) -> str:
    """No person answers in an eval: a question gets its own default, as an unattended run would give it."""
    return json.dumps({"answer": tool_input.get("default_if_unanswered") or "No answer in this evaluation; use your default.",
                       "answered_by": "eval"})


def replay_one(driver, item: dict, uploads: dict, run_id: str, n: int):
    """Ask `driver` (a SessionDriver, or a test double) the recorded session `item` again, with the uploads it got."""
    i = item["inputs"]
    tools = {"ask_human": ask_human_default} if "ask_human" in (i.get("custom_tools") or []) else None
    return driver.run(agent_key=item["agent_key"], title=f"eval {i.get('title')} #{n}", run_id=run_id, stage=item["stage"],
                      uploads=uploads, description=i.get("description") or "", rubric_md=i.get("rubric_md"),
                      max_iterations=int(i.get("max_iterations") or 1), budget_usd=float(i.get("budget_usd") or 5.0),
                      custom_tools=tools, model_override=i.get("model_override"), metadata=i.get("metadata"),
                      system_override=i.get("system_override"), environment_id=i.get("environment_id") or None,
                      schema_names=tuple(i["schema_names"]) if i.get("schema_names") else None)


def ok_rate(rows: list[dict]) -> dict:
    oks = [float(r["grade"]["ok"]) for r in rows]
    return {"n": len(oks), "ok": int(sum(oks)), "posterior": sto.beta_credible_interval(int(sum(oks)), len(oks)) if oks else None}


def regressed(baseline: dict, candidate: dict) -> bool:
    """The decision rule: the candidate's credible interval for `ok` lies wholly below the recorded versions'."""
    if not baseline["posterior"] or not candidate["posterior"]:
        return False
    (_, base_lo, _), (_, _, cand_hi) = baseline["posterior"], candidate["posterior"]      # (mean, low, high)
    return cand_hi < base_lo


def run_recorded(args, driver=None, root: Path = HERE.parent) -> dict:
    from factory import receipts, recordings
    schemas = root / "schemas"
    items = [i for i in recordings.load(args.recorded) if i["agent_key"] == args.agent]
    baseline = [{"item_id": i["item_id"], "grade": recordings.grade(args.agent, {n: recordings.blob(args.recorded, sha)
                                                                              for n, sha in i["outputs"].items()},
                                                                     schemas, recordings.spec_of(args.recorded, i)),
                 "labels": i.get("labels"), "agent_version": i.get("agent_version")} for i in items]
    summary: dict = {"agent": args.agent, "dataset": str(args.recorded), "items": len(items), "recorded": ok_rate(baseline)}
    rows = [{"kind": "recorded", **r} for r in baseline]
    if args.replay:
        if driver is None:
            from factory import config as cfgmod
            from factory.sessions import SessionDriver
            driver = SessionDriver(cfgmod.load(), unattended=True)
        run_id = "eval_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        replayed, skipped = [], []
        for item in items:
            uploads = recordings.uploads(args.recorded, item)
            if uploads is None:
                skipped.append(item["item_id"])           # an upload too large to record: this session cannot be asked again
                continue
            recorded = next(b for b in baseline if b["item_id"] == item["item_id"])
            for n in range(args.runs):
                res = replay_one(driver, item, uploads, run_id, n)
                g = recordings.grade(args.agent, res.outputs or {}, schemas, recordings.spec_of(args.recorded, item))
                replayed.append({"kind": "replayed", "item_id": item["item_id"], "run": n, "grade": g,
                                 "same_result": g.get("result") == recorded["grade"].get("result") if "result" in g else None,
                                 "list_cost_usd": getattr(res, "list_cost_usd", None), "session_id": getattr(res, "session_id", None)})
        rows += replayed
        summary.update(replayed=ok_rate(replayed), skipped=skipped,
                       same_result=sto.bootstrap_ci([float(r["same_result"]) for r in replayed if r["same_result"] is not None])
                       if any(r["same_result"] is not None for r in replayed) else None)
        summary["regressed"] = regressed(summary["recorded"], summary["replayed"])
        if args.receipt:
            if summary["regressed"] or not replayed:
                summary["receipt"] = "not recorded: " + ("the replay regressed" if replayed else "nothing could be replayed")
            else:
                receipts.record(root, args.agent, "replayed", dataset=Path(args.recorded).name, items=len(items),
                                runs=args.runs, recorded=summary["recorded"], replayed=summary["replayed"])
                summary["receipt"] = "recorded"
    summary["rows"] = rows
    return summary


def grade(stage: str, item: dict, artifact: dict, aux: dict) -> dict:
    if stage == "decomposer":
        return det.grade_decomposition(item["outcome"], artifact, item)
    if stage == "spec_writer":
        return det.grade_spec(artifact, aux["decomposition"], aux.get("repo_files"))
    if stage == "spec_judge":
        return det.grade_judge_verdict(artifact, item)
    if stage == "reviewer":
        return det.grade_review(artifact, item)
    raise ValueError(stage)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["decomposer", "spec_writer", "spec_judge", "reviewer"])
    ap.add_argument("--dataset")
    ap.add_argument("--recorded", type=Path, default=None, help="A dataset of recorded sessions (factory eval export)")
    ap.add_argument("--agent", default=None, help="With --recorded: the role whose sessions are graded")
    ap.add_argument("--regrade", action="store_true", help="With --recorded: grade the recorded outputs only")
    ap.add_argument("--replay", action="store_true", help="With --recorded: ask the configured agent version again")
    ap.add_argument("--receipt", action="store_true", help="With --replay: record the role's eval receipt unless it regressed")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--split", default=None)
    ap.add_argument("--agent-version", type=int, default=None)
    ap.add_argument("--from-artifacts", type=Path, default=None,
                    help="Directory of pre-produced artifacts named <item_id>.run<N>.json; grades them instead of producing.")
    args = ap.parse_args()
    if args.recorded:
        if not args.agent:
            ap.error("--recorded needs --agent")
        summary = run_recorded(args)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = HERE / "results" / f"{stamp}-recorded-{args.agent}"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "rows.jsonl").write_text("\n".join(json.dumps(r) for r in summary.pop("rows")) + "\n")
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary, indent=2))
        print(f"wrote {out_dir}")
        return 1 if summary.get("regressed") else 0
    if not args.stage or not args.dataset:
        ap.error("--stage and --dataset, or --recorded and --agent")

    items = load_items(args.dataset, args.split)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = HERE / "results" / f"{stamp}-{args.stage}-{args.dataset}-v{args.agent_version or 'latest'}"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in items:
        for r in range(args.runs):
            t0 = time.time()
            if args.from_artifacts:
                p = args.from_artifacts / f"{item['item_id']}.run{r}.json"
                if not p.exists():
                    continue
                produced = {"artifact": json.loads(p.read_text()), "list_cost_usd": None, "duration_s": None, "session_id": None}
            else:
                produced = produce(args.stage, item, args.agent_version, r)
            g = grade(args.stage, item, produced["artifact"], {})
            rows.append({"item_id": item["item_id"], "pair_id": item.get("pair_id"), "defect_code": item.get("defect_code"),
                         "run": r, "grade": g, "list_cost_usd": produced["list_cost_usd"],
                         "duration_s": produced["duration_s"] or (time.time() - t0), "session_id": produced["session_id"]})
    (out_dir / "rows.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    summary: dict = {"stage": args.stage, "dataset": args.dataset, "runs": args.runs, "items": len(items), "rows": len(rows)}
    if rows and args.stage in ("spec_judge", "reviewer"):
        pairs: dict[str, dict] = {}
        for row in rows:
            if not row["pair_id"]:
                continue
            slot = "clean" if row["defect_code"] == "clean" else "mutant"
            pairs.setdefault(f"{row['pair_id']}:{row['run']}", {"code": None})
            pairs[f"{row['pair_id']}:{row['run']}"][slot] = row["grade"]
            if slot == "mutant":
                pairs[f"{row['pair_id']}:{row['run']}"]["code"] = row["defect_code"]
        flag_key = "detected_at_severity" if args.stage == "spec_judge" else "detected"
        summary["paired_discrimination_by_code"] = det.paired_discrimination(pairs, flag_key)
        detected = [float(r["grade"].get("detected", False)) for r in rows if r["defect_code"] not in (None, "clean")]
        if detected:
            summary["detection_rate"] = sto.bootstrap_ci(detected)
            summary["detection_posterior"] = sto.beta_credible_interval(int(sum(detected)), len(detected))
    if rows and args.stage in ("decomposer", "spec_writer"):
        ok = [float(r["grade"]["all_checks_passed"]) for r in rows]
        summary["deterministic_pass_rate"] = sto.bootstrap_ci(ok)
        by_item: dict[str, list[bool]] = {}
        for r in rows:
            by_item.setdefault(r["item_id"], []).append(bool(r["grade"]["all_checks_passed"]))
        summary["pass_power_k"] = {str(k): sto.pass_power_k(by_item, k) for k in (1, 3, 5) if k <= args.runs}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    print(f"wrote {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
