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
    flag: str | None = None        # something the owner should see that is not a failure (an over-budget change)

    def to_dict(self) -> dict[str, Any]:
        return {"check": self.check, "passed": self.passed, "detail": self.detail, **({"flag": self.flag} if self.flag else {})}


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


GLOB_CHARS = set("*?[]{}!")


def named_test_paths(allowed_paths: Iterable[str]) -> list[str]:
    """The test files an approved spec names by exact path in allowed_paths: no glob character, not a directory. A glob
    such as tests/** names no file, so it never unlocks an existing test."""
    return sorted({p.lstrip("/") for p in allowed_paths if p and not (set(p) & GLOB_CHARS) and not p.endswith("/") and is_test_file(p)})


def protected_tests(
    modified_files: Iterable[str],
    deleted_files: Iterable[str],
    existing_test_files: Iterable[str],
    named_test_files: Iterable[str] = (),
    removed_lines_by_file: dict[str, list[str]] | None = None,
) -> CheckResult:
    """An approved spec may update existing tests it names by exact path.

    Globs never unlock existing tests. Deleted or renamed files and unreadable diffs remain protected.
    The separate test_weakening check and independent review still enforce assertion strength.
    """
    existing = set(existing_test_files)
    deleted = set(deleted_files)
    named = set(named_test_files)
    removed = removed_lines_by_file
    touched = [f for f in list(modified_files) + list(deleted) if f in existing and is_test_file(f)]

    def why_not(f: str) -> str | None:
        """Why a named test update cannot be reviewed safely."""
        if f in deleted:
            return "deleted or renamed"
        if removed is None or not isinstance(removed.get(f), list):
            return "the diff does not provide readable test changes"
        return None
    reasons = {f: why_not(f) for f in touched if f in named}
    approved = sorted(f for f, r in reasons.items() if r is None)
    blocked = sorted(set(touched) - set(approved))
    reasons = {f: r for f, r in reasons.items() if r is not None}
    if blocked:
        detail = "existing tests modified/deleted: " + str(blocked) + (
            "; named by the spec but not permitted: " + "; ".join(f"{f} ({r})" for f, r in sorted(reasons.items())) if reasons else "")
    else:
        detail = "no existing tests modified" + (
            f"; updated existing tests the approved spec names exactly: {approved}" if approved else "")
    return CheckResult(check="protected_tests", passed=not blocked, detail=detail,
                       data={"touched": blocked, "approved_updates": approved})


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
    if jsonschema is None:
        return CheckResult("schema_valid", True, "jsonschema not installed; skipped (sandbox validator ran)")
    errors = schema_errors(artifact, schema_path, schema_dir)
    msgs = [f"{'/'.join(str(x) for x in e.path) or '$'}: {e.message}" for e in errors[:20]]
    return CheckResult(
        check=f"schema_valid:{schema_path.name}", passed=not errors,
        detail="valid" if not errors else "; ".join(msgs), data={"errors": msgs},
    )


def schema_errors(artifact: dict[str, Any], schema_path: Path, schema_dir: Path) -> list[Any]:
    """The jsonschema ValidationErrors for `artifact`, ordered by path; [] when jsonschema is not installed."""
    if jsonschema is None:
        return []
    schema = json.loads(schema_path.read_text())
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
    return sorted(validator.iter_errors(artifact), key=lambda e: list(e.path))


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


def decomposition_size(decomposition: dict[str, Any], budget: dict[str, int]) -> CheckResult:
    """No work item is estimated above size M: hand-written lines, tests included, generated files excluded
    (size.py; post-mortem 2026-10-11 §3). An item above M is split before anyone specifies it."""
    from .size import size_for, whole
    over = [f"{w['work_item_id']} ({whole(w.get('estimated_lines'))} lines)" for w in decomposition.get("work_items", [])
            if whole(w.get("estimated_lines")) is not None and size_for(whole(w["estimated_lines"]), budget) == "L"]
    big = [w["work_item_id"] for w in decomposition.get("work_items", []) if w.get("size") == "L"]
    problems = ([f"estimated above size M (≤ {budget['M']} hand-written lines); split them: {over}"] if over else []) + \
               ([f"sized L; split them: {big}"] if big else [])
    return CheckResult("size_budget", not problems, "every work item is estimated at size M or below" if not problems
                       else "; ".join(problems), {"over": over, "large": big})


