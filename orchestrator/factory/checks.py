"""Deterministic checks. No agent may override a failing check.

These are the harness-side oracles the research argues for (SEAL, arXiv:2607.24300;
Destefanis and Aste, arXiv:2608.16801): cheap, exact, and outside any agent's control.
Each returns a `CheckResult` the merge judge receives verbatim.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    import jsonschema  # full validation when available (orchestrator side)
except ImportError:  # pragma: no cover
    jsonschema = None


@dataclass
class CheckResult:
    check: str
    passed: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"check": self.check, "passed": self.passed, "detail": self.detail}


# ---------------------------------------------------------------- globs

def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Convert a gitignore-style glob to a regex. Supports `**`, `*`, `?`."""
    pattern = pattern.strip().lstrip("/")
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def path_allowed(path: str, allowed: Iterable[str]) -> bool:
    p = path.lstrip("/")
    return any(_glob_to_regex(g).match(p) for g in allowed)


# ---------------------------------------------------------------- checks

def diff_scope(changed_files: Iterable[str], allowed_paths: Iterable[str]) -> CheckResult:
    allowed = list(allowed_paths)
    outside = sorted(f for f in changed_files if not path_allowed(f, allowed))
    return CheckResult(
        check="diff_scope", passed=not outside,
        detail="all changed files inside allowed_paths" if not outside else f"outside allowed_paths: {outside}",
        data={"outside": outside},
    )


TEST_FILE_RE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(_test|\.test|\.spec|_spec)\.[a-z]+$|^test_.*\.py$")


def is_test_file(path: str) -> bool:
    return bool(TEST_FILE_RE.search(path))


def protected_tests(
    modified_files: Iterable[str],
    deleted_files: Iterable[str],
    existing_test_files: Iterable[str],
    allowed_paths: Iterable[str],
    spec_touches_tests: bool,
) -> CheckResult:
    """Existing tests may only change if the spec is explicitly about them."""
    existing = set(existing_test_files)
    touched = [f for f in list(modified_files) + list(deleted_files) if f in existing and is_test_file(f)]
    if spec_touches_tests:
        touched = [f for f in touched if not path_allowed(f, allowed_paths)]
    return CheckResult(
        check="protected_tests", passed=not touched,
        detail="no existing tests modified" if not touched else f"existing tests modified/deleted: {sorted(touched)}",
        data={"touched": sorted(touched)},
    )


WEAKENING_PATTERNS = [
    r"@pytest\.mark\.(skip|xfail)", r"\bunittest\.skip", r"\bit\.skip\b", r"\bxit\b", r"\bxdescribe\b",
    r"\btest\.skip\b", r"\bdescribe\.skip\b", r"#\[ignore\]", r"\bt\.Skip\(", r"\bskip\s*[:(]",
]


def test_weakening(added_lines_by_file: dict[str, list[str]]) -> CheckResult:
    """Flags newly added skip/xfail markers anywhere in the diff."""
    hits = []
    for f, lines in added_lines_by_file.items():
        for ln in lines:
            if any(re.search(p, ln) for p in WEAKENING_PATTERNS):
                hits.append(f"{f}: {ln.strip()[:80]}")
    return CheckResult(
        check="test_weakening", passed=not hits,
        detail="no skip/xfail markers added" if not hits else f"added skip/xfail markers: {hits}",
        data={"hits": hits},
    )


def schema_valid(artifact: dict[str, Any], schema_path: Path, schema_dir: Path) -> CheckResult:
    schema = json.loads(schema_path.read_text())
    if jsonschema is None:
        return CheckResult("schema_valid", True, "jsonschema not installed; skipped (sandbox validator ran)")
    resolver_store = {}
    for p in schema_dir.glob("*.schema.json"):
        s = json.loads(p.read_text())
        resolver_store[s.get("$id", p.name)] = s
        resolver_store[p.name] = s
    try:
        from referencing import Registry, Resource  # jsonschema >= 4.18
        registry = Registry()
        for key, s in resolver_store.items():
            registry = registry.with_resource(key, Resource.from_contents(s))
        validator = jsonschema.Draft202012Validator(schema, registry=registry)
    except ImportError:  # pragma: no cover
        validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(artifact), key=lambda e: list(e.path))
    msgs = [f"{'/'.join(str(x) for x in e.path) or '$'}: {e.message}" for e in errors[:20]]
    return CheckResult(
        check=f"schema_valid:{schema_path.name}", passed=not errors,
        detail="valid" if not errors else "; ".join(msgs), data={"errors": msgs},
    )


