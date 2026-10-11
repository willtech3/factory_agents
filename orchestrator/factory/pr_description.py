"""The change request description: written for an engineer who knows the app and nothing about the factory.

The controller renders it from `templates/cr_template.md` and structured facts (the merge judge's `pr_summary`, the
orchestrator's own test results, the diff's size, published media, the run-record link), and lints whatever the
integrator finally sends before the forge sees it. Everything the description leaves out (the criteria table, review
findings, verification detail, every screenshot and recording, per-session cost) lives in the run record
(`run_record.py`), which the description links once. Post-mortem 2026-10-11 §4-§6: the description of
willtech3/evergreen_chat#120 carried factory ids, a 15-row criteria table, a cost table, the judge's first-person
text and eleven links that only resolved on the owner's machine, and buried the one product decision.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MAX_LINES = 60
MAX_SCREENSHOTS = 3
MAX_DEMOS = 1
MAX_DECISIONS = 6
MAX_COMMANDS = 6
SECTIONS = ("## What changed and why", "## Needs your decision", "## How it was tested", "## Size", "## Start reading at")

# Factory vocabulary a reviewer who knows only the app cannot use. Ids and costs are always refused; a jargon word is
# allowed when the spec's own title or goal uses it as a product word (a recipe app may say "recipe").
IDS = [(re.compile(p), why) for p, why in (
    (r"\bAC-\d+\b", "acceptance-criterion id"), (r"\bF-\d+\b", "review-finding id"), (r"\bDET-[\w-]*", "check id"),
    (r"\bsesn_\w+", "session id"), (r"\brun_[0-9a-f]{6,}\b", "run id"), (r"\bspec_[a-z0-9][\w-]*", "spec id"),
    (r"\bwi-[a-z0-9][\w-]*", "work-item id"), (r"\bmrv_\w+", "verdict id"),
    (r"\$\s?\d", "a cost"), (r"\b(?:list cost|USD)\b", "a cost"),
    (r"(?<![\w/+\-\"“'‘])I(?:'m|'ve|'d|'ll|’m|’ve|’d|’ll)?(?![\w/'’])", "first-person text"), (r"\bmyself\b", "first-person text"),
)]
IN_CODE = (0, 1, 2, 3, 4, 7)     # the ids still refused inside a code span: no app names a file or a flag like these
CODE_SPAN = re.compile(r"`[^`\n]*`")
TERMS = ["merge judge", "spec judge", "spec writer", "the judge", "orchestrator", "recipe", "journey", "round",
         "decomposer", "implementer", "integrator", "codebase researcher", "security reviewer", "adversarial reviewer",
         "merge_readiness", "deterministic check"]
LOCAL_HOST = re.compile(r"(?i)\b(?:127\.0\.0\.1|localhost|0\.0\.0\.0)\b|\[::1\]")
IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)[^)]*\)|<img\b[^>]*\bsrc=[\"']([^\"']+)")
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)[^)]*\)")
VIDEO = (".mp4", ".webm", ".mov")


# ---------------------------------------------------------------------------------------------------- rendering

def render_template(template: str, context: dict[str, Any]) -> str:
    """The mustache subset the template uses: {{name}}, {{#section}}..{{/section}} (a list repeats, a truthy value
    renders once), {{^section}}..{{/section}} (renders when empty or false) and {{! comments }}. A tag alone on its
    line takes the line with it. Values are inserted as they are: the output is Markdown, not HTML."""
    template = re.sub(r"(?m)^[ \t]*(\{\{[#^/!][^}]*\}\})[ \t]*\n", r"\1", template)
    return _render(template, [context]).strip() + "\n"


def _lookup(stack: list[Any], name: str) -> Any:
    if name == ".":
        return stack[-1]
    for frame in reversed(stack):
        if isinstance(frame, dict) and name in frame:
            return frame[name]
    return None


def _render(text: str, stack: list[Any]) -> str:
    out, pos = [], 0
    for m in re.finditer(r"\{\{([#^/!]?)\s*([^}]*?)\s*\}\}", text):
        if m.start() < pos:
            continue
        out.append(text[pos:m.start()])
        kind, name = m.group(1), m.group(2)
        pos = m.end()
        if kind == "!":
            continue
        if kind in "#^" and kind:
            close = re.search(r"\{\{/\s*" + re.escape(name) + r"\s*\}\}", text[pos:])
            if close is None:
                raise ValueError(f"unclosed section {name!r} in the change request template")
            inner, pos = text[pos:pos + close.start()], pos + close.end()
            value = _lookup(stack, name)
            if kind == "^":
                out.append("" if value else _render(inner, stack))
            elif isinstance(value, list):
                out.extend(_render(inner, stack + [item]) for item in value)
            elif value:
                out.append(_render(inner, stack + [value]))
            continue
        value = _lookup(stack, name)
        out.append("" if value is None else str(value))
    out.append(text[pos:])
    return "".join(out)


def render(facts: dict[str, Any], template_path: Path) -> str:
    return render_template(template_path.read_text(), facts)


# ---------------------------------------------------------------------------------------------------- facts

def run_record_url(base_url: str, run_id: str, spec_id: str) -> str:
    """The Console's run page, opened at this spec's run record."""
    return f"{base_url.rstrip('/')}/#/runs/{run_id}#record-{spec_id}"


def facts(*, spec: dict[str, Any], verdict: dict[str, Any], test_results: dict[str, Any] | None, preview_mode: str,
          head_sha: str, diff_stats: dict[str, Any] | None, record_url: str, media_markdown: str = "") -> dict[str, Any]:
    """Everything the template needs, already worded. The merge judge's `pr_summary` supplies the narrative (what
    changed, what the tests cover, the decisions, where to start reading); everything countable comes from the
    orchestrator's own records, never from an agent's prose."""
    summary = verdict.get("pr_summary") if isinstance(verdict.get("pr_summary"), dict) else {}
    what = str(summary.get("what_changed") or "").strip() or _first_sentences(spec.get("goal", ""), 2)
    decisions = [d for d in summary.get("decisions") or [] if isinstance(d, dict) and d.get("decision")]
    decisions.sort(key=lambda d: not d.get("affects_product"))      # product-affecting first, order kept otherwise
    shown = [{"text": str(d["decision"]).strip().rstrip("."), "location": str(d.get("location") or "").strip()}
             for d in decisions[:MAX_DECISIONS]]
    if len(decisions) > MAX_DECISIONS:
        shown.append({"text": f"{len(decisions) - MAX_DECISIONS} more in the run record", "location": ""})
    if not summary:
        shown = [{"text": "The review did not state the decisions for this change; read them in the run record", "location": ""}]
    read_first = [{"path": str(r["path"]), "why": str(r.get("why") or "").strip()}
                  for r in (summary.get("read_first") or []) if isinstance(r, dict) and r.get("path")][:3]
    if not read_first and diff_stats:
        read_first = [{"path": f["path"], "why": f"+{f['added']} / −{f['removed']}"} for f in _largest_files(diff_stats)]
    commands = [{"command": c} for c in spec.get("test_plan", {}).get("commands", [])[:MAX_COMMANDS]]
    return {
        "title": str(spec.get("title") or spec["spec_id"])[:120],
        "what_changed": what,
        "decisions": shown,
        "see_it": media_markdown.strip(),
        "tested": _tested(test_results or {}, ((test_results or {}).get("environment") or {}).get("mode") or preview_mode,
                          head_sha, str(summary.get("how_tested") or "").strip()),
        "commands": commands,
        "has_commands": bool(commands),
        "size_line": size_line(diff_stats),
        "read_first": read_first,
        "run_record_url": record_url,
    }


def size_line(diff_stats: dict[str, Any] | None) -> str:
    if not diff_stats:
        return "Not measured for this change; the run record has the diff."
    files = len(diff_stats.get("files", []))
    added, removed = diff_stats.get("added", 0), diff_stats.get("removed", 0)
    return f"{added + removed} changed lines in {files} file{'s' if files != 1 else ''} (+{added} / −{removed})."


def diff_stats(numstat: str) -> dict[str, Any]:
    """`git diff --numstat` output as totals and per-file rows; binary files count as zero lines."""
    rows = []
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, removed = (int(x) if x.isdigit() else 0 for x in parts[:2])
        rows.append({"path": parts[-1], "added": added, "removed": removed})
    return {"files": rows, "added": sum(r["added"] for r in rows), "removed": sum(r["removed"] for r in rows)}


def _largest_files(stats: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(stats.get("files", []), key=lambda f: -(f["added"] + f["removed"]))[:3]


def _first_sentences(text: str, n: int) -> str:
    parts = re.split(r"(?<=[.!?])\s+", str(text).strip())
    return " ".join(parts[:n]).strip()


def _tested(test_results: dict[str, Any], preview_mode: str, head_sha: str, note: str) -> str:
    where = {"managed": "in a fresh Linux sandbox built from the app's committed test environment",
             "local": "on a clean checkout, in a virtual environment built from the repository's declared dependencies",
             }.get(preview_mode, "in the platform's CI, with end-to-end checks against the branch's preview deployment")
    commands = [r for r in test_results.get("commands") or [] if isinstance(r, dict)]
    passed = sum(1 for r in commands if r.get("exit_code") == 0)
    tests = sum(len(_evidence(r).get("passed") or []) for r in commands)
    failed = sum(len(_evidence(r).get("failed") or []) for r in commands)
    text = f"Ran on commit `{head_sha[:10]}` {where}: {passed} of {len(commands)} test commands passed"
    if tests or failed:
        text += f" ({tests} tests passed, {failed} failed)"
    e2e = [r for r in test_results.get("e2e") or [] if isinstance(r, dict)]
    if e2e:
        ok = sum(1 for r in e2e if r.get("exit_code") == 0 and (not r.get("visual") or r["visual"].get("status") == "passed"))
        browser = [r for r in e2e if r.get("visual")]
        sizes = sorted({f"{v['width']}×{v['height']}" for r in browser for v in [r["visual"].get("viewport") or {}]
                        if v.get("width") and v.get("height")})
        text += f"; {ok} of {len(e2e)} end-to-end checks passed"
        if browser:
            text += (", all in a real browser" if len(browser) == len(e2e) else f", {len(browser)} of them in a real browser")
            text += f" at {_and(sizes)}" if sizes else ""
    text += "."
    return f"{text} {note}".strip() if note else text


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _evidence(row: dict[str, Any]) -> dict[str, Any]:
    tests = row.get("tests") if isinstance(row.get("tests"), dict) else row.get("pytest")
    return tests if isinstance(tests, dict) else {}


# ---------------------------------------------------------------------------------------------------- lint

def lint(description: str, *, record_url: str, allowed_words: Iterable[str] = (), reserved_lines: int = 0) -> list[str]:
    """What keeps `description` from opening, one plain sentence each; [] when it may open. `reserved_lines` are the
    controller's own lines above it (an evaluation banner), which count against the limit but are not linted."""
    problems: list[str] = []
    lines = description.rstrip("\n").splitlines()
    if len(lines) + reserved_lines > MAX_LINES:
        problems.append(f"{len(lines) + reserved_lines} lines; the description must fit in {MAX_LINES}")
    if description.count(record_url) != 1:
        problems.append("link the run record exactly once, with the URL from pr_facts.json")
    for heading in SECTIONS:
        if not re.search(rf"(?m)^{re.escape(heading)}\s*$", description):
            problems.append(f"missing the section heading `{heading}`")
    if re.search(r"(?m)^\s*\|?\s*:?-{3,}:?\s*\|", description):
        problems.append("no tables; the criteria and the review findings live in the run record")
    without_record = description.replace(record_url, "")
    console = record_url.split("/#/")[0]
    if console in without_record or LOCAL_HOST.search(CODE_SPAN.sub("", without_record)):     # a code span is not a link
        problems.append("links to the owner's machine (127.0.0.1, localhost) or the factory's Console other than the "
                        "run-record link")
    images = [(m.group(1) or m.group(2)) for m in IMAGE.finditer(description)]
    gifs = [u for u in images if _path_of(u).endswith(".gif")]
    videos = [m.group(1) for m in LINK.finditer(description) if _path_of(m.group(1)).endswith(VIDEO)]
    if len(images) - len(gifs) > MAX_SCREENSHOTS:
        problems.append(f"{len(images) - len(gifs)} screenshots; at most {MAX_SCREENSHOTS}, the rest are in the run record")
    if len(gifs) + len(videos) > MAX_DEMOS:
        problems.append(f"{len(gifs) + len(videos)} demos; at most {MAX_DEMOS}")
    prose = _prose(without_record)
    spans, outside = " ".join(CODE_SPAN.findall(prose)), CODE_SPAN.sub("", prose)
    for i, (pattern, why) in enumerate(IDS):
        # a path or a command is the app's own name (`spec/spec_helper.rb`, `python -I`): inside one only a factory id
        # that cannot be an app's own name is refused
        hit = pattern.search(outside) or (pattern.search(spans) if i in IN_CODE else None)
        if hit:
            problems.append(f"contains {why} ({hit.group(0)!r}); describe it in the app's own terms")
    allowed = " ".join(allowed_words).lower()
    words = outside                                 # a path or a command is the app's own name for a thing
    for term in TERMS:
        word = rf"\b{re.escape(term)}s?\b" + (r"(?![- ]trip)" if term == "round" else "")
        hit = re.search(word, words, flags=re.IGNORECASE)
        if hit and not re.search(rf"\b{re.escape(term)}", allowed):
            problems.append(f"uses factory vocabulary ({hit.group(0)!r}); a reviewer who knows only the app cannot use it")
    return problems


def title_problems(title: str) -> list[str]:
    return [f"the title contains {why} ({m.group(0)!r})" for pattern, why in IDS[:8] for m in [pattern.search(title)] if m]


def _path_of(url: str) -> str:
    return url.split("?")[0].split("#")[0].lower()


def _prose(text: str) -> str:
    """The text a reader reads: link targets and bare URLs removed (a media URL may carry ids in its path), code spans
    kept (a command or a path is still read)."""
    text = re.sub(r"(!?\[[^\]]*\])\([^)]*\)", r"\1", text)
    text = re.sub(r"<img\b[^>]*>", "", text)
    return re.sub(r"https?://\S+", "", text)


__all__ = ["MAX_LINES", "diff_stats", "facts", "lint", "render", "render_template", "run_record_url", "size_line", "title_problems"]
