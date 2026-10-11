"""The change request description's renderer and lint (post-mortem 2026-10-11 §4-§5)."""
from factory import pr_description as pd

URL = "http://127.0.0.1:8772/#/runs/run_abcdef0123#record-spec_x"


def _body(extra: str = "", what: str = "Adds a thing.") -> str:
    return (f"## What changed and why\n\n{what}\n\n## Needs your decision\n\nNone.\n\n{extra}## How it was tested\n\nRan.\n\n"
            f"## Size\n\n3 changed lines in 1 file (+3 / −0).\n\n## Start reading at\n\n- `a.py`\n\n---\n\n[Full run record]({URL})\n")


def test_template_sections_inverted_sections_and_standalone_tags():
    template = "{{! note }}\nA\n{{#items}}\n- {{name}}{{#why}}: {{why}}{{/why}}\n{{/items}}\n{{^items}}\nNone.\n{{/items}}\n{{#extra}}\nX {{extra}}\n{{/extra}}\nB\n"
    assert pd.render_template(template, {"items": [{"name": "a", "why": "w"}, {"name": "b"}], "extra": ""}) == "A\n- a: w\n- b\nB\n"
    assert pd.render_template(template, {"items": [], "extra": "e"}) == "A\nNone.\nX e\nB\n"


def test_a_clean_description_passes_and_each_rule_is_named():
    assert pd.lint(_body(), record_url=URL) == []
    assert any("lines" in p for p in pd.lint(_body() + "x\n" * 60, record_url=URL))
    assert any("exactly once" in p for p in pd.lint(_body() + f"[again]({URL})\n", record_url=URL))
    assert any("`## Size`" in p for p in pd.lint(_body().replace("## Size", "## How big"), record_url=URL))


def test_media_limits():
    shots = "".join(f"![s{i}](https://github.com/o/r/blob/e/s{i}.png?raw=true)\n" for i in range(4))
    assert any("4 screenshots" in p for p in pd.lint(_body(shots), record_url=URL))
    demos = "![d](https://github.com/o/r/blob/e/d.gif?raw=true)\n[demo](https://github.com/o/r/blob/e/d.mp4?raw=true)\n"
    assert any("2 demos" in p for p in pd.lint(_body(demos), record_url=URL))
    ok = shots.splitlines()[0] + "\n" + demos.splitlines()[1] + "\n"
    assert pd.lint(_body(ok), record_url=URL) == []


def test_ids_inside_media_urls_are_not_prose_but_local_links_are_refused():
    media = "![picker](https://github.com/o/r/blob/factory-evidence/run_abcdef0123/spec_x/AC-9.png?raw=true)\n"
    assert pd.lint(_body(media), record_url=URL) == []
    assert any("owner's machine" in p for p in pd.lint(_body("[x](http://localhost:3000/api)\n"), record_url=URL))


def test_product_words_ordinary_english_and_first_person():
    recipe = _body(what="Adds a recipe import button and handles the round-trip to the server; I/O errors show inline.")
    assert any("'recipe'" in p for p in pd.lint(recipe, record_url=URL))
    assert pd.lint(recipe, record_url=URL, allowed_words=["Import recipes from a URL"]) == []
    assert any("first-person" in p for p in pd.lint(_body(what="I re-ran the suites."), record_url=URL))
    assert any("'rounds'" in p for p in pd.lint(_body(what="Took two review rounds."), record_url=URL))
    assert pd.title_problems("spec_x: Add models") and not pd.title_problems("Add models")


def test_facts_order_decisions_and_say_when_the_review_stated_none():
    spec = {"spec_id": "spec_x", "title": "T", "goal": "Users can do X. Metric m moves.", "test_plan": {"commands": []}}
    decisions = [{"decision": f"d{i}", "location": f"a.py:{i}", "affects_product": i == 7} for i in range(8)]
    verdict = {"pr_summary": {"what_changed": "W.", "how_tested": "", "decisions": decisions, "read_first": []}}
    f = pd.facts(spec=spec, verdict=verdict, test_results={}, preview_mode="local", head_sha="a" * 40, diff_stats=None, record_url=URL)
    assert f["decisions"][0]["text"] == "d7" and f["decisions"][-1]["text"] == "2 more in the run record" and len(f["decisions"]) == 7
    assert f["size_line"].startswith("Not measured") and f["has_commands"] is False
    bare = pd.facts(spec=spec, verdict={}, test_results={}, preview_mode="local", head_sha="a" * 40,
                    diff_stats=pd.diff_stats("3\t1\tsrc/a.py\n-\t-\tlogo.png\n"), record_url=URL)
    assert bare["what_changed"] == "Users can do X. Metric m moves." and "did not state the decisions" in bare["decisions"][0]["text"]
    assert bare["size_line"] == "4 changed lines in 2 files (+3 / −1)." and bare["read_first"][0]["path"] == "src/a.py"


def test_paths_and_commands_name_the_app_but_still_carry_no_ids_and_the_banner_counts_lines():
    body = _body(what="Renames `src/journey/steps.ts` and runs `bun test tests/recipe.test.ts`.")
    assert pd.lint(body, record_url=URL) == []
    assert any("AC-3" in p for p in pd.lint(_body(what="Covers `AC-3`."), record_url=URL))
    assert pd.lint(_body(), record_url=URL, reserved_lines=60 - len(_body().splitlines())) == []
    assert any("lines" in p for p in pd.lint(_body(), record_url=URL, reserved_lines=61 - len(_body().splitlines())))