def dag_acyclic(work_items: list[dict[str, Any]]) -> CheckResult:
    ids = {w["work_item_id"] for w in work_items}
    deps = {w["work_item_id"]: [d for d in w.get("depends_on", [])] for w in work_items}
    unknown = sorted({d for ds in deps.values() for d in ds if d not in ids})
    if unknown:
        return CheckResult("dag_acyclic", False, f"unknown dependency ids: {unknown}")
    state: dict[str, int] = {}

    def visit(n: str, stack: list[str]) -> list[str] | None:
        if state.get(n) == 1:
            return stack + [n]
        if state.get(n) == 2:
            return None
        state[n] = 1
        for d in deps[n]:
            cyc = visit(d, stack + [n])
            if cyc:
                return cyc
        state[n] = 2
        return None

    for n in deps:
        cyc = visit(n, [])
        if cyc:
            return CheckResult("dag_acyclic", False, f"cycle: {' -> '.join(cyc)}", {"cycle": cyc})
    return CheckResult("dag_acyclic", True, "no cycles")


def metric_coverage(outcome: dict[str, Any], decomposition: dict[str, Any]) -> CheckResult:
    metrics = {m["metric"] for m in outcome["success_metrics"]}
    covered = {c["metric"] for c in decomposition.get("coverage", []) if c.get("work_item_ids")}
    stated_uncoverable = {c["metric"] for c in decomposition.get("coverage", []) if not c.get("work_item_ids")}
    missing = sorted(metrics - covered - stated_uncoverable)
    return CheckResult(
        check="metric_coverage", passed=not missing,
        detail="every success metric mapped" if not missing else f"metrics with no work items and no rationale: {missing}",
        data={"missing": missing, "uncoverable": sorted(stated_uncoverable)},
    )


def spec_traceability(spec: dict[str, Any], decomposition: dict[str, Any]) -> CheckResult:
    """Spec points at a real work item; declared spec dependencies match the DAG."""
    items = {w["work_item_id"]: w for w in decomposition["work_items"]}
    wi = items.get(spec["work_item_id"])
    if wi is None:
        return CheckResult("spec_traceability", False, f"work_item {spec['work_item_id']} not in decomposition")
    problems = []
    if spec["decomposition_id"] != decomposition["decomposition_id"]:
        problems.append("decomposition_id mismatch")
    if spec["outcome_id"] != decomposition["outcome_id"]:
        problems.append("outcome_id mismatch")
    return CheckResult("spec_traceability", not problems, "traceable" if not problems else "; ".join(problems))


def criteria_mapping(implementation_report: dict[str, Any]) -> dict[str, list[str]]:
    """ac_id -> test ids from the implementation report.

    The kickoff asks for `criteria: [{ac_id, tests: [...]}]`. Implementers have also written `criteria_to_tests`
    (same list shape) and `acceptance_criteria` as a map `{ac_id: {pytest_node_ids|tests|test_ids: [...]}}` or a
    list of `{ac_id, ...}`; every one of those is read, because the check is about whether the tests exist and
    pass, not about which key the agent chose."""
    out: dict[str, list[str]] = {}

    def add(ac_id: Any, entry: Any) -> None:
        if not ac_id or not isinstance(entry, dict):
            return
        tests: Any = None
        for key in ("tests", "pytest_node_ids", "test_ids", "node_ids", "test"):
            if entry.get(key):
                tests = entry[key]
                break
        if isinstance(tests, str):
            tests = [tests]
        out.setdefault(str(ac_id), []).extend(t for t in (tests or []) if isinstance(t, str) and t)

    for key in ("criteria", "criteria_to_tests", "acceptance_criteria"):
        block = implementation_report.get(key)
        if isinstance(block, dict):
            for ac_id, entry in block.items():
                add(ac_id, entry)
        elif isinstance(block, list):
            for entry in block:
                if isinstance(entry, dict):
                    add(entry.get("ac_id"), entry)
    return out


