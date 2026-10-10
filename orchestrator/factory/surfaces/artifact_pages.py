"""Artifact pages: one self-contained HTML page per gate, for reading.

Gate 1 renders the decomposition (judge/agent summary first, metric coverage matrix, work-item table,
open questions, the markdown body). Gate 2 renders the spec judge's verdict (summary first, goal-coverage
matrix, per-spec sections with acceptance criteria, interfaces, allowed paths, test plan, judge issues,
open questions). Every page lists the artifact names and sha256 values the gate binds to (ADR-0005), so
what the human reads and what they approve are visibly the same thing.

Pages are *reading* surfaces; the decision is recorded on the gate surface (tracker, forge, or CLI).
`PagePublisher` decides where a page lives: `LocalHtmlPublisher` writes `runs/<run_id>/pages/<name>.html`;
`ClaudeArtifactPublisher` is a documented stub (claude.ai artifacts have no public publish API today).
"""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config


class PagePublisher(Protocol):
    def publish(self, run_id: str, name: str, html_text: str) -> str:
        """Store the page and return a URL a human can open."""
        ...


class LocalHtmlPublisher:
    def __init__(self, runs_dir: Path):
        self.runs_dir = runs_dir

    def publish(self, run_id: str, name: str, html_text: str) -> str:
        p = self.runs_dir / run_id / "pages" / f"{name}.html"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(html_text)
        return p.resolve().as_uri()


class ClaudeArtifactPublisher:
    """Publish a gate page as a claude.ai artifact (a private page with a shareable link).

    Stub by design: claude.ai artifacts are published from a Claude session (the Artifact tool in Claude
    Code or the desktop app), not through a public HTTP API the orchestrator could call. The intended
    flow is `LocalHtmlPublisher` first, then a human or a Claude session publishes `runs/<run_id>/pages/*.html`
    and pastes the artifact URL back with `factory pages link <run_id> <name> <url>` (future CLI). Until
    then this publisher writes the page locally and returns that URL, so configuring it is harmless.
    """

    def __init__(self, runs_dir: Path):
        self.local = LocalHtmlPublisher(runs_dir)

    def publish(self, run_id: str, name: str, html_text: str) -> str:
        return self.local.publish(run_id, name, html_text)


def make_publisher(cfg: "Config") -> PagePublisher:
    if cfg.page_publisher == "artifact":
        return ClaudeArtifactPublisher(cfg.runs_dir)
    return LocalHtmlPublisher(cfg.runs_dir)


# ------------------------------------------------------------------------------------------ rendering

_CSS = """
:root{--fg:#1b1b1b;--bg:#fff;--muted:#5f6368;--line:#e3e3e3;--ok:#1a7f37;--warn:#9a6700;--bad:#b42318;--soft:#f6f7f9}
@media(prefers-color-scheme:dark){:root{--fg:#e8e8e8;--bg:#121212;--muted:#9aa0a6;--line:#2c2c2c;--soft:#1c1c1c}}
body{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color:var(--fg);background:var(--bg);margin:0;padding:24px 16px;max-width:1040px;margin-inline:auto}
h1{font-size:1.6rem;margin:.2em 0}h2{font-size:1.2rem;margin-top:2em;border-bottom:1px solid var(--line);padding-bottom:.2em}h3{font-size:1.05rem;margin-top:1.4em}
table{border-collapse:collapse;width:100%;margin:.6em 0;font-size:.92em}th,td{border:1px solid var(--line);padding:6px 8px;vertical-align:top;text-align:left}th{background:var(--soft)}
code{font:.88em ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--soft);padding:1px 4px;border-radius:3px}pre{background:var(--soft);padding:10px;overflow:auto;border-radius:6px}
.summary{background:var(--soft);border-left:4px solid var(--ok);padding:12px 16px;border-radius:6px;white-space:pre-wrap}.summary.warn{border-color:var(--warn)}
.muted{color:var(--muted)}.pill{display:inline-block;padding:0 8px;border-radius:999px;font-size:.8em;border:1px solid var(--line)}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}nav a{margin-right:12px}.sha{font:.8em ui-monospace,monospace;word-break:break-all}
details{margin:.4em 0}summary{cursor:pointer}
"""


