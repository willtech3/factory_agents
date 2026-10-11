"""The run record: one spec's full evidence, kept in Factory's artifact store and shown on the Console's run page.

The change request description is written for the code reviewer and links here once (`pr_description.py`). Everything
it leaves out lives in this record: the acceptance criteria with the judge's evidence and the orchestrator's covering
e2e rows, every review finding and how it was settled, the deterministic checks and the verification environment,
every screenshot and recording with its step observations, the judge's residual risks and full summary, and
provenance with per-session cost. Post-mortem 2026-10-11 §7. Built only from records the controller already holds;
nothing here is an agent's claim that was not already in the verdict or the findings.
"""
from __future__ import annotations

import json
from typing import Any

STAGE = "integrate"
ENVIRONMENT_KEYS = ("mode", "host", "status", "head_sha", "session_id", "environment_id", "profile_path", "profile_sha256",
                    "app_base_url", "installer", "python", "install", "detail")


def name(spec_id: str) -> str:
    return f"{spec_id}-run_record.json"


def build(*, run_id: str, spec: dict[str, Any], verdict: dict[str, Any], findings: dict[str, Any] | None,
          response: dict[str, Any] | None, test_results: dict[str, Any] | None, run_summary: dict[str, Any] | None,
          visual_evidence: list[dict[str, Any]], diff_stats: dict[str, Any] | None, base_branch: str,
          artifact_links: dict[str, str] | None = None) -> dict[str, Any]:
    findings, response, test_results, run_summary = findings or {}, response or {}, test_results or {}, run_summary or {}
    summary = verdict.get("pr_summary") if isinstance(verdict.get("pr_summary"), dict) else {}
    head = verdict.get("_head_sha")
    return {
        "run_id": run_id, "spec_id": spec["spec_id"], "work_item_id": spec.get("work_item_id"), "title": spec.get("title"),
        "branch": verdict.get("_branch"), "base_branch": base_branch, "head_sha": head, "round": verdict.get("round"),
        "what_changed": summary.get("what_changed"), "decisions": summary.get("decisions") or [],
        "criteria": criteria(spec, verdict, findings, test_results),
        "findings": settled_findings(findings, response, verdict),
        "checks": [c for c in verdict.get("deterministic_checks") or [] if isinstance(c, dict)],
        "verification": verification(test_results),
        "media": media(visual_evidence, head),
        "residual_risks": list(verdict.get("residual_risks") or []),
        "judge_summary": verdict.get("summary_for_human"),
        "size": diff_stats,
        "provenance": provenance(run_summary),
        "artifact_links": artifact_links or {},
        "pull_request": None,
    }