def ac_test_mapping(spec: dict[str, Any], implementation_report: dict[str, Any],
                    tests_that_passed: Iterable[str] | None, tests_that_exist: Iterable[str] | None = None) -> CheckResult:
    """Every automated acceptance criterion names a test the orchestrator collected and saw pass.

    `tests_that_passed` are node ids from the orchestrator's own pytest run (never the implementer's report);
    `tests_that_exist` is what `pytest --collect-only` lists in the checkout, so a mapped id that pytest does not know
    is reported as not found rather than as not passing. A mapped id may name a file or a class: it counts when every
    collected test under it passed. `tests_that_passed=None` means the orchestrator could not run pytest in this
    repository; the mapping is then checked for presence only and the detail says it is unverified."""
    verified = tests_that_passed is not None
    passed = set(tests_that_passed or [])
    exist = set(tests_that_exist) if tests_that_exist is not None else None
    mapping = criteria_mapping(implementation_report)
    problems = []
    for ac in spec["acceptance_criteria"]:
        if ac["verification"] != "automated_test":
            continue
        tests = mapping.get(ac["ac_id"], [])
        if not tests:
            problems.append(f"{ac['ac_id']}: no test mapped")
            continue
        if not verified:
            continue
        unknown = [t for t in tests if exist is not None and not _test_known(t, exist)]
        if unknown:
            problems.append(f"{ac['ac_id']}: tests not found (pytest did not collect them): {unknown}")
        not_passed = [t for t in tests if t not in unknown and not _test_passed(t, passed, exist)]
        if not_passed:
            problems.append(f"{ac['ac_id']}: tests did not pass in the orchestrator's run: {not_passed}")
    if problems:
        detail = "; ".join(problems)
    elif verified:
        detail = "all automated criteria map to tests the orchestrator collected and saw pass"
    else:
        detail = "all automated criteria map to a test; UNVERIFIED: the orchestrator ran no pytest in this repository"
    return CheckResult("ac_test_mapping", not problems, detail, {"problems": problems, "verified": verified})


def _test_known(test_id: str, collected: set[str]) -> bool:
    return test_id in collected or any(c.startswith(test_id + "::") for c in collected)


def _test_passed(test_id: str, passed: set[str], collected: set[str] | None) -> bool:
    if test_id in passed:
        return True
    pool = collected if collected is not None else passed
    under = [c for c in pool if c.startswith(test_id + "::")]
    return bool(under) and all(c in passed for c in under)


def secret_scan(added_lines_by_file: dict[str, list[str]]) -> CheckResult:
    """No high-confidence credential in the added lines. A hit is blocking whatever a reviewer thinks."""
    from .security import secret_scan as scan
    hits = scan(added_lines_by_file)["hits"]
    detail = "no credential shapes in the added lines" if not hits else "; ".join(f"{h['file']} (+{h['added_line_index']}): {h['kind']}" for h in hits)
    return CheckResult("secret_scan", not hits, detail, {"hits": hits})


def commands_passed(results: list[dict[str, Any]]) -> CheckResult:
    failed = [r["command"] for r in results if r.get("exit_code", 1) != 0]
    return CheckResult("test_plan_commands", not failed,
                       "all test plan commands exit 0" if not failed else f"failed: {failed}", {"failed": failed})


