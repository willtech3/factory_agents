#!/usr/bin/env python3
"""Run one stage over one dataset for one agent version, N times, and write a results bundle.

    python evals/run_eval.py --stage spec_judge --dataset spec-defects --runs 5 --agent-version 4
    python evals/run_eval.py --stage decomposer --dataset outcomes-golden --runs 5 --split test
    python evals/run_eval.py --stage reviewer --dataset review-mutations --runs 3

Shape of the loop (the stage-specific `produce_*` functions drive a Managed Agents session
through the orchestrator's SessionDriver with the same prompts production uses, pinned to the
agent version under test; they are stubs here until the first live spike):

    for item in dataset[split]:
        for run in range(N):
            artifact = produce(stage, item, agent_version)        # one session, pinned version
            det = deterministic grade                            # graders/deterministic.py
            sto = stochastic grade (if the stage has a rubric)   # graders/stochastic.py
            record(item, run, det, sto, cost, duration, session_id)
    report: per-metric mean with bootstrap CI; paired discrimination; kappa vs human labels where present

Results go to evals/results/<timestamp>-<stage>-<dataset>-v<agent_version>/ as JSONL plus a summary.json.
Comparisons between two agent versions are made with `compare.py` (same items, same split, non-overlapping
credible intervals as the decision rule). This file deliberately does not try to be clever: an eval harness
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
    ap.add_argument("--stage", required=True, choices=["decomposer", "spec_writer", "spec_judge", "reviewer"])
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--split", default=None)
    ap.add_argument("--agent-version", type=int, default=None)
    ap.add_argument("--from-artifacts", type=Path, default=None,
                    help="Directory of pre-produced artifacts named <item_id>.run<N>.json; grades them instead of producing.")
    args = ap.parse_args()

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
