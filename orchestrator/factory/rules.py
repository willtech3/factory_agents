"""The rule registry (ADR-0015): every rule the agents are told, the orchestrator checks and the judges apply, once.

A rule has an id the findings and checks cite, the text agents read, how it is enforced and which roles it binds:

- `check`: a deterministic check no judge can override (`checks.py`, `verification.judge`). `checks` names the
  CheckResults that enforce it; `fix` is what a failed check tells the implementer (`deterministic_findings`).
- `judge`: a judge's decision rule. Its post-condition is code here (`spec_pass_violations`, `merge_ready_violations`),
  so a verdict that breaks it is overridden, not trusted.
- `advice`: working practice the orchestrator cannot check; reviewers and judges weigh it.

What reads the registry: the rule block in each agent prompt and the deterministic part of the implementation rubric
(both rendered by `render_role` / `render_rubric` and kept in sync by `factory rules`, which a test runs), the findings for
failed checks, and the judges' post-conditions. A test fails when a check has no rule or a check rule names no check.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CHECK, JUDGE, ADVICE = "check", "judge", "advice"


@dataclass(frozen=True)
class Rule:
    id: str
    text: str
    enforced_by: str                          # check | judge | advice
    roles: frozenset[str]                     # agent keys whose prompt states it
    checks: tuple[str, ...] = ()              # CheckResult names that enforce it (enforced_by == check)
    fix: str = ""                             # what a failed check tells the implementer
    category: str = "spec_deviation"          # the finding category of a failed check (review_findings.schema.json)
    rubric: str | None = None                 # the implementation rubric item it is, when it is one
    hosted_fix: str = ""                      # `fix` when the head was verified in a hosted sandbox


BUILD = frozenset({"implementer", "reviewer", "merge_judge"})
DESIGN = frozenset({"spec_writer", "spec_judge"})

RULES: tuple[Rule, ...] = (
    # ------------------------------------------------------------------ the change (checked at every verification)
    Rule("allowed-paths", "Change only files that match the spec's `allowed_paths`; a file outside them fails the merge. "
         "If the change needs another path, the spec is wrong: stop and say so (`blocked.md`).", CHECK,
         BUILD | {"spec_writer"}, ("diff_scope",), "Revert the changes outside allowed_paths, or stop with blocked.md if the "
         "spec cannot be met without them.", rubric="I3"),
    Rule("protected-tests", "Existing tests are protected. Only an existing test file the approved spec names by its exact path "
         "in `allowed_paths` may be updated, to test the specified behaviour; globs do not authorize it. Never delete or "
         "rename an existing test file.", CHECK, BUILD | {"spec_writer"}, ("protected_tests",),
         "Existing tests are protected. An approved spec may name an existing test file by its exact path in allowed_paths; "
         "that file may then be updated to test the specified behavior. Globs do not authorize existing test edits. Never "
         "delete or rename an existing test file, skip a test, or weaken assertions to make the change pass.",
         category="test_quality", rubric="I4"),
    Rule("no-test-weakening", "Never skip a test or weaken an assertion to make the change pass.", CHECK, BUILD,
         ("test_weakening",), "Restore the assertion or the test; fix the code instead.", category="test_quality", rubric="I4"),
    Rule("criteria-mapped-to-tests", "Every automated acceptance criterion maps, in implementation_report.json `criteria`, to the "
         "tests that prove it, by the ids the orchestrator's own run reports: pytest node ids, or Bun ids "
         "`path/x.test.ts::outer describe > test name`. Only a test the orchestrator saw pass counts; an `e2e:<index>` "
         "entry counts when that entry passed with its evidence.", CHECK, BUILD, ("ac_test_mapping",),
         "Map every automated acceptance criterion to a test in implementation_report.json `criteria` ([{ac_id, tests: [...]}]) "
         "using pytest node ids exactly as `pytest --collect-only -q` prints them, or Bun ids `path/x.test.ts::outer describe > "
         "test name`; the orchestrator only counts tests its own run of the test plan collected or reported and saw pass "
         "(structured {runner, select} commands, and pytest runs inside other commands).", category="test_quality", rubric="I1"),
    Rule("test-plan-passes", "Every `test_plan.commands` entry (the tests, and the builds and linters the spec lists) exits 0 "
         "on the head the orchestrator verifies, from a clean checkout. A structured entry `{runner, select}` is run by the "
         "orchestrator with its own reporters.", CHECK, BUILD,
         ("test_plan_commands",), "Make every test_plan command exit 0 from a clean checkout; if a command depends on a port or "
         "host, make it robust or pick a free port.", category="test_quality", rubric="I2"),
    Rule("e2e-passes", "Every `test_plan.e2e` entry passes in the orchestrator's run of the head, `local` and `preview` entries "
         "alike: it ran, exited 0, and any browser evidence it owes passed. Not run is not passed.", CHECK, BUILD,
         ("e2e_local", "e2e_preview"), "Make the entry pass against the instance the orchestrator runs it on; the reproduction "
         "names the command and its output.", category="test_quality", rubric="I9"),
    Rule("visual-evidence", "A browser entry counts only with current screenshots at the requested viewport for every step of "
         "its visual contract, on the verified head.", CHECK, BUILD | DESIGN, ("visual_e2e",),
         "Make the browser check produce passing evidence at the requested viewport on the pushed head.", category="test_quality"),
    Rule("no-secrets", "No credential shapes in the added lines; credentials are references, never values.", CHECK,
         BUILD | {"security_reviewer"}, ("secret_scan",), "Remove the credential from the change and rotate it.", category="security"),
    Rule("no-factory-proof-code", "Never commit browser-test code for the factory or anything that reads FACTORY_VISUAL_REQUEST "
         "or FACTORY_EVIDENCE_DIR: journeys are run artifacts the orchestrator uploads and binds by hash.", CHECK, BUILD,
         ("factory_proof_in_diff",), "Remove the code that reads FACTORY_VISUAL_REQUEST or FACTORY_EVIDENCE_DIR; write the "
         "journey to /mnt/session/outputs/journeys/<name>.json instead."),
    Rule("manifest-valid", "The service manifest exists at the platform contract's path and validates.", CHECK, BUILD,
         ("manifest_valid",), "Fix the service manifest so it validates against service_manifest.schema.json."),
    Rule("size-budget", "Work stays within the run's size budget in hand-written changed lines: a work item or spec above M is "
         "split, and a change far above its approved estimate is flagged to the owner.", CHECK,
         BUILD | DESIGN | {"decomposer"}, ("size_budget",), "Bring the change back to the approved size, or stop with blocked.md "
         "saying the spec needs to split."),
    Rule("environment-builds", "The head installs into a fresh environment the orchestrator builds (the manifest's "
         "`runtime.setup`, else the pyproject), or in hosted verification the app's recipe prepares it.", CHECK, BUILD,
         ("verification_setup",), "Make the repository installable into a fresh virtual environment: a pyproject.toml whose "
         "dependencies resolve (runtime plus a dev/test extra for the test plan), or a `runtime.setup` command in the service "
         "manifest that installs what the tests and the entrypoint need.", category="test_quality",
         hosted_fix="The hosted verification could not run on this head; the detail says why. A failed recipe prepare phase is "
         "fixed in the change. A change to the recipe's trusted part (network, secrets, data, command_environment), a missing "
         "recipe, or test credentials that are not attached needs a person to re-approve the app's recipe: do not edit those "
         "to get past it."),
    Rule("app-runs", "In hosted verification the app starts, its services answer their health paths and the recipe's baseline "
         "check passes. The baseline shows the app runs, never that a criterion is met.", CHECK, BUILD, ("hosted_environment",),
         "The app did not start, its services did not answer their health paths, or the recipe's baseline check failed in the "
         "hosted sandbox (hosted_verification.json has the phases). Make the change keep the app starting and the baseline "
         "journey working; the recipe's commands and services may change with it, its trusted part may not.",
         category="test_quality"),
    Rule("report-names-head", "The implementation report describes the head the orchestrator verified; a branch that moved since "
         "needs an updated report.", CHECK, BUILD, ("implementation_head",), "Write implementation_report.json for the head you "
         "pushed last."),
    Rule("artifact-schema", "Every artifact validates against its schema in /mnt/session/uploads/schemas/ (check with "
         "`python -I /mnt/session/uploads/tools/validate.py <file>` before finishing). The orchestrator validates it again on "
         "receipt: an invalid report is a failed check, an invalid verdict or set of findings is refused.", CHECK,
         BUILD | DESIGN | {"decomposer", "security_reviewer"}, ("schema_valid", "report_schema"),
         "Make implementation_report.json and review_response.json validate against their schemas (validate.py names each "
         "error)."),
    # ------------------------------------------------------------------ the plan and the specs
    Rule("dag-acyclic", "Work item dependencies form a DAG.", CHECK, frozenset({"decomposer"}), ("dag_acyclic",)),
    Rule("metrics-covered", "Every success metric of the outcome is served by at least one work item, or marked not "
         "measurable by code with the reason.", CHECK, frozenset({"decomposer"}), ("metric_coverage",)),
    Rule("specs-trace-to-items", "Every spec traces to its work item and the success metrics that item serves.", CHECK, DESIGN,
         ("spec_traceability",)),
    Rule("criteria-describe-product", "Acceptance criteria describe the product's behaviour, not the factory's process (no "
         "criterion about reports, pull requests or the pipeline).", CHECK, DESIGN, ("criteria_describe_product",)),
    Rule("e2e-coverage", "Every user-visible acceptance criterion has a `test_plan.e2e` entry, and a UI criterion a visual "
         "browser entry with persona, viewport, steps and environment.", CHECK, DESIGN, ("e2e_coverage",),
         category="test_quality"),
    # ------------------------------------------------------------------ judges: post-conditions in code
    Rule("spec-pass", "A spec judge `pass` means no blocking issue and no rubric criterion scored 0; anything else is `revise` "
         "or `escalate`.", JUDGE, DESIGN),
    Rule("merge-ready", "`ready` means every deterministic check passed, every acceptance criterion is `met` with located "
         "evidence, and no blocking or major finding stands after the judge's re-rating.", JUDGE, BUILD),
    # ------------------------------------------------------------------ working practice
    Rule("no-history-rewrite", "Never rewrite history you have pushed (no rebase, no force-push); answer a remark about an "
         "earlier commit with a new commit, and bring in a moved base with a merge commit.", ADVICE, frozenset({"implementer"})),
    Rule("stop-rather-than-guess", "When the spec is ambiguous, two criteria conflict, or the work needs something the platform "
         "contract reserves, stop and say so (`blocked.md`, or `ask_human` where the role has it); never pick an "
         "interpretation silently.", ADVICE, frozenset({"implementer", "spec_writer", "decomposer"})),
)

BY_ID = {r.id: r for r in RULES}
BY_CHECK = {c: r for r in RULES for c in r.checks}


def rule_for_check(check: str) -> Rule | None:
    return BY_CHECK.get(check.split(":")[0])


def fix_for(check: str, *, hosted: bool = False) -> str:
    rule = rule_for_check(check)
    if rule is None:
        return "Fix the cause named in the detail."
    return (rule.hosted_fix if hosted and rule.hosted_fix else rule.fix) or "Fix the cause named in the detail."


def for_role(role: str) -> list[Rule]:
    return [r for r in RULES if role in r.roles]


# ---------------------------------------------------------------------- judges' decision rules, as code

def spec_pass_violations(verdict: dict[str, Any]) -> list[str]:
    """Why a spec judge's `pass` breaks `spec-pass` (empty when it holds or the verdict is not a pass)."""
    if (verdict.get("overall") or {}).get("result") != "pass":
        return []
    out = []
    for ps in verdict.get("per_spec") or []:
        out += [f"{ps.get('spec_id')}: rubric criterion {c.get('criterion')!r} scored 0"
                for c in ps.get("criteria") or [] if c.get("score") == 0]
        out += [f"{ps.get('spec_id')}: blocking issue {i.get('issue_id')}" for i in ps.get("issues") or [] if i.get("severity") == "blocking"]
    for bucket in (verdict.get("coherence") or {}).values():
        out += [f"coherence: blocking issue {i.get('issue_id')}" for i in bucket or [] if i.get("severity") == "blocking"]
    return out