def _e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def md_to_html(md: str) -> str:
    """Small markdown subset (headings, lists, fenced code, paragraphs, bold, inline code, links). Enough for
    decomposition.md and spec.md; anything else degrades to escaped text."""
    out: list[str] = []
    lines = md.splitlines()
    i, in_list, in_code = 0, False, False
    para: list[str] = []

    def flush() -> None:
        nonlocal para
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
            para = []

    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            flush()
            if in_list:
                out.append("</ul>"); in_list = False
            if in_code:
                out.append("</pre>"); in_code = False
            else:
                out.append("<pre>"); in_code = True
            i += 1
            continue
        if in_code:
            out.append(_e(ln)); i += 1; continue
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            flush()
            if in_list:
                out.append("</ul>"); in_list = False
            lvl = min(len(m.group(1)) + 1, 6)
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
        elif re.match(r"^\s*[-*]\s+", ln):
            flush()
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{_inline(re.sub(r'^\s*[-*]\s+', '', ln))}</li>")
        elif ln.startswith("|") and ln.endswith("|"):
            flush()
            if in_list:
                out.append("</ul>"); in_list = False
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                if not re.match(r"^\|[\s:|-]+\|$", lines[i]):
                    rows.append([c.strip() for c in lines[i].strip("|").split("|")])
                i += 1
            if rows:
                head = "".join(f"<th>{_inline(c)}</th>" for c in rows[0])
                body = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows[1:])
                out.append(f"<table><tr>{head}</tr>{body}</table>")
            continue
        elif not ln.strip():
            flush()
            if in_list:
                out.append("</ul>"); in_list = False
        else:
            para.append(ln.strip())
        i += 1
    flush()
    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("</pre>")
    return "\n".join(out)


def _inline(t: str) -> str:
    t = _e(t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
    return t


def _page(title: str, subtitle: str, nav: list[tuple[str, str]], body: str) -> str:
    links = "".join(f'<a href="#{_e(a)}">{_e(t)}</a>' for a, t in nav)
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
            f"content=\"width=device-width,initial-scale=1\"><title>{_e(title)}</title><style>{_CSS}</style></head>"
            f"<body><h1>{_e(title)}</h1><p class=\"muted\">{_e(subtitle)}</p><nav>{links}</nav>{body}</body></html>")


def _artifacts_section(shas: dict[str, str] | None) -> str:
    if not shas:
        return ""
    rows = "".join(f"<tr><td><code>{_e(n)}</code></td><td class=\"sha\">{_e(s)}</td></tr>" for n, s in sorted(shas.items()))
    return (f"<h2 id=\"artifacts\">Artifacts this gate binds to</h2><p class=\"muted\">A decision is accepted only "
            f"if it names exactly these sha256 values (ADR-0005).</p><table><tr><th>Artifact</th><th>sha256</th></tr>{rows}</table>")


def _questions_section(questions: list[dict[str, Any]], heading: str = "Open questions") -> str:
    if not questions:
        return f"<h2 id=\"questions\">{_e(heading)}</h2><p class=\"muted\">None.</p>"
    items = "".join(
        f"<li><strong>{_e(q.get('question'))}</strong>"
        f"{' <span class=\"pill bad\">blocking</span>' if q.get('blocking') else ' <span class=\"pill\">non-blocking</span>'}"
        f"<br><span class=\"muted\">Default if unanswered: {_e(q.get('default_if_unanswered') or '—')}</span></li>"
        for q in questions)
    return f"<h2 id=\"questions\">{_e(heading)}</h2><ul>{items}</ul>"