def spec_size_estimate(spec: dict[str, Any], budget: dict[str, int]) -> CheckResult:
    """The spec's own estimate is at size M or below, and its size label matches it."""
    from .size import estimate_of, size_for
    estimate = estimate_of(spec)
    if estimate is None:
        return CheckResult("size_budget", False, "no estimated_lines.hand_written; estimate the hand-written lines, tests included")
    size = size_for(estimate, budget)
    if size == "L":
        return CheckResult("size_budget", False, f"estimated {estimate} hand-written lines, above size M (≤ {budget['M']}); "
                           "split the work item (a regenerated data refresh lands as its own item first)", {"estimate": estimate})
    label = spec.get("estimated_size")
    if label and label != size:
        return CheckResult("size_budget", False, f"estimated_size {label} does not match estimated_lines ({estimate} lines is size {size})",
                           {"estimate": estimate})
    return CheckResult("size_budget", True, f"estimated {estimate} hand-written lines (size {size})", {"estimate": estimate})


def size_budget(measured: dict[str, Any] | None, estimate: int | None, budget: dict[str, int]) -> CheckResult:
    """Merge time: the change's hand-written and generated lines against the spec's estimate. Always passes: going
    over budget is a flag the owner sees in the run record and the pull request's size line, not a reason for another
    round (post-mortem 2026-10-11 §3)."""
    from .size import flag, size_for
    if measured is None:
        return CheckResult("size_budget", True, "size not measured (no diff statistics)")
    over = flag(measured, estimate, budget)
    detail = (f"{measured['hand_written']} hand-written lines (app {measured['app']}, tests {measured['tests']}; size "
              f"{size_for(measured['hand_written'], budget)}), {measured['generated']} generated lines in "
              f"{len(measured['generated_files'])} files; estimate {estimate if estimate is not None else 'none'}")
    return CheckResult("size_budget", True, detail, {"measured": measured, "estimate": estimate, "budget": dict(budget)}, flag=over)


EVIDENCE_PROTOCOL = re.compile(r"\bFACTORY_(?:VISUAL_REQUEST|EVIDENCE_DIR)\b|\bviewport_observed\b")


def criteria_describe_product(spec: dict[str, Any]) -> CheckResult:
    """No acceptance criterion is about the factory's own evidence protocol. A criterion is behaviour a user, an operator
    or a caller of the app observes; how the factory proves it (the browser evidence directory and request, the evidence
    file's fields) belongs to the verification and the run record. Narrow on purpose: it names only the protocol, so a
    product that happens to write a result.json is unaffected; the spec judge scores the rest (rubric S13). Post-mortem
    2026-10-11 §2."""
    hits = []
    for ac in spec.get("acceptance_criteria", []):
        text = " ".join(str(ac.get(k) or "") for k in ("given", "when", "then"))
        m = EVIDENCE_PROTOCOL.search(text)
        if m:
            hits.append(f"{ac.get('ac_id')} ({m.group(0)})")
    return CheckResult("criteria_describe_product", not hits,
                       "every criterion describes the product" if not hits else
                       f"criteria about the factory's evidence, not the product: {hits}; state the behaviour a user, operator or "
                       "caller observes and leave the proof to the e2e entry's visual contract", {"hits": hits})


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
    """ac_id -> test ids from the implementation report's `criteria: [{ac_id, tests: [...]}]` (the schema's only shape)."""
    out: dict[str, list[str]] = {}
    for entry in implementation_report.get("criteria") or []:
        if isinstance(entry, dict) and entry.get("ac_id") and isinstance(entry.get("tests"), list):
            out.setdefault(str(entry["ac_id"]), []).extend(t for t in entry["tests"] if isinstance(t, str) and t)
    return out