def merge_ready_violations(verdict: dict[str, Any], spec: dict[str, Any], det: list[Any]) -> list[str]:
    """Why a merge judge's `ready` breaks `merge-ready` (empty when it holds or the verdict is not `ready`). The standing
    findings are the judge's own re-rating, which no code can redo; the checks and the criteria table are mechanical."""
    if verdict.get("result") != "ready":
        return []
    out = [f"deterministic check {d.check} failed" for d in det if not d.passed]
    rows = {c.get("ac_id"): c.get("status") for c in verdict.get("criteria") or [] if isinstance(c, dict)}
    for ac in spec.get("acceptance_criteria") or []:
        status = rows.get(ac["ac_id"])
        if status != "met":
            out.append(f"{ac['ac_id']} is {status or 'missing from the criteria table'}, not met")
    return out


# ---------------------------------------------------------------------- generated prompt and rubric sections

BEGIN = "<!-- rules:begin: generated from orchestrator/factory/rules.py by `factory rules`; edit the registry, not this block -->"
END = "<!-- rules:end -->"
HOW = {CHECK: "checked by the orchestrator", JUDGE: "the judge's decision rule, enforced in code", ADVICE: "practice"}


def render_role(role: str) -> str:
    lines = ["## Rules", "", "The rules the factory holds this role to. A check, a finding or a verdict cites a rule by its id.", ""]
    for r in for_role(role):
        how = HOW[r.enforced_by] + (f": `{'`, `'.join(r.checks)}`" if r.checks else "")
        lines.append(f"- **`{r.id}`** ({how}). {r.text}")
    return "\n".join(lines)


