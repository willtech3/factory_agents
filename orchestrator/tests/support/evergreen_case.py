"""The evergreen_chat#120 case, replayed through the real integrate stage: a 71-line product change with its tests and a
regenerated catalog, three passing browser checks with screenshots and a demo, a review that contested ten findings,
and a merge judge verdict whose run-record summary is full of factory ids and first-person text. Post-mortem
2026-10-11. Test support only."""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from factory import visual

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/k9sAAAAASUVORK5CYII=')
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
SPEC_ID, WORK_ITEM = "spec_frontier-models", "wi-frontier-models"
BRANCH = "factory/run_budget000001/spec_frontier-models"
JOURNEYS = {"desktop-pointer": (1440, 900), "desktop-keyboard": (1440, 900), "mobile-touch": (390, 844)}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def repository(tmp_path: Path, extra_files: dict[str, str] | None = None) -> tuple[Path, Path, str, str]:
    """(origin, work clone, base sha, head sha): main holds the app; the factory branch adds four models, their tests, an
    evidence fixture and a regenerated catalog snapshot (plus `extra_files`)."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True)
    git(work, "config", "user.email", "dev@example.com")
    git(work, "config", "user.name", "dev")
    (work / "src/lib").mkdir(parents=True)
    (work / "tests").mkdir()
    (work / "catalog-watch").mkdir()
    (work / "src/lib/models.ts").write_text("export const MODELS = [\n" + "".join(f"  {{ id: 'm{i}' }},\n" for i in range(25)) + "];\n")
    (work / "tests/models.test.ts").write_text("import { MODELS } from '../src/lib/models';\n")
    (work / "catalog-watch/openrouter-models.json").write_text("".join(f'{{"id": "old-{i}"}}\n' for i in range(2445)))
    (work / "catalog-watch/report.md").write_text("".join(f"row {i}\n" for i in range(16)))
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "app")
    git(work, "push", "-q", "origin", "HEAD:main")
    base = git(work, "rev-parse", "HEAD")
    git(work, "checkout", "-q", "-b", BRANCH)
    models = (work / "src/lib/models.ts").read_text().replace("];\n", "".join(f"  {{ id: 'new-{i}' }},\n" for i in range(71)) + "];\n")
    (work / "src/lib/models.ts").write_text(models)
    (work / "tests/models.test.ts").write_text((work / "tests/models.test.ts").read_text() + "".join(f"test('{i}', () => {{}});\n" for i in range(532)))
    (work / "tests/fixtures/frontier-models").mkdir(parents=True)
    (work / "tests/fixtures/frontier-models/catalog-evidence.json").write_text("".join(f'{{"raw": {i}}}\n' for i in range(492)))
    (work / "catalog-watch/openrouter-models.json").write_text("".join(f'{{"id": "new-{i}"}}\n' for i in range(5084)))
    (work / "catalog-watch/report.md").write_text("".join(f"new row {i}\n" for i in range(59)))
    for path, text in (extra_files or {}).items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(text)
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "add frontier models")
    git(work, "push", "-q", "-u", "origin", BRANCH)
    return origin, work, base, git(work, "rev-parse", "HEAD")


def spec() -> dict[str, Any]:
    criteria = [{"ac_id": f"AC-{i}", "given": f"given {i}", "when": f"when {i}", "then": f"then {i}", "verification": "automated_test",
                 "user_visible": i >= 9, **({"surface": "ui"} if i >= 9 else {})} for i in range(1, 13)]
    e2e = [{"command": f"factory:journey {name}", "target": "preview", "description": f"Pick and chat ({name})",
            "covers_ac": ["AC-9", "AC-12"] if name == "desktop-pointer" else ["AC-10"] if name == "desktop-keyboard" else ["AC-11"],
            "visual": {"persona": "Chat member", "viewport": {"width": w, "height": h},
                       "steps": ["Open the picker", "Select Claude Haiku 5.5", "Send and watch the reply stream"],
                       "environment": {"name": "Hosted app", "data": "synthetic", "authentication": "test-identity"}}}
           for name, (w, h) in JOURNEYS.items()]
    return {"spec_id": SPEC_ID, "work_item_id": WORK_ITEM, "title": "Add source-backed frontier models to the picker",
            "goal": "Users can pick Claude Opus 5.5, Sonnet 5.5, Haiku 5.5 and GPT-6.1 Sol, with limits and prices from the catalog.",
            "acceptance_criteria": criteria, "allowed_paths": ["src/lib/models.ts", "tests/**", "catalog-watch/**"],
            "estimated_size": "S", "estimated_lines": {"hand_written": 120, "app": 70, "tests": 50},
            "regenerated": [{"paths": ["catalog-watch/openrouter-models.json", "catalog-watch/report.md"], "command": "bun run catalog:refresh"}],
            "test_plan": {"commands": ["bun test tests/models.test.ts", "bun run lint", "bunx tsc --noEmit"], "e2e": e2e}}


def verdict(base: str, head: str) -> dict[str, Any]:
    findings = [("F-1", "withdrawn"), ("F-2", "withdrawn"), ("F-3", "withdrawn"), ("F-4", "withdrawn"), ("F-5", "withdrawn"),
                ("F-6", "fixed"), ("F-7", "fixed"), ("F-8", "fixed"), ("F-9", "fixed"), ("F-10", "fixed")]
    return {
        "verdict_id": "mrv_frontier001", "spec_id": SPEC_ID, "round": 3, "result": "ready",
        "unresolved_findings": [{"finding_id": f, "reviewer_status": s, "judge_assessment": f"I re-ran the reproduction for {f}; settled."}
                                for f, s in findings],
        "criteria": [{"ac_id": f"AC-{i}", "status": "met", "evidence": f"commands[0] passed; e2e row {i} exit 0"} for i in range(1, 13)],
        "deterministic_checks": [{"check": c, "passed": True, "detail": "ok"} for c in
                                 ("verification_setup", "hosted_environment", "diff_scope", "protected_tests", "test_weakening",
                                  "ac_test_mapping", "test_plan_commands", "e2e_coverage", "visual_e2e", "secret_scan", "manifest_valid", "e2e_preview")],
        "residual_risks": ["Only Claude Haiku 5.5 was live-chatted; the other three are metadata-verified only.",
                           "I viewed 11 of the 21 hosted PNGs; the DET-implementation_head row is absent this round."],
        "pr_summary": {
            "what_changed": ("Adds Claude Opus 5.5, Claude Sonnet 5.5, Claude Haiku 5.5 and GPT-6.1 Sol to the model picker. "
                             "Each entry's limits, prices, input types and reasoning efforts come from the refreshed OpenRouter catalog, "
                             "so the picker matches what the provider actually serves."),
            "how_tested": ("The model tests compare every new entry with the catalog snapshot and check that defaults and saved "
                           "favourites are unchanged; the browser checks pick Claude Haiku 5.5, stream a reply, reload and reopen it."),
            "decisions": [
                {"decision": "Only Claude Haiku 5.5 was chatted with live; the other three were checked against catalog metadata only",
                 "location": "src/lib/models.ts:212", "affects_product": False},
                {"decision": "The picker labels the xhigh effort as Max, and the catalog's own max effort was dropped, so choosing Max sends xhigh",
                 "location": "src/lib/models.ts:41", "affects_product": True},
            ],
            "read_first": [{"path": "src/lib/models.ts", "why": "the four new entries"},
                           {"path": "tests/models.test.ts", "why": "how each entry is checked against the catalog"}],
        },
        "summary_for_human": ("I re-ran the Bun suites at 477321f. F-7 (blocking) was fixed by the implementer in round 3; the merge "
                              "judge settled DET-implementation_head. AC-13 and AC-14 covered the journey's result.json. "
                              "See http://127.0.0.1:8772/api/artifacts/run_1bf2f7742b/implement/x.json"),
        "produced_by": {"agent_name": "Merge judge", "session_id": "sesn_01judge", "list_cost_usd": 2.08},
        "_head_sha": head, "_branch": BRANCH, "_base_sha": base,
    }


def findings() -> dict[str, Any]:
    out = []
    for i in range(1, 11):
        status = "withdrawn" if i <= 5 else "fixed"
        out.append({"finding_id": f"F-{i}", "severity": "blocking" if i in (1, 6, 7, 8) else "minor", "category": "test_quality",
                    "claim": f"journey plumbing defect {i}", "status": status, "pass": "1",
                    "evidence": {"locations": [f"verification/frontier-models/browser_journey.py:{100 + i}"]}})
    return {"findings": out, "spec_conformance": [{"ac_id": f"AC-{i}", "status": "met" if i < 9 else "not_assessed"} for i in range(1, 13)]}


def response() -> dict[str, Any]:
    return {"responses": [{"finding_id": f"F-{i}", "disposition": "refuted" if i == 5 else "fixed", "explanation": "see commit"}
                          for i in range(1, 11)]}


def run_summary() -> dict[str, Any]:
    sessions = [{"stage": stage, "agent_name": stage, "agent_version": 3, "session_id": f"sesn_{stage}_{i}",
                 "console_url": f"https://platform.claude.com/sessions/sesn_{stage}_{i}", "list_cost_usd": 1.8, "duration": "6m 00s",
                 "duration_seconds": 360} for i, stage in enumerate(["decompose", "specify", "judge"] + ["implement"] * 3 + ["verify"] * 8
                                                                    + ["review"] * 7 + ["merge_judge"] * 1 + ["catalog"] * 3)]
    return {"run_id": "run_budget000001", "outcome_title": "Refresh Evergreen frontier models", "reviewers": ["willtech3"],
            "sessions": sessions, "list_cost_usd": 52.02, "sessions_cost_usd": 50.84, "sessions_duration": "3h 58m 26s"}


def browser_evidence(ctx: Any, tmp_path: Path, the_spec: dict[str, Any], head: str) -> list[dict[str, Any]]:
    """Three passing browser checks through the controller's own protocol (request, report, collect, persist), each with
    two screenshots and a GIF demo, as the factory's browser runner hands them back from the hosted sandbox."""
    rows = []
    for entry in the_spec["test_plan"]["e2e"]:
        directory, request = visual.new_request(tmp_path / "verify", tmp_path, head, "http://localhost:3000", entry)
        (directory / "picker.png").write_bytes(PNG)
        (directory / "reply.png").write_bytes(PNG)
        (directory / "demo.gif").write_bytes(GIF)
        (directory / "result.json").write_text(json.dumps({
            "request_id": request["id"], "head_sha": head, "status": "passed", "runner": "factory browser runner (Playwright, Chromium)",
            "secret_seen": False,
            "summary": "Haiku 5.5 selected; the reply streamed and survived a reload.",
            "viewport_observed": entry["visual"]["viewport"],
            "steps": [{"status": "passed", "observation": f"observed: {s}"} for s in entry["visual"]["steps"]],
            "media": [{"file": "picker.png", "role": "screenshot", "caption": "The picker lists the new models"},
                      {"file": "reply.png", "role": "screenshot", "caption": "The streamed reply"},
                      {"file": "demo.gif", "role": "demo", "caption": "Picking a model and chatting"}]}))
        evidence = visual.collect(directory, request, 0)
        assert evidence["status"] == "passed", evidence["summary"]
        rows.append({"command": entry["command"], "target": "preview", "covers_ac": entry["covers_ac"], "exit_code": 0,
                     "location": "hosted-sandbox", "visual": evidence})
    ctx.event("verification.started", {"head_sha": head, "round": 3}, spec_id=SPEC_ID, work_item_id=WORK_ITEM)
    visual.persist(ctx, SPEC_ID, 3, head, rows)
    return rows