def render_gate1_page(run_id: str, outcome: dict[str, Any], decomposition: dict[str, Any], decomposition_md: str = "",
                      shas: dict[str, str] | None = None, checks: list[dict[str, Any]] | None = None) -> str:
    """Gate 1: what you are approving, metric coverage, the work-item table, open questions, the full markdown."""
    metrics = [m.get("name") or m.get("metric") for m in outcome.get("success_metrics", [])]
    cov = {c.get("metric"): c for c in decomposition.get("coverage", [])}
    items = decomposition.get("work_items", [])
    cov_rows = ""
    for m in metrics or list(cov):
        c = cov.get(m) or {}
        wids = c.get("work_item_ids", [])
        cls = "ok" if wids else "bad"
        cov_rows += (f"<tr><td>{_e(m)}</td><td class=\"{cls}\">{', '.join(f'<code>{_e(w)}</code>' for w in wids) or 'not covered'}</td>"
                     f"<td>{_e(c.get('rationale', ''))}</td></tr>")
    item_rows = "".join(
        f"<tr><td><code>{_e(w['work_item_id'])}</code></td><td>{_e(w.get('title'))}</td><td>{_e(w.get('kind'))}</td>"
        f"<td>{_e(w.get('size'))}</td><td>{', '.join(f'<code>{_e(d)}</code>' for d in w.get('depends_on', [])) or '—'}</td>"
        f"<td>{', '.join(_e(m) for m in w.get('contributes_to_metrics', []))}</td>"
        f"<td>{'yes' if w.get('human_judgement_required') else ''}</td></tr>" for w in items)
    out_scope = "".join(f"<li>{_e(x)}</li>" for x in decomposition.get("explicitly_out_of_scope", []))
    assumptions = "".join(f"<li>{_e(x)}</li>" for x in decomposition.get("assumptions", []))
    risks = "".join(f"<li>{_e(r if isinstance(r, str) else r.get('risk') or r.get('description') or r)}</li>"
                    for r in decomposition.get("risks", []))
    checks_html = ""
    if checks:
        checks_html = "<h2 id=\"checks\">Deterministic checks</h2><ul>" + "".join(
            f"<li class=\"{'ok' if c.get('passed') else 'bad'}\"><code>{_e(c.get('check'))}</code>: {_e(c.get('detail'))}</li>"
            for c in checks) + "</ul>"
    body = (f"<h2 id=\"summary\">What you are approving</h2><div class=\"summary\">{_e(decomposition.get('summary'))}</div>"
            f"<p class=\"muted\">Revision {_e(decomposition.get('revision'))} · {len(items)} work items · "
            f"outcome <code>{_e(outcome.get('outcome_id'))}</code></p>"
            f"<h2 id=\"coverage\">Metric coverage</h2><table><tr><th>Success metric</th><th>Covered by</th><th>Rationale</th></tr>{cov_rows}</table>"
            f"<h2 id=\"work-items\">Work items</h2><table><tr><th>id</th><th>title</th><th>kind</th><th>size</th><th>depends on</th>"
            f"<th>metrics</th><th>human judgement</th></tr>{item_rows}</table>"
            f"{_questions_section(decomposition.get('open_questions', []))}"
            f"<h2 id=\"scope\">Explicitly out of scope</h2><ul>{out_scope or '<li class=muted>None listed.</li>'}</ul>"
            f"<h2 id=\"assumptions\">Assumptions and risks</h2><ul>{assumptions}{risks}</ul>{checks_html}"
            f"{_artifacts_section(shas)}"
            + (f"<h2 id=\"full\">decomposition.md</h2>{md_to_html(decomposition_md)}" if decomposition_md else ""))
    nav = [("summary", "Summary"), ("coverage", "Coverage"), ("work-items", "Work items"), ("questions", "Questions"),
           ("artifacts", "Artifacts")] + ([("full", "Full text")] if decomposition_md else [])
    return _page(f"Gate 1 · {outcome.get('title', run_id)}", f"Decomposition review · run {run_id}", nav, body)