def criteria(spec: dict[str, Any], verdict: dict[str, Any], findings: dict[str, Any], test_results: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per acceptance criterion: the judge's status and evidence, the orchestrator's e2e rows that name it, and
    the reviewers' status (for information; it never decides the row)."""
    judged = {c.get("ac_id"): c for c in verdict.get("criteria") or [] if isinstance(c, dict)}
    reviewed = {c.get("ac_id"): c.get("status") for c in findings.get("spec_conformance") or [] if isinstance(c, dict)}
    e2e = [r for r in test_results.get("e2e") or [] if isinstance(r, dict)]
    rows = []
    for ac in spec.get("acceptance_criteria", []):
        aid = ac["ac_id"]
        rows.append({"ac_id": aid, "given": ac.get("given"), "when": ac.get("when"), "then": ac.get("then"),
                     "status": (judged.get(aid) or {}).get("status", "not_assessed"),
                     "evidence": (judged.get(aid) or {}).get("evidence", ""),
                     "e2e": [{"command": r.get("command"), "target": r.get("target"), "exit_code": r.get("exit_code"),
                              "visual": (r.get("visual") or {}).get("status")}
                             for r in e2e if aid in (r.get("covers_ac") or [])],
                     "reviewer_status": reviewed.get(aid)})
    return rows


def settled_findings(findings: dict[str, Any], response: dict[str, Any], verdict: dict[str, Any]) -> list[dict[str, Any]]:
    """Every finding of the final merged set with the implementer's answer, the final status and the judge's view;
    `contested` marks the ones the implementer disputed or a reviewer re-adjudicated."""
    answered = {r.get("finding_id"): r for r in response.get("responses") or [] if isinstance(r, dict)}
    judged = {u.get("finding_id"): u.get("judge_assessment") for u in verdict.get("unresolved_findings") or [] if isinstance(u, dict)}
    out = []
    for f in findings.get("findings") or []:
        if not isinstance(f, dict):
            continue
        fid = f.get("finding_id")
        answer = answered.get(fid) or {}
        disposition = answer.get("disposition")
        out.append({"finding_id": fid, "severity": f.get("severity"), "category": f.get("category"), "claim": f.get("claim"),
                    "pass": f.get("pass"), "disposition": disposition, "explanation": answer.get("explanation"),
                    "final_status": f.get("status"), "judge_assessment": judged.get(fid),
                    "contested": disposition in ("refuted", "accepted_risk") or f.get("status") in ("upheld", "withdrawn")})
    return out


def verification(test_results: dict[str, Any]) -> dict[str, Any]:
    env = test_results.get("environment") if isinstance(test_results.get("environment"), dict) else {}
    commands = []
    for r in test_results.get("commands") or []:
        if not isinstance(r, dict):
            continue
        tests = r.get("tests") if isinstance(r.get("tests"), dict) else r.get("pytest") if isinstance(r.get("pytest"), dict) else {}
        commands.append({"command": r.get("command"), "exit_code": r.get("exit_code"), "status": r.get("status"),
                         "location": r.get("location"), "passed": len(tests.get("passed") or []),
                         "failed": len(tests.get("failed") or []), "skipped": len(tests.get("skipped") or [])})
    e2e = [{"command": r.get("command"), "target": r.get("target"), "covers_ac": r.get("covers_ac") or [],
            "exit_code": r.get("exit_code"), "location": r.get("location"), "visual": (r.get("visual") or {}).get("status")}
           for r in test_results.get("e2e") or [] if isinstance(r, dict)]
    return {"environment": {k: env[k] for k in ENVIRONMENT_KEYS if env.get(k) is not None},
            "preview": {k: v for k, v in (test_results.get("preview") or {}).items() if k in ("status", "mode", "url", "pipeline", "head_sha")},
            "commands": commands, "e2e": e2e}


def media(visual_evidence: list[dict[str, Any]], head_sha: str | None) -> list[dict[str, Any]]:
    """Every browser check of this spec, newest first, with its observations and media artifacts. `current` marks the
    checks of the head the judge approved; earlier heads stay listed so the record shows what changed between rounds."""
    out = []
    for ev in visual_evidence:
        out.append({"description": ev.get("description"), "status": ev.get("status"), "head_sha": ev.get("head_sha"),
                    "current": bool(head_sha) and ev.get("head_sha") == head_sha, "persona": ev.get("persona"),
                    "viewport": ev.get("viewport"), "summary": ev.get("summary"), "summary_source": ev.get("summary_source"),
                    "steps_observed": ev.get("steps_observed") or [], "demo_note": ev.get("demo_note"),
                    "media": [{k: m.get(k) for k in ("name", "role", "mime_type", "bytes", "sha256", "caption", "artifact")}
                              for m in ev.get("media") or [] if isinstance(m, dict) and m.get("artifact")]})
    return out


def provenance(run_summary: dict[str, Any]) -> dict[str, Any]:
    keys = ("stage", "agent_name", "agent_version", "agent_key", "session_id", "console_url", "duration", "duration_seconds",
            "list_cost_usd", "stop_reason")
    return {"sessions": [{k: s.get(k) for k in keys} for s in run_summary.get("sessions") or [] if isinstance(s, dict)],
            "sessions_cost_usd": run_summary.get("sessions_cost_usd"), "sessions_duration": run_summary.get("sessions_duration"),
            "run_cost_usd": run_summary.get("list_cost_usd")}


def store(ctx: Any, record: dict[str, Any]) -> str:
    """Write the record (the controller's own derived file: a resumed integration or the opened pull request refreshes
    it) and announce it once per content."""
    sid = record["spec_id"]
    digest = ctx.store.put_artifact(ctx.run_id, STAGE, name(sid), json.dumps(record, indent=2, default=str).encode(), replace=True)
    ctx.event("artifact.produced", {"artifact_type": "run_record", "path": f"{STAGE}/{name(sid)}", "sha256": digest},
              work_item_id=record.get("work_item_id"), spec_id=sid, stage=STAGE)
    return digest


def load_all(artifacts_root: Any) -> list[dict[str, Any]]:
    """Every run record under a run's artifacts directory, for the Console."""
    out = []
    for p in sorted((artifacts_root / STAGE).glob("*-run_record.json")) if (artifacts_root / STAGE).is_dir() else []:
        try:
            record = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("spec_id"):
            out.append(record)
    return out


__all__ = ["build", "load_all", "name", "store"]
