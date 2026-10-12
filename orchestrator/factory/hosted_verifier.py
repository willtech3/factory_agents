"""Preview mode "managed": verify a candidate head in an Anthropic-hosted sandbox built from the app's approved recipe.

The controller only reads git here (the checkout, the diff, `git archive` of the exact head). Everything that executes
the repository runs in one fresh hosted session per round: the recipe's prepare/start/seed and its baseline verify,
the spec's `test_plan.commands` (pytest and direct `bun test` outcomes summarised from JUnit reports the harness itself
asks for, inside the sandbox), `pytest --collect-only`,
and the spec's `test_plan.e2e` entries for targets "local" and "preview" inside that sandbox, each visual entry
answering a controller-issued browser request. Results come back bound to the head, the recipe fingerprint and the
source archive fingerprint; anything missing, unbound or unsupported is recorded as not run, so the deterministic
checks fail closed. The recipe's baseline check proves the app runs; it never stands in for a spec criterion.

The recipe is a trust boundary (network allowlist, which test secrets reach which hosts), so a change may not alter
it: the candidate's recipe must hash to the approved fingerprint (`FACTORY_TEST_PROFILE_SHA256`, else the recipe
committed at the base). Only the explicit test vaults (`FACTORY_TEST_VAULT_IDS`) are attached; never the forge's.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import test_environments as te
from . import visual
from .budget import reserve_budget
from .checks import SETUP_CHECK, CheckResult
from .sandbox_tools import browser_runner as br
from .sandbox_tools import environment_runner as er

ENVIRONMENT_CHECK = "hosted_environment"
VERIFY_STAGE = "verify"
NODE_ID_RE = er.NODE_ID_RE                 # one definition: the sandbox runner writes ids, this side checks them
TEST_ID_RE = {"pytest": NODE_ID_RE, "bun": er.BUN_ID_RE}       # one definition each, shared with the sandbox runner
UNSUPPORTED_COMMANDS = {"factory:browser": "the attached-browser worker (factory:browser) runs on a person's machine and cannot "
                                           "run in a hosted sandbox; use `factory:journey <name>` with a journey the implementer writes"}
# `factory:journey <name>`: the factory's own browser runner on the change's journey, a run artifact the implementer wrote
# to /mnt/session/outputs/journeys/<name>.json. It is uploaded and bound by sha256, never committed (post-mortem 2026-10-11 §1).
JOURNEY_RE = re.compile(r"^factory:journey\s+([A-Za-z0-9][A-Za-z0-9_.-]{0,80})$")


def journey_name(command: str) -> str | None:
    m = JOURNEY_RE.match(command.strip())
    return m.group(1) if m else None

DESCRIPTION = """Run `python3 -I /mnt/session/uploads/tools/environment_runner.py` once and report its exit status. It verifies
candidate {head} of {spec_id} (round {round}) from the uploaded source archive and recipe. All application, database
and browser execution stays inside this sandbox. Leave /mnt/session/outputs/ exactly as the harness writes it; do not
re-run commands, edit app or evidence files, or produce test evidence yourself. If checks failed, write only the bounded diagnosis.json requested by the system instructions."""


@dataclass
class HostedRun:
    """What one hosted verification gives the implement loop, in the shapes the deterministic checks consume."""
    setup: CheckResult
    environment: CheckResult
    commands: list[dict[str, Any]]
    e2e: list[dict[str, Any]]
    collected: list[str] | None
    preview: dict[str, Any]
    record: dict[str, Any]


def preview_record(branch: str, head_sha: str | None, hosted: dict[str, Any] | None = None) -> dict[str, Any]:
    """`preview.json` for managed mode: there is no URL any agent sandbox can reach, and it says so."""
    return {"branch": branch, "status": "hosted_sandbox", "mode": "managed", "url": None, "pipeline": None, "head_sha": head_sha,
            "reachable_from_agent_sandboxes": False,
            "note": ("No preview deployment exists in managed mode. The orchestrator verifies each pushed head in a separate "
                     "Anthropic-hosted sandbox built from the app's approved test recipe; only that run counts. No agent "
                     "sandbox can reach that app instance."),
            **({"hosted": hosted} if hosted else {})}


def _entries(spec: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    return list(enumerate(spec["test_plan"].get("e2e", [])))


class HostedVerifier:
    def __init__(self, ctx, workdir: Path):
        self.ctx, self.cfg = ctx, ctx.cfg
        self.workdir = workdir / "hosted"          # outside verify/visual: no local worker can see or claim these requests

    # ------------------------------------------------------------ recipe and request
    def _recipe(self, repo: Path, base_sha: str, head_sha: str) -> tuple[str, bytes, str, dict, str]:
        """(head, source archive, recipe path, recipe, approved fingerprint) or ValueError naming what is wrong."""
        head, source = te.snapshot(repo, head_sha)
        if head != head_sha:
            raise ValueError(f"snapshot resolved to {head}, not the verified head {head_sha}")
        manifest = te.read_snapshot_json(source, self.cfg.service_manifest_path)
        relative = te.recipe_relative(manifest)
        profile = te.read_snapshot_profile(source, relative, self.cfg.schemas_dir)
        approved = self.cfg.test_profile_sha256
        if not approved:
            _, base_source = te.snapshot(repo, base_sha)
            base_relative = te.recipe_relative(te.read_snapshot_json(base_source, self.cfg.service_manifest_path))
            try:
                approved = te.fingerprint(te.read_snapshot_profile(base_source, base_relative, self.cfg.schemas_dir))
            except ValueError as exc:
                raise ValueError(f"no approved recipe: none is configured (FACTORY_TEST_PROFILE_SHA256) and the base commit has no valid one ({exc})") from None
        if te.fingerprint(profile) != approved:
            raise ValueError(f"the candidate's recipe {relative} (sha256 {te.fingerprint(profile)[:12]}) differs from the approved recipe "
                             f"({approved[:12]}); a change may not alter the test recipe without a person re-approving it")
        missing = [s["name"] for s in profile["secrets"] if s["required"]]
        if missing and not self.cfg.test_vault_ids:
            raise ValueError(f"the recipe requires test credentials ({', '.join(missing)}) and no test vault is attached (FACTORY_TEST_VAULT_IDS)")
        return head, source, relative, profile, approved

    # ------------------------------------------------------------ the round
    def verify(self, repo: Path, base_sha: str, head_sha: str, spec: dict[str, Any], *, branch: str, round_no: int,
               work_item_id: str | None = None, journeys: dict[str, bytes] | None = None) -> HostedRun:
        ctx, sid = self.ctx, spec["spec_id"]
        record: dict[str, Any] = {"mode": "managed", "host": "anthropic-cloud", "app_id": self.cfg.app_id, "head_sha": head_sha,
                                  "spec_id": sid, "round": round_no, "status": "blocked", "phases": []}
        try:
            head, source, relative, profile, approved = self._recipe(repo, base_sha, head_sha)
        except (ValueError, OSError) as exc:
            record["detail"] = f"hosted verification not started: {exc}"
            return self._finish(spec, branch, record, None, {}, {})
        digest = te.fingerprint(profile)
        app = next((s for s in profile["services"] if s["id"] == "app"), None)
        base_url = er.app_base_url(app) or None          # the origin the harness gives the app's browser checks too
        requests: dict[int, tuple[Path, dict]] = {}
        blocked: dict[int, str] = {}
        sandbox_entries = []
        bound_journeys: dict[str, str] = {}
        journey_uploads: dict[str, bytes] = {}
        for index, entry in _entries(spec):
            if entry.get("target") not in ("local", "preview"):
                blocked[index] = "unsupported e2e target; expected local or preview inside the hosted sandbox"
                continue
            if entry["command"] in UNSUPPORTED_COMMANDS:
                blocked[index] = UNSUPPORTED_COMMANDS[entry["command"]]
                continue
            if base_url is None and (entry.get("visual") or entry["target"] == "preview"):
                blocked[index] = "the approved recipe declares no service with id \"app\" for e2e entries to run against"
                continue
            name = journey_name(entry["command"])
            journey_ref = None
            if entry["command"].startswith("factory:journey"):
                problem = _journey_problem(name, (journeys or {}).get(name or ""), entry)
                if problem:
                    blocked[index] = problem
                    continue
                content = journeys[name]
                journey_ref = {"name": name, "sha256": hashlib.sha256(content).hexdigest()}
                bound_journeys[name] = journey_ref["sha256"]
                journey_uploads[f"journeys/{name}.json"] = content
            visual_request = None
            if entry.get("visual"):
                directory, visual_request = visual.new_request(self.workdir, repo, head, base_url, entry)
                requests[index] = (directory, visual_request)
            sandbox_entries.append({"index": index, "command": entry["command"], "target": entry["target"], "visual_request": visual_request,
                                    **({"journey": journey_ref} if journey_ref else {})})
        cap = float(self.cfg.managed_verify_budget_usd)
        verify_model = getattr(self.cfg, "verify_model", None)
        record.update(profile_path=relative, profile_sha256=digest, approved_profile_sha256=approved,
                      source_sha256=hashlib.sha256(source).hexdigest(), vaults_attached=len(self.cfg.test_vault_ids), cap_usd=cap,
                      # which model drove the harness, so runs before and after a model change compare (design principle 20)
                      session_model=dict(verify_model) if verify_model else "implementer agent")
        reserve_budget(ctx, VERIFY_STAGE, cap_usd=cap)
        client = ctx.driver.client
        # An unchanged recipe reuses the environment the app was onboarded in; a changed one is labelled with the app.
        app = ({"app_id": self.cfg.app_id, "repository_url": self.cfg.repo_url if self.cfg.forge_repo else ""}
               if self.cfg.app_id else None)
        environment_id = te.ensure_environment(client, profile, self.cfg.runs_dir / "_test_environments", app=app)
        descriptor = {"head_sha": head, "profile_sha256": digest, "source_sha256": record["source_sha256"], "profile_path": relative,
                      "spec_sha256": te.fingerprint({k: v for k, v in spec.items() if not k.startswith("_")}),
                      "environment_id": environment_id, "provider": "anthropic-cloud", "journeys": bound_journeys,
                      "change": {"spec_id": sid, "round": round_no, "commands": list(spec["test_plan"]["commands"]),
                                 **pytest_collection(spec["test_plan"]["commands"]),
                                 "e2e": sandbox_entries, "command_timeout_seconds": 900}}
        record["spec_sha256"] = descriptor["spec_sha256"]
        record["journeys"] = [{"name": n, "sha256": s} for n, s in sorted(bound_journeys.items())]
        record["app_base_url"] = base_url
        record["environment_id"] = environment_id
        # late import: the stages package imports this module
        from .stages.common import run_session
        res = run_session(
            ctx, stage=VERIFY_STAGE, prefix=f"{sid}-r{round_no}-hosted-", spec_id=sid,
            agent_key="implementer", title=f"{ctx.run_id} verify {sid} r{round_no} (hosted)", run_id=ctx.run_id,
            uploads={"test-source.tar": source, "environment-request.json": json.dumps(descriptor).encode(),
                     "tools/environment_runner.py": (Path(__file__).parent / "sandbox_tools/environment_runner.py").read_bytes(),
                     **({"tools/browser_runner.py": (Path(__file__).parent / "sandbox_tools/browser_runner.py").read_bytes(),
                         **journey_uploads} if journey_uploads else {})},
            work_item_id=work_item_id, description=DESCRIPTION.format(head=head, spec_id=sid, round=round_no),
            rubric_md=None, max_iterations=1, budget_usd=cap, environment_id=environment_id,
            vault_ids=list(self.cfg.test_vault_ids), system_override=te.SYSTEM + te.CHANGE_DIAGNOSIS_SYSTEM,
            # The session runs the harness once and, on a failure, writes a bounded diagnosis: a mid-tier model at low
            # effort, not the implementer's Opus at xhigh (Config.verify_model).
            model_override=verify_model,
        )
        try:
            diagnosis = json.loads(res.outputs.get("diagnosis.json", b"{}"))
        except ValueError:
            diagnosis = {}
        if isinstance(diagnosis, dict) and isinstance(diagnosis.get("summary"), str):
            record["diagnosis"] = {"source": "sandbox-agent", "summary": diagnosis["summary"][:1500]}
        record.update(session_id=res.session_id, cost_usd=res.list_cost_usd, stop_reason=res.stop_reason)
        record.update(te.archive_session(client, res.session_id))
        report, bound = te.harness_report(res.outputs, descriptor, te.BINDING_KEYS + ("spec_sha256", "journeys"))
        results = ((report.get("change") or {}).get("results") or {}) if bound else {}
        if bound and report.get("missing_secrets"):
            record["missing_secrets"] = [str(x) for x in report["missing_secrets"]][:20]
        record.update(bound=bound, phases=[p for p in report.get("phases", []) if isinstance(p, dict)] if bound else [],
                      services=report.get("services", []) if bound else [], harness_status=report.get("status"), harness_reason=report.get("reason"))
        if bound:
            record["runtime"] = _runtime(report.get("runtime"))
        if not bound:
            record["detail"] = "the hosted result is missing or not bound to this head, recipe and source archive"
        return self._finish(spec, branch, record, results, requests, blocked, res.outputs)

    # ------------------------------------------------------------ results -> rows the checks read
    def _finish(self, spec: dict[str, Any], branch: str, record: dict[str, Any], results: dict[str, Any] | None,
                requests: dict[int, tuple[Path, dict]], blocked: dict[int, str], outputs: dict[str, bytes] | None = None) -> HostedRun:
        ctx, sid = self.ctx, spec["spec_id"]
        ran = results is not None and bool(results)
        why_not = record.get("detail") or (f"hosted harness stopped early: {record['harness_reason']}" if record.get("harness_reason")
                                            else "hosted verification did not report this row")
        by_index = {r.get("index"): r for r in (results or {}).get("commands", []) if isinstance(r, dict)}
        commands = []
        for i, text in enumerate(spec["test_plan"]["commands"]):
            r = by_index.get(i) if ran else None
            if r is None or r.get("command") != text or r.get("status") == "not_run":
                commands.append({"command": text, "exit_code": None, "status": "not_run", "location": "hosted-sandbox", "stdout_tail": "",
                                 "stderr_tail": f"not run: {(r or {}).get('reason') or why_not}"})
                continue
            row = {"command": text, "exit_code": _exit_code(r), "status": r.get("status"), "seconds": r.get("seconds"),
                   "location": "hosted-sandbox", "stdout_tail": "",
                   "stderr_tail": _summary(r) + " Raw output stays in the hosted sandbox.",
                   **({"diagnostic": r["diagnostic"]} if isinstance(r.get("diagnostic"), dict) else {})}
            evidence = _test_evidence(r.get("tests"))
            if evidence is not None:
                row["tests"] = evidence
                if evidence.get("status") != "reported":
                    row["stderr_tail"] += f" No {evidence['framework']} test evidence: {evidence.get('reason', evidence['status'])}."
            commands.append(row)
        collected = None
        c = (results or {}).get("collected_tests") if ran else None
        if isinstance(c, dict) and c.get("exit_code") in (0, 5) and isinstance(c.get("node_ids"), list):
            collected = [x for x in c["node_ids"] if isinstance(x, str) and NODE_ID_RE.match(x)]
        if isinstance(c, dict):              # what pytest collection did, so a Bun-only run never reads as a pytest failure
            record["collection"] = {"framework": "pytest", "status": str(c.get("status"))[:20],
                                    **({"exit_code": c["exit_code"]} if isinstance(c.get("exit_code"), int) else {}),
                                    **({"collected": len(collected)} if collected is not None else {}),
                                    **({"reason": str(c["reason"])[:300]} if c.get("reason") else {})}
        e2e_by_index = {r.get("index"): r for r in (results or {}).get("e2e", []) if isinstance(r, dict)}
        e2e = []
        for index, entry in _entries(spec):
            row: dict[str, Any] = {"command": entry["command"], "target": entry.get("target"), "covers_ac": list(entry.get("covers_ac", [])),
                                   "base_url": "sandbox-local", "location": "hosted-sandbox", "stdout_tail": ""}
            r = e2e_by_index.get(index) if ran else None
            reason = blocked.get(index) or (r or {}).get("reason") or why_not
            if r is None or r.get("command") != entry["command"] or r.get("target") != entry.get("target") or r.get("status") == "not_run" or index in blocked:
                row.update(exit_code=None, base_url=None, stderr_tail=f"not run: {reason}")
                if entry.get("visual"):
                    row["visual"] = {**entry["visual"], "description": entry.get("description"), "status": "blocked",
                                     "summary": f"Hosted browser check not run: {reason}", "summary_source": "controller", "media": []}
                    if index in requests:
                        (requests[index][0] / "closed").touch()
                e2e.append(row)
                continue
            row.update(exit_code=_exit_code(r), stderr_tail=_summary(r) + " Raw output stays in the hosted sandbox.")
            if index in requests:
                directory, request = requests[index]
                for name in r.get("files", []) if isinstance(r.get("files"), list) else []:
                    content = (outputs or {}).get(f"e2e-{index}-{name}")
                    if isinstance(name, str) and Path(name).name == name and content is not None and not (directory / name).exists():
                        (directory / name).write_bytes(content)
                evidence = visual.collect(directory, request, row["exit_code"])
                if evidence["status"] == "passed":
                    observed = _viewport_observed(directory)
                    if observed != request["viewport"]:
                        evidence.update(status="blocked", summary_source="controller", summary=f"Browser evidence does not show the requested viewport "
                                                                  f"{request['viewport']} (viewport_observed: {observed or 'missing'}).")
                evidence["location"] = "hosted-sandbox"
                (directory / "closed").touch()
                row["visual"] = evidence
            e2e.append(row)
        phases = {p.get("phase"): p.get("status") for p in record.get("phases", [])}
        prepared = phases.get("prepare") == "passed"
        setup_ok = bool(record.get("bound")) and prepared
        setup_detail = ("hosted sandbox prepared the candidate from the approved recipe "
                        f"{record.get('profile_path')} ({str(record.get('profile_sha256'))[:12]}) in session {record.get('session_id')}"
                        if setup_ok else record.get("detail") or
                        f"the hosted sandbox did not prepare the candidate: {record.get('harness_reason') or 'the recipe prepare phase did not pass'}")
        baseline = (results or {}).get("baseline") if ran else None
        baseline_ok = isinstance(baseline, dict) and baseline.get("status") == "passed"
        env_ok = setup_ok and bool((results or {}).get("app_ready")) and baseline_ok and phases.get("stop") == "passed"
        env_detail = ("the app started, its services answered their health paths, and the recipe's baseline check passed; this shows "
                      "the app runs, not that any acceptance criterion is met" if env_ok else
                      f"app not ready or baseline check not passed: {((baseline or {}).get('diagnostic') or {}).get('summary') or (baseline or {}).get('detail') or record.get('detail') or 'see the phases'}"
                      + ("; environment teardown failed" if setup_ok and phases.get("stop") not in (None, "passed") else ""))
        record.update(status=("passed" if env_ok and all(c["exit_code"] == 0 for c in commands) and all(
                          r["exit_code"] == 0 and (not r.get("visual") or r["visual"]["status"] == "passed") for r in e2e)
                          else "blocked" if not setup_ok else "failed"),
                      baseline=baseline, ready=setup_ok, detail=record.get("detail") or (setup_detail if setup_ok else None))
        setup = CheckResult(SETUP_CHECK, setup_ok, setup_detail, {k: record.get(k) for k in (
            "mode", "host", "app_id", "profile_path", "profile_sha256", "approved_profile_sha256", "session_id", "environment_id")})
        environment = CheckResult(ENVIRONMENT_CHECK, env_ok, env_detail, {"phases": record.get("phases", []), "baseline": baseline})
        hosted = {k: record.get(k) for k in ("status", "session_id", "profile_sha256", "environment_id", "app_id")}
        preview = preview_record(branch, record.get("head_sha"), hosted)
        ctx.store.put_derived_artifact(ctx.run_id, "implement", f"{sid}-r{record['round']}-hosted_verification.json",
                               json.dumps(record, indent=2, default=str).encode())
        ctx.event("verification.hosted", {"head_sha": record["head_sha"], "round": record["round"], "status": record["status"],
                                          "app_id": record.get("app_id"), "profile_sha256": record.get("profile_sha256"),
                                          "session_id": record.get("session_id"), "cost_usd": record.get("cost_usd"),
                                          "detail": (record.get("detail") or env_detail)[:500]}, spec_id=sid)
        return HostedRun(setup=setup, environment=environment, commands=commands, e2e=e2e, collected=collected, preview=preview,
                         record={**record, "ready": setup_ok, "environment_ready": env_ok})


def _journey_problem(name: str | None, content: bytes | None, entry: dict[str, Any]) -> str | None:
    """Why a `factory:journey` entry cannot run, or None. Checked before the session so a missing or malformed journey
    costs nothing and comes back to the implementer as a not-run row with the reason."""
    if name is None:
        return "a journey entry is `factory:journey <name>` with a name of letters, digits, dots, dashes or underscores"
    if not entry.get("visual"):
        return "a journey entry needs the spec's visual contract (persona, viewport, steps, environment)"
    if content is None:
        return f"the implementer wrote no journeys/{name}.json to /mnt/session/outputs/"
    try:
        problems = br.validate_journey(json.loads(content), entry["visual"].get("steps"))
    except ValueError as exc:
        problems = [f"not JSON: {exc}"]
    return f"journeys/{name}.json is not a runnable journey: " + "; ".join(problems[:6]) if problems else None


def pytest_collection(commands: list[str]) -> dict[str, Any]:
    """Whether the harness collects pytest node ids for this change. Skipped, with the reason recorded, only when every
    test_plan command is a Bun command and none mentions pytest: Bun has no verified collect-only mode, and its own JUnit
    reports are the inventory of what exists. Any other plan collects as before, so a pytest suite keeps its existence check."""
    bun = [c for c in commands if any(er.bun_command(c, Path("/dev/null")))]
    if commands and len(bun) == len(commands) and not any("pytest" in c for c in commands):
        return {"collect_tests": False, "collect_skip_reason": "every test_plan command is a Bun command; Bun's own JUnit "
                                                               "reports list the tests, so pytest is not collected"}
    return {"collect_tests": True}


def _runtime(value: Any) -> dict[str, Any] | None:
    """The harness's runtime facts (non-secret), kept to known keys: the app's base URL, the readiness probe host, and the
    compatibility defaults it applied (names, values, whether applied, why)."""
    if not isinstance(value, dict):
        return None
    compat = value.get("compat") if isinstance(value.get("compat"), dict) else {}
    return {"base_url": str(value.get("base_url") or "")[:200], "health_host": str(value.get("health_host") or "")[:64],
            "compat": {str(k)[:80]: {"value": str(v.get("value"))[:8], "applied": v.get("applied") is True, "reason": str(v.get("reason") or "")[:200]}
                       for k, v in list(compat.items())[:8] if isinstance(v, dict) and k == er.NEXT_SYSTEM_TLS}}


def _exit_code(r: dict[str, Any]) -> int | None:
    if r.get("status") == "blocked" and r.get("reason") == "timeout":
        return 124                                   # the local verifier's timeout code: ran, and failed by running out of time
    code = r.get("exit_code")
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _summary(r: dict[str, Any]) -> str:
    code = _exit_code(r)
    tests = r.get("tests")
    failed = (tests.get("failed") or [])[:10] if isinstance(tests, dict) else []
    text = "timed out in the hosted sandbox." if code == 124 else f"exit {code} in the hosted sandbox."
    return text + (f" Failed tests: {', '.join(map(str, failed))}." if failed else "")


def _test_evidence(value: Any) -> dict[str, Any] | None:
    """A command row's framework-neutral test evidence: `{framework, report, status: "reported", passed, failed,
    skipped}` from a JUnit report the harness asked for, with every id checked against its framework's id shape; or
    `{framework, status: missing|invalid|unsupported, reason}`, which proves nothing. None when the command ran no test
    framework the harness knows."""
    if isinstance(value, dict) and value.get("framework") in TEST_ID_RE:
        framework = value["framework"]
        lists_ok = all(isinstance(value.get(k), list) for k in ("passed", "failed", "skipped"))
        if value.get("status") == "reported" and lists_ok:
            shape = TEST_ID_RE[framework]
            out = {k: [x for x in value[k] if isinstance(x, str) and shape.match(x)] for k in ("passed", "failed", "skipped")}
            if sum(map(len, out.values())) != sum(len(value[k]) for k in out):
                return {"framework": framework, "status": "invalid", "reason": "the report holds ids that are not this framework's"}
            return {"framework": framework, "report": "junit", "status": "reported", **out}
        status = value.get("status") if value.get("status") in ("missing", "invalid", "unsupported") else "invalid"
        return {"framework": framework, "status": status, "reason": str(value.get("reason") or status)[:300]}
    return None


def _viewport_observed(directory: Path) -> dict[str, int] | None:
    try:
        seen = json.loads((directory / "result.json").read_text()).get("viewport_observed")
        return {"width": int(seen["width"]), "height": int(seen["height"])}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