def render_gate2_page(run_id: str, outcome: dict[str, Any], specs: dict[str, dict[str, Any]], verdict: dict[str, Any],
                      shas: dict[str, str] | None = None, spec_md: dict[str, str] | None = None) -> str:
    """Gate 2: judge summary first, goal-coverage matrix, per-spec sections, open questions, artifact hashes."""
    overall = verdict.get("overall", {})
    result = overall.get("result", "")
    cls = "summary" if result == "pass" else "summary warn"
    gc_rows = "".join(
        f"<tr><td>{_e(g.get('metric'))}</td><td class=\"{'ok' if g.get('assessment') == 'covered' else 'warn'}\">{_e(g.get('assessment'))}</td>"
        f"<td>{', '.join(f'<code>{_e(a)}</code>' for a in g.get('covering_acs', []))}</td><td>{_e(g.get('note', ''))}</td></tr>"
        for g in verdict.get("goal_coverage", []))
    issues_by_spec = {ps.get("spec_id"): ps for ps in verdict.get("per_spec", [])}
    coherence = verdict.get("coherence", {})
    coh_html = ""
    for bucket, items in coherence.items():
        if items:
            coh_html += f"<h3>{_e(bucket.replace('_', ' '))}</h3><ul>" + "".join(
                f"<li>{_e(it.get('description') or it)} <span class=\"muted\">({', '.join(it.get('related_spec_ids', []))})</span></li>"
                if isinstance(it, dict) else f"<li>{_e(it)}</li>" for it in items) + "</ul>"
    spec_sections = ""
    nav = [("summary", "Summary"), ("coverage", "Goal coverage")]
    all_questions: list[dict[str, Any]] = []
    for sid, s in specs.items():
        anchor = f"spec-{sid}"
        nav.append((anchor, sid))
        ac_rows = "".join(
            f"<tr><td><code>{_e(ac.get('ac_id'))}</code></td><td>{'yes' if ac.get('user_visible') else 'no'}</td>"
            f"<td><b>Given</b> {_e(ac.get('given'))}<br><b>When</b> {_e(ac.get('when'))}<br><b>Then</b> {_e(ac.get('then'))}</td>"
            f"<td>{_e(ac.get('verification'))}</td></tr>" for ac in s.get("acceptance_criteria", []))
        ifaces = "".join(
            f"<li><code>{_e(i.get('name'))}</code> <span class=\"pill\">{_e(i.get('kind'))} · {_e(i.get('change'))}</span>"
            f"<pre>{_e(i.get('definition'))}</pre></li>" for i in s.get("interfaces", []))
        paths = "".join(f"<li><code>{_e(p)}</code></li>" for p in s.get("allowed_paths", []))
        tp = s.get("test_plan", {})
        cmds = "".join(f"<li><code>{_e(c)}</code></li>" for c in tp.get("commands", []))
        e2e = "".join(f"<li><code>{_e(e.get('command'))}</code> <span class=\"muted\">target {_e(e.get('target'))}, "
                      f"covers {', '.join(e.get('covers_ac', []))}</span></li>" for e in tp.get("e2e", []))
        ps = issues_by_spec.get(sid, {})
        issues = "".join(
            f"<li class=\"{'bad' if i.get('severity') == 'blocking' else 'warn' if i.get('severity') == 'major' else ''}\">"
            f"<b>{_e(i.get('severity'))}</b> {_e(i.get('description'))}<br><span class=\"muted\">{_e(i.get('suggested_resolution', ''))}</span></li>"
            for i in ps.get("issues", []))
        crit = "".join(f"<tr><td><code>{_e(c.get('criterion_id') or c.get('id'))}</code></td><td>{_e(c.get('score'))}</td>"
                       f"<td>{_e(c.get('evidence') or c.get('rationale') or '')}</td></tr>" for c in ps.get("criteria", []))
        for q in s.get("open_questions", []):
            all_questions.append({**q, "question": f"[{sid}] {q.get('question')}"})
        spec_sections += (
            f"<h2 id=\"{_e(anchor)}\">{_e(sid)}: {_e(s.get('title'))}</h2><p>{_e(s.get('goal'))}</p>"
            f"<p class=\"muted\">Work item <code>{_e(s.get('work_item_id'))}</code> · revision {_e(s.get('revision'))} · size {_e(s.get('estimated_size'))}</p>"
            f"<h3>Acceptance criteria</h3><table><tr><th>AC</th><th>user visible</th><th>Given / When / Then</th><th>Verification</th></tr>{ac_rows}</table>"
            f"<h3>Interfaces</h3><ul>{ifaces or '<li class=muted>None.</li>'}</ul>"
            f"<h3>Allowed paths</h3><ul>{paths}</ul>"
            f"<h3>Test plan</h3><ul>{cmds}</ul><p class=\"muted\">End to end:</p><ul>{e2e or '<li class=muted>None.</li>'}</ul>"
            f"<h3>Judge issues</h3><ul>{issues or '<li class=muted>None.</li>'}</ul>"
            + (f"<details><summary>Rubric scores</summary><table><tr><th>criterion</th><th>score</th><th>evidence</th></tr>{crit}</table></details>" if crit else "")
            + (f"<details><summary>spec.md</summary>{md_to_html((spec_md or {}).get(sid, ''))}</details>" if (spec_md or {}).get(sid) else ""))
    nav += [("questions", "Questions"), ("artifacts", "Artifacts")]
    body = (f"<h2 id=\"summary\">Judge summary</h2><div class=\"{cls}\">{_e(overall.get('summary_for_human'))}</div>"
            f"<p class=\"muted\">Result <b class=\"{'ok' if result == 'pass' else 'warn'}\">{_e(result)}</b> · round {_e(verdict.get('round'))} · "
            f"{_e(overall.get('blocking_issue_count', 0))} blocking issues · {len(specs)} specs</p>"
            f"<h2 id=\"coverage\">Goal coverage</h2><table><tr><th>Metric</th><th>Assessment</th><th>Covering acceptance criteria</th><th>Note</th></tr>{gc_rows}</table>"
            + (f"<h2 id=\"coherence\">Coherence</h2>{coh_html}" if coh_html else "")
            + spec_sections + _questions_section(all_questions, "Open questions across specs") + _artifacts_section(shas))
    return _page(f"Gate 2 · {outcome.get('title', run_id)}", f"Specification readiness · run {run_id}", nav, body)