def ac_test_mapping(spec: dict[str, Any], implementation_report: dict[str, Any],
                    tests_that_passed: Iterable[str] | None, tests_that_exist: Iterable[str] | None = None,
                    require_verified: bool = False, e2e_results: list[dict[str, Any]] | None = None) -> CheckResult:
    """Every automated acceptance criterion names a test the orchestrator collected and saw pass.

    `tests_that_passed` are test ids from the orchestrator's own run (never the implementer's report): pytest node ids
    (`tests/test_x.py::Class::test_y`) or Bun ids (`src/x.test.ts::outer describe > inner describe > test name`), each
    read from a JUnit report the orchestrator's harness asked for. `tests_that_exist` is what was collected or reported
    in the checkout, so a mapped id nothing lists is reported as not found rather than as not passing. A mapped id may
    name a file, a pytest class or a Bun describe block: it counts when every listed test under it passed (a skipped,
    todo or filtered-out test is not a pass). `tests_that_passed=None` means the orchestrator has no machine-readable
    test evidence for this repository: the mapping is then checked for presence only and the detail says it is
    unverified, unless `require_verified` (managed preview mode), where missing evidence fails the check."""
    reported_tests = tests_that_passed is not None
    verified = reported_tests
    unverified_tests = False
    passed = set(tests_that_passed or [])
    exist = set(tests_that_exist) if tests_that_exist is not None else None
    mapping = criteria_mapping(implementation_report)
    problems = []
    if not isinstance(implementation_report.get("criteria"), list):
        problems.append("implementation_report.json has no `criteria: [{ac_id, tests}]` list (implementation_report.schema.json), "
                        "so no criterion maps to a test")
    for ac in spec["acceptance_criteria"]:
        if ac["verification"] != "automated_test":
            continue
        tests = mapping.get(ac["ac_id"], [])
        if not tests:
            problems.append(f"{ac['ac_id']}: no test mapped")
            continue
        # A browser journey can itself be the automated test. Its stable index refers to the approved
        # specification, and only the controller's matching result can prove it (never an agent claim).
        for test_id in [t for t in tests if t.startswith("e2e:")]:
            reason = _mapped_e2e_error(test_id, ac["ac_id"], spec, e2e_results or [])
            if reason:
                problems.append(f"{ac['ac_id']}: {test_id}: {reason}")
            else:
                verified = True
        tests = [t for t in tests if not t.startswith("e2e:")]
        if not tests:
            continue
        if not reported_tests:
            unverified_tests = True
            if require_verified:
                problems.append(f"{ac['ac_id']}: no machine-readable test report from the orchestrator's run covers {tests} "
                                "(only pytest and direct `bun test` commands in test_plan produce one)")
            continue
        unknown = [t for t in tests if exist is not None and not _test_known(t, exist)]
        if unknown:
            problems.append(f"{ac['ac_id']}: tests not found (no collection or test report in the orchestrator's run lists them): {unknown}")
        not_passed = [t for t in tests if t not in unknown and not _test_passed(t, passed, exist)]
        if not_passed:
            problems.append(f"{ac['ac_id']}: tests did not pass in the orchestrator's run: {not_passed}")
    verified = verified and not unverified_tests
    if problems:
        detail = "; ".join(problems)
    elif verified:
        detail = "all automated criteria map to tests the orchestrator collected and saw pass"
    else:
        detail = "all automated criteria map to a test; UNVERIFIED: the orchestrator ran no pytest in this repository"
    return CheckResult("ac_test_mapping", not problems, detail, {"problems": problems, "verified": verified})


def _mapped_e2e_error(test_id: str, ac_id: str, spec: dict[str, Any], results: list[dict[str, Any]]) -> str | None:
    entries = spec.get("test_plan", {}).get("e2e", [])
    if not re.fullmatch(r"e2e:(0|[1-9][0-9]*)", test_id) or int(test_id.split(":")[1]) >= len(entries):
        return "not an e2e entry in the specification"
    entry = entries[int(test_id.split(":")[1])]
    if ac_id not in entry.get("covers_ac", []):
        return "the specification does not assign this e2e entry to this criterion"
    matching = [r for r in results if r.get("command") == entry["command"] and r.get("target") == entry["target"]
                and set(r.get("covers_ac", [])) == set(entry.get("covers_ac", []))]
    if not matching or any(r.get("exit_code") != 0 for r in matching):
        return "no matching passing result from the orchestrator's e2e run"
    if entry.get("visual") and not visual_passed({"test_plan": {"e2e": [entry]}}, matching).passed:
        return "the browser entry lacks its matching current visual evidence"
    return None


JS_TEST_ID = re.compile(r"^[^\n]+?\.[cm]?[jt]sx?::")


def _under(test_id: str, candidate: str) -> bool:
    """`candidate` is a test inside `test_id`: below a file or pytest class (`::`), or, for a Bun id, inside a describe
    block (` > `)."""
    return candidate.startswith(test_id + "::") or (bool(JS_TEST_ID.match(test_id)) and candidate.startswith(test_id + " > "))


def _test_known(test_id: str, collected: set[str]) -> bool:
    return test_id in collected or any(_under(test_id, c) for c in collected)


