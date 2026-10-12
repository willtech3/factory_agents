"""Media in the change request without media in its diff (post-mortem 2026-10-11 §4, ADR-0012): what may be published,
and how each forge publishes it."""
import hashlib

import httpx
from support.forge_http import Seq, github_forge

from factory import pr_media
from factory.forge import Media
from factory.forge.gitlab import GitLabForge

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
GIF = b"GIF89a" + b"\x00" * 24
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 24
HEAD = "a" * 40


def _evidence(root, n, *, status="passed", head=HEAD, viewport=(1440, 900), seen=None, demo=GIF, credential=False, runner=True):
    media = []
    for i, content in enumerate([PNG, PNG, PNG] + ([demo] if demo else [])):
        name = f"check{n}-{i}.{'png' if content == PNG else 'gif' if content == GIF else 'webm'}"
        (root / "visual").mkdir(parents=True, exist_ok=True)
        (root / "visual" / name).write_bytes(content)
        media.append({"name": name, "role": "screenshot" if content == PNG else "demo", "artifact": f"visual/{name}",
                      "sha256": hashlib.sha256(content).hexdigest(), "caption": f"step {i + 1}",
                      "mime_type": {PNG: "image/png", GIF: "image/gif", WEBM: "video/webm"}[content]})
    vp = {"width": viewport[0], "height": viewport[1]}
    return {"id": f"req{n}abcdef", "description": f"Check {n}", "status": status, "head_sha": head, "viewport": vp,
            "viewport_observed": seen or vp, "media": media, "factory_runner": runner, **({"credential_seen": True} if credential else {})}


def test_three_screenshots_and_one_demo_from_passing_checks_on_the_approved_head(tmp_path):
    evidence = [_evidence(tmp_path, 1), _evidence(tmp_path, 2, demo=WEBM), _evidence(tmp_path, 3),
                _evidence(tmp_path, 4, status="blocked"), _evidence(tmp_path, 5, head="b" * 40)]
    items, why = pr_media.select(evidence, HEAD, tmp_path)
    assert [i.role for i in items] == ["screenshot"] * 3 + ["demo"] and why == "3 screenshots and 1 demo"
    # the last step of each passing check first; the GIF demo is preferred because it plays inline everywhere
    assert [i.alt for i in items] == ["step 3", "step 3", "step 3", "Demo: step 4"] and items[-1].mime_type == "image/gif"
    assert {i.name.split("-")[0] for i in items[:3]} == {"req1abcd", "req2abcd", "req3abcd"}


def test_nothing_is_published_after_a_credential_was_seen_or_without_the_requested_viewport(tmp_path):
    assert pr_media.select([_evidence(tmp_path, 1), _evidence(tmp_path, 2, status="blocked", credential=True)], HEAD, tmp_path)[0] == []
    items, why = pr_media.select([_evidence(tmp_path, 1, seen={"width": 1280, "height": 720})], HEAD, tmp_path)
    assert items == [] and "viewport" in why
    # a repository's own visual command or a connected browser never checked the screen for credentials
    assert pr_media.select([_evidence(tmp_path, 1, runner=False)], HEAD, tmp_path)[0] == []


def test_a_file_that_changed_since_verification_or_a_caption_with_a_credential_is_skipped(tmp_path):
    e = _evidence(tmp_path, 1)
    (tmp_path / e["media"][2]["artifact"]).write_bytes(PNG + b"tampered")
    e["media"][1]["caption"] = "token ghp_" + "A" * 36
    items, _ = pr_media.select([e], HEAD, tmp_path)
    assert [i.alt for i in items] == ["step 1", "Demo: step 4"]


def test_github_publishes_on_an_orphan_branch_and_links_a_video():
    routes = {
        "GET /repos/will/ledgerline/git/ref/heads/factory-evidence": Seq([(404, {"message": "Not Found"}), (200, {"object": {"sha": "orphan1"}})]),
        "POST /repos/will/ledgerline/git/blobs": (201, lambda b, p: {"sha": "blob1"}),
        "POST /repos/will/ledgerline/git/trees": (201, {"sha": "tree1"}),
        "POST /repos/will/ledgerline/git/commits": (201, lambda b, p: {"sha": "commit-" + str(len(b["parents"]))}),
        "POST /repos/will/ledgerline/git/refs": (201, {"ref": "refs/heads/factory-evidence"}),
        "GET /repos/will/ledgerline/git/commits/orphan1": {"tree": {"sha": "tree0"}},
        "PATCH /repos/will/ledgerline/git/refs/heads/factory-evidence": {"object": {"sha": "commit-1"}},
    }
    f, rec = github_forge(routes)
    markdown = f.publish_media("run_x/spec_y/abc", [Media("shot.png", PNG, "image/png", "The picker", "screenshot"),
                                                    Media("demo.webm", WEBM, "video/webm", "Demo: pick and chat", "demo")])
    assert markdown == ("![The picker](https://github.com/will/ledgerline/blob/factory-evidence/run_x/spec_y/abc/shot.png?raw=true)\n\n"
                        "[▶ Demo: pick and chat (WEBM recording)](https://github.com/will/ledgerline/blob/factory-evidence/run_x/spec_y/abc/demo.webm?raw=true)")
    orphan = next(b for m, p, _, b in rec.calls if p.endswith("/git/commits") and m == "POST")
    assert orphan["parents"] == [] and "base_tree" not in next(b for m, p, _, b in rec.calls if p.endswith("/git/trees"))
    tree = [b for m, p, _, b in rec.calls if p.endswith("/git/trees")][-1]
    assert tree["base_tree"] == "tree0" and {e["path"] for e in tree["tree"]} == {"run_x/spec_y/abc/shot.png", "run_x/spec_y/abc/demo.webm"}
    assert not any("/pulls" in p or p.endswith("/heads/main") for _, p, _, _ in rec.calls)      # never the change or main


def test_gitlab_publishes_with_the_project_uploads_api():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.raw_path.decode(), request.headers.get("content-type", "")))
        return httpx.Response(201, json={"url": "/uploads/abc123/demo.webm", "markdown": "![demo](/uploads/abc123/demo.webm)"})
    f = GitLabForge("https://gitlab.example", "glpat", "group/app")
    f.http = httpx.Client(transport=httpx.MockTransport(handler), headers={"PRIVATE-TOKEN": "glpat"})
    md = f.publish_media("run_x", [Media("demo.webm", WEBM, "video/webm", "Demo: pick", "demo")])
    assert md == "![Demo: pick](/uploads/abc123/demo.webm)"
    assert seen == [("POST", "/api/v4/projects/group%2Fapp/uploads", seen[0][2])] and seen[0][2].startswith("multipart/form-data")
