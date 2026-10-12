#!/usr/bin/env python3
"""CI gate (ADR-0016): every graded role's prompt and rubrics are covered by an eval receipt for their exact bytes.

    python evals/gate.py

Exits 1 naming each role whose prompt or rubric changed since its receipt. Record one of:

    python evals/run_eval.py --recorded <dataset> --agent <role> --replay --receipt   # the recorded sessions, asked again
    factory eval waive --agent <role> --reason "<why this change needs no eval>"       # a person's decision, kept
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "orchestrator"))

from factory import receipts  # noqa: E402


def main() -> int:
    stale = receipts.stale(ROOT)
    for role, files in sorted(stale.items()):
        print(f"{role}: {', '.join(files)} changed since its eval receipt (evals/receipts.json)")
    if stale:
        print("Replay the recorded sessions (evals/run_eval.py --recorded ... --replay --receipt) or record a waiver "
              "(factory eval waive --agent <role> --reason ...).")
        return 1
    print(f"every graded role's prompt and rubric has an eval receipt ({len(receipts.ROLE_FILES)} roles)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