def _test_passed(test_id: str, passed: set[str], collected: set[str] | None) -> bool:
    if test_id in passed:
        return True
    pool = collected if collected is not None else passed
    under = [c for c in pool if _under(test_id, c)]
    return bool(under) and all(c in passed for c in under)


PROOF_CODE = re.compile(r"\bFACTORY_(?:VISUAL_REQUEST|EVIDENCE_DIR)\b")


def factory_proof_in_diff(added_lines_by_file: dict[str, list[str]]) -> CheckResult:
    """The change adds no code that answers the factory's evidence protocol. Browser checks run through the factory's
    own runner on a journey that is a run artifact (`factory:journey <name>`); a UI change's pull request adds no
    browser-test files (post-mortem 2026-10-11 §1: a 71-line change shipped with a 1,860-line runner written for it).
    A repository's own end-to-end suite, which maintainers keep, is unaffected: only code that reads the factory's
    request or writes its evidence directory is refused."""
    hits = sorted({f for f, lines in added_lines_by_file.items() if any(PROOF_CODE.search(ln) for ln in lines)})
    return CheckResult("factory_proof_in_diff", not hits,
                       "no code answering the factory's evidence protocol" if not hits else
                       f"files answering the factory's evidence protocol (FACTORY_VISUAL_REQUEST / FACTORY_EVIDENCE_DIR): {hits}; "
                       "write the steps as a journey under /mnt/session/outputs/journeys/ and remove these files", {"files": hits})


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
                    and set(r.get("covers_ac", [])) == set(entry.get("covers_ac", []))
                    and all(r.get("visual", {}).get(k) == entry["visual"].get(k)
                            for k in ("persona", "viewport", "steps", "environment"))]
        if not matching or not all(r.get("exit_code") == 0 and r.get("visual", {}).get("status") == "passed" for r in matching):
            missing.append(entry.get("description") or entry["command"])
    return CheckResult("visual_e2e", not missing, "All required browser checks have current visual evidence" if not missing
                       else "Browser verification missing or blocked: " + "; ".join(missing))


def run_all_for_merge(
    *, spec: dict[str, Any], changed_files: list[str], deleted_files: list[str],
    existing_test_files: list[str], added_lines_by_file: dict[str, list[str]],
    implementation_report: dict[str, Any], passing_tests: list[str] | None, command_results: list[dict[str, Any]],
    named_test_files: Iterable[str] = (), removed_lines_by_file: dict[str, list[str]] | None = None,
    manifest_path: Path | None = None, schema_dir: Path | None = None,
    e2e_results: list[dict[str, Any]] | None = None, tests_that_exist: list[str] | None = None,
    require_test_evidence: bool = False, size: dict[str, Any] | None = None, size_budget_lines: dict[str, int] | None = None,
) -> list[CheckResult]:
    """The merge judge receives this list verbatim and may not override a failure.

    `passing_tests` and `tests_that_exist` come from the orchestrator's own pytest run and collection (see
    `ac_test_mapping`). `manifest_path`/`schema_dir` enable `manifest_valid`; `e2e_results` (from
    `LocalVerifier.run_e2e` against the preview URL) enables `e2e_preview`. When the spec has preview e2e entries
    and no results were supplied, `e2e_preview` fails: not running them is not the same as passing them.
    """
    out = [
        diff_scope(changed_files + deleted_files, spec["allowed_paths"]),
        protected_tests(changed_files, deleted_files, existing_test_files, named_test_files, removed_lines_by_file),
        test_weakening(added_lines_by_file),
        ac_test_mapping(spec, implementation_report, passing_tests, tests_that_exist,
                        require_verified=require_test_evidence, e2e_results=e2e_results),
        commands_passed(command_results),
        e2e_coverage(spec),
        visual_passed(spec, e2e_results or []),
        secret_scan(added_lines_by_file),
        factory_proof_in_diff(added_lines_by_file),
    ]
    if manifest_path is not None and schema_dir is not None:
        out.append(manifest_valid(manifest_path, schema_dir))
    if size_budget_lines is not None:
        from .size import estimate_of
        out.append(size_budget(size, estimate_of(spec), size_budget_lines))
    preview_entries = [e for e in spec.get("test_plan", {}).get("e2e", []) if e.get("target") == "preview"]
    if preview_entries:
        if e2e_results is None:
            e2e_results = [{"command": e["command"], "target": "preview", "exit_code": None} for e in preview_entries]
        out.append(e2e_passed(e2e_results, "preview"))
    return out