def test_results(e2e_rows: list[dict[str, Any]], head: str) -> dict[str, Any]:
    tests = {"framework": "bun", "report": "junit", "status": "reported", "passed": [f"tests/models.test.ts::t {i}" for i in range(26)],
             "failed": [], "skipped": []}
    return {"commands": [{"command": "bun test tests/models.test.ts", "exit_code": 0, "status": "passed", "location": "hosted-sandbox", "tests": tests},
                         {"command": "bun run lint", "exit_code": 0, "status": "passed", "location": "hosted-sandbox"},
                         {"command": "bunx tsc --noEmit", "exit_code": 0, "status": "passed", "location": "hosted-sandbox"}],
            "e2e": e2e_rows, "preview": {"status": "hosted_sandbox", "mode": "managed", "url": None, "head_sha": head},
            "environment": {"mode": "managed", "host": "anthropic-cloud", "status": "passed", "head_sha": head,
                            "session_id": "sesn_verify_r3", "profile_path": "test_environment.json", "profile_sha256": "31c1bb524ac5" * 4}}


def integrator(opened: list[str]):
    """An integrator session that sends the orchestrator's draft unchanged, as the prompt asks when the lint is clean."""
    from support.sessions import stage_result

    def run(**kw):
        facts = json.loads(kw["uploads"]["pr_facts.json"])
        assert json.loads(kw["uploads"]["pr_lint.json"]) == {"problems": []}, kw["uploads"]["pr_lint.json"]
        answer = kw["custom_tools"]["open_change_request"]({
            "source_branch": facts["source_branch"], "target_branch": facts["target_branch"], "title": facts["title"],
            "description_markdown": kw["uploads"]["pr_description.md"].decode(), "labels": facts["labels"],
            "reviewers": facts["reviewers"], "draft": facts["draft"]}, None)
        opened.append(answer)
        return stage_result({"cr.json": answer.encode()})
    return SimpleNamespace(run=run)
