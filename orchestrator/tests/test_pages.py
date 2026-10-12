"""Artifact pages rendered from the Ledgerline example, published locally. No network."""
import html

from factory.surfaces.artifact_pages import (ClaudeArtifactPublisher, LocalHtmlPublisher, md_to_html, render_gate1_page,
                                             render_gate2_page)


def test_gate1_page_has_summary_coverage_items_questions_and_hashes(examples, tmp_path):
    dec = examples["decomposition"]
    shas = {"decomposition.json": "a" * 64, "decomposition.md": "b" * 64}
    page = render_gate1_page("run_abc1234567", examples["outcome"], dec, "# Decomposition\n\n- one\n- two\n\n```\ncode\n```", shas)
    assert page.startswith("<!doctype html>") and "<script" not in page and "http" not in page.split("<body>")[0].split("<title>")[0]
    body = page.split("<body>")[1]
    # judge/agent summary first, then coverage, then work items
    assert body.index('id="summary"') < body.index('id="coverage"') < body.index('id="work-items"') < body.index('id="questions"')
    assert html.escape(dec["summary"][:60]) in page
    for m in dec["coverage"]:
        assert html.escape(m["metric"]) in page
    for w in dec["work_items"]:
        assert f"<code>{w['work_item_id']}</code>" in page
    for q in dec["open_questions"]:
        assert html.escape(q["question"][:40]) in page
    assert "a" * 64 in page and "b" * 64 in page and "ADR-0005" in page
    assert "<ul>\n<li>one</li>\n<li>two</li>\n</ul>" in page and "<pre>\ncode\n</pre>" in page      # the decomposition.md, rendered
    url = LocalHtmlPublisher(tmp_path).publish("run_abc1234567", "gate_1", page)
    assert url.startswith("file://") and (tmp_path / "run_abc1234567" / "pages" / "gate_1.html").read_text() == page
    assert ClaudeArtifactPublisher(tmp_path).publish("run_abc1234567", "gate_1b", page).endswith("gate_1b.html")


def test_gate2_page_has_judge_summary_matrix_specs_and_hashes(examples):
    spec = examples["spec_smart_retries"]
    verdict = examples["verdict_round2"]
    specs = {spec["spec_id"]: spec}
    shas = {f"{spec['spec_id']}.json": "c" * 64, "verdict.json": "d" * 64}
    page = render_gate2_page("run_abc1234567", examples["outcome"], specs, verdict, shas, {spec["spec_id"]: "# Spec\n\nGoal."})
    body = page.split("<body>")[1]
    assert body.index('id="summary"') < body.index('id="coverage"') < body.index(f'id="spec-{spec["spec_id"]}"') < body.index('id="questions"')
    assert html.escape(verdict["overall"]["summary_for_human"][:60]) in page
    for g in verdict["goal_coverage"]:
        assert html.escape(g["metric"]) in page
    for ac in spec["acceptance_criteria"]:
        assert f"<code>{ac['ac_id']}</code>" in page
    for i in spec["interfaces"]:
        assert f"<code>{html.escape(i['name'])}</code>" in page
    for p in spec["allowed_paths"]:
        assert html.escape(p) in page
    for e in spec["test_plan"]["e2e"]:
        assert html.escape(e["command"][:30]) in page
    for q in spec["open_questions"]:
        assert html.escape(q["question"][:30]) in page
    rubric = next(p for p in verdict["per_spec"] if p["spec_id"] == spec["spec_id"])["criteria"]
    for c in rubric:          # the rubric table names each criterion as the verdict schema spells it
        assert f"<tr><td><code>{html.escape(c['criterion'])}</code></td><td>{c['score']}</td>" in page
    assert "c" * 64 in page and "d" * 64 in page
    assert "<details><summary>spec.md</summary>" in page


def test_markdown_subset():
    out = md_to_html("## H\n\ntext **bold** `code` [l](https://x.y)\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert "<h3>H</h3>" in out and "<strong>bold</strong>" in out and "<code>code</code>" in out
    assert '<a href="https://x.y">l</a>' in out and "<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>" in out
    assert "&lt;script&gt;" in md_to_html("<script>")