def render_rubric() -> str:
    """The deterministic items of the implementation rubric: the merge judge reads them; the in-session grader never
    gets them (they sit below the grader marker)."""
    lines = ["**Computed by the orchestrator.** Deterministic; handed to the merge judge with the results. Each item is 0 when "
             "its check failed and 2 when it passed; the judge cannot override a failure.", ""]
    for r in sorted((r for r in RULES if r.enforced_by == CHECK and r.rubric), key=lambda r: int(r.rubric[1:])):
        if r.rubric != "I1":                  # partly judged: the grader and the judge read it above the marker
            lines.append(f"## {r.rubric}. {r.id} **[deterministic: `{'`, `'.join(r.checks)}`]**")
            lines.append(r.text)
            lines.append("")
    return "\n".join(lines).rstrip()


AGENT_ROLES = ("decomposer", "spec_writer", "spec_judge", "implementer", "reviewer", "security_reviewer", "merge_judge")


def generated_files(root: Path) -> dict[Path, str]:
    """Every file with a generated rules block, and the block it must hold."""
    out = {root / "agents" / f"{role}.md": render_role(role) for role in AGENT_ROLES}
    out[root / "rubrics" / "implementation.md"] = render_rubric()
    return out


def splice(text: str, block: str) -> str:
    """`text` with its rules block replaced by `block`, or the block appended when it has none."""
    wrapped = f"{BEGIN}\n{block}\n{END}"
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
    if pattern.search(text):
        return pattern.sub(lambda _: wrapped, text, count=1)
    return text.rstrip("\n") + "\n\n" + wrapped + "\n"


def drift(root: Path) -> list[Path]:
    """The generated files whose block differs from the registry."""
    return [p for p, block in generated_files(root).items() if not p.is_file() or splice(p.read_text(), block) != p.read_text()]


def write(root: Path) -> list[Path]:
    """Re-render every block; returns the files that changed."""
    changed = []
    for p, block in generated_files(root).items():
        new = splice(p.read_text(), block)
        if new != p.read_text():
            p.write_text(new)
            changed.append(p)
    return changed


__all__ = ["ADVICE", "AGENT_ROLES", "CHECK", "JUDGE", "RULES", "Rule", "drift", "fix_for", "for_role", "merge_ready_violations",
           "render_role", "render_rubric", "rule_for_check", "spec_pass_violations", "write"]