def manifest_valid(manifest_path: Path, schema_dir: Path) -> CheckResult:
    """The service manifest at the platform contract's fixed path exists and validates.

    `service_manifest.schema.json` is a placeholder the platform team's schema replaces; the check
    is the same either way. Deployability is part of done (SWE-Cycle, arXiv:2605.13139: solve rates
    drop sharply once the cycle includes running the result).
    """
    if not manifest_path.exists():
        return CheckResult("manifest_valid", False, f"service manifest missing at {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        return CheckResult("manifest_valid", False, f"service manifest is not valid JSON: {e}")
    inner = schema_valid(manifest, schema_dir / "service_manifest.schema.json", schema_dir)
    return CheckResult("manifest_valid", inner.passed, inner.detail, inner.data)


def e2e_coverage(spec: dict[str, Any]) -> CheckResult:
    """Every `user_visible` acceptance criterion is covered by at least one test_plan.e2e entry.

    Agents mark features done without verifying them end to end unless the harness demands it
    (Anthropic, "Effective harnesses for long-running agents"). This is the demand.
    """
    entries = spec.get("test_plan", {}).get("e2e", [])
    covered = {ac for e in entries for ac in e.get("covers_ac", [])}
    known = {ac["ac_id"] for ac in spec["acceptance_criteria"]}
    missing = sorted(ac["ac_id"] for ac in spec["acceptance_criteria"] if ac.get("user_visible") and ac["ac_id"] not in covered)
    dangling = sorted(covered - known)
    problems = []
    if missing:
        problems.append(f"user_visible criteria with no e2e entry: {missing}")
    if dangling:
        problems.append(f"e2e entries cover unknown criteria: {dangling}")
    visual_covered = {ac for e in entries if e.get("visual") and e.get("target") == "preview" for ac in e.get("covers_ac", [])}
    has_ui = any(i.get("kind") == "ui" for i in spec.get("interfaces", []))
    missing_visual = sorted(ac["ac_id"] for ac in spec["acceptance_criteria"]
                            if (ac.get("surface") == "ui" or (has_ui and ac.get("user_visible") and not ac.get("surface")))
                            and ac["ac_id"] not in visual_covered)
    if missing_visual:
        problems.append(f"UI criteria without visual browser coverage: {missing_visual}")
    return CheckResult("e2e_coverage", not problems,
                       "every user_visible criterion has an e2e entry" if not problems else "; ".join(problems),
                       {"missing": missing, "dangling": dangling})


def e2e_passed(results: list[dict[str, Any]], target: str = "preview") -> CheckResult:
    """Every e2e entry for `target` exited 0 when the orchestrator ran it against the environment URL."""
    relevant = [r for r in results if r.get("target", target) == target]
    failed = [r["command"] for r in relevant if r.get("exit_code", 1) != 0 or (r.get("visual") and r["visual"].get("status") != "passed")]
    skipped = [r["command"] for r in relevant if r.get("exit_code") is None]
    detail = (f"all {len(relevant)} e2e entries exit 0 on {target}" if not failed and not skipped
              else f"failed on {target}: {failed}" + (f"; not run: {skipped}" if skipped else ""))
    return CheckResult(f"e2e_{target}", not failed and not skipped, detail, {"failed": failed, "skipped": skipped})


def visual_passed(spec: dict[str, Any], results: list[dict[str, Any]]) -> CheckResult:
    missing = []
    for entry in spec.get("test_plan", {}).get("e2e", []):
        if not entry.get("visual"):
            continue
        matching = [r for r in results if r.get("command") == entry["command"] and r.get("target") == entry["target"]
                    and set(r.get("covers_ac", [])) == set(entry.get("covers_ac", []))]
        if not matching or not all(r.get("exit_code") == 0 and r.get("visual", {}).get("status") == "passed" for r in matching):
            missing.append(entry.get("description") or entry["command"])
    return CheckResult("visual_e2e", not missing, "All required browser checks have current visual evidence" if not missing
                       else "Browser verification missing or blocked: " + "; ".join(missing))


def run_all_for_merge(
    *, spec: dict[str, Any], changed_files: list[str], deleted_files: list[str],
    existing_test_files: list[str], added_lines_by_file: dict[str, list[str]],
    implementation_report: dict[str, Any], passing_tests: list[str] | None, command_results: list[dict[str, Any]],
    spec_touches_tests: bool = False,
    manifest_path: Path | None = None, schema_dir: Path | None = None,
    e2e_results: list[dict[str, Any]] | None = None, tests_that_exist: list[str] | None = None,
) -> list[CheckResult]:
    """The merge judge receives this list verbatim and may not override a failure.

    `passing_tests` and `tests_that_exist` come from the orchestrator's own pytest run and collection (see
    `ac_test_mapping`). `manifest_path`/`schema_dir` enable `manifest_valid`; `e2e_results` (from
    `LocalVerifier.run_e2e` against the preview URL) enables `e2e_preview`. When the spec has preview e2e entries
    and no results were supplied, `e2e_preview` fails: not running them is not the same as passing them.
    """
    out = [
        diff_scope(changed_files + deleted_files, spec["allowed_paths"]),
        protected_tests(changed_files, deleted_files, existing_test_files, spec["allowed_paths"], spec_touches_tests),
        test_weakening(added_lines_by_file),
        ac_test_mapping(spec, implementation_report, passing_tests, tests_that_exist),
        commands_passed(command_results),
        e2e_coverage(spec),
        visual_passed(spec, e2e_results or []),
        secret_scan(added_lines_by_file),
    ]
    if manifest_path is not None and schema_dir is not None:
        out.append(manifest_valid(manifest_path, schema_dir))
    preview_entries = [e for e in spec.get("test_plan", {}).get("e2e", []) if e.get("target") == "preview"]
    if preview_entries:
        if e2e_results is None:
            e2e_results = [{"command": e["command"], "target": "preview", "exit_code": None} for e in preview_entries]
        out.append(e2e_passed(e2e_results, "preview"))
    return out
