"""What a change request may show: up to three screenshots and one short demo, from browser checks that passed.

Only media from a check the factory's own browser runner ran (the one runner that checks the screen for credentials)
that passed on the head the merge judge approved, observed at the viewport the spec asked for, already validated by the
controller (`visual.collect`: signature, size, plain names, fingerprints) and during which no test credential was on
screen. If any check of this spec saw a credential, nothing is published. Each file is
re-read from the artifact store and must still hash to the fingerprint recorded at verification, and its caption must
carry no credential shape (`security.secret_scan`). Post-mortem 2026-10-11 §4 and §7: the reviewer gets a couple of
screenshots and a demo; every other screenshot and recording stays in the run record.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from . import security, visual
from .forge import Media

MAX_SCREENSHOTS = 3


def select(evidence: list[dict[str, Any]], head_sha: str, artifacts_root: Path) -> tuple[list[Media], str]:
    """(media to publish, why nothing or what was chosen) for one spec's browser checks."""
    mine = [e for e in evidence if e.get("head_sha") == head_sha]
    if any(e.get("credential_seen") for e in evidence):
        return [], "a test credential was visible during a browser check, so no media from this change is published"
    # Only the factory's own runner checks the screen for credentials; a repository's visual command or a connected
    # browser operator may pass without that check, so its media stays in the run record.
    usable = [e for e in mine if e.get("factory_runner") and e.get("status") == "passed" and e.get("viewport")
              and e.get("viewport_observed") == e.get("viewport")]
    if not usable:
        return [], "no factory browser check passed at its requested viewport on the approved head"
    shots: list[tuple[dict[str, Any], dict[str, Any]]] = []
    queues = [[m for m in e.get("media") or [] if m.get("role") == "screenshot"][::-1] for e in usable]
    while len(shots) < MAX_SCREENSHOTS and any(queues):           # the last step of each check first, then earlier ones
        for e, q in zip(usable, queues):
            if q and len(shots) < MAX_SCREENSHOTS:
                shots.append((e, q.pop(0)))
    demos = sorted(((e, m) for e in usable for m in e.get("media") or [] if m.get("role") == "demo"),
                   key=lambda em: em[1].get("mime_type") != "image/gif")      # a GIF plays inline on every forge
    chosen = shots + demos[:1]
    out = []
    for e, m in chosen:
        item = _load(e, m, artifacts_root)
        if item is not None:
            out.append(item)
    return out, f"{sum(1 for i in out if i.role == 'screenshot')} screenshots and {sum(1 for i in out if i.role == 'demo')} demo"


def _load(evidence: dict[str, Any], media: dict[str, Any], artifacts_root: Path) -> Media | None:
    artifact = media.get("artifact")
    if not artifact:
        return None
    path = (artifacts_root / artifact).resolve()
    if not path.is_relative_to(artifacts_root.resolve()) or not path.is_file():
        return None
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != media.get("sha256") or visual.media_type(path) != media.get("mime_type"):
        return None
    caption = str(media.get("caption") or evidence.get("description") or "Screenshot")
    alt = " ".join(caption.replace("[", "(").replace("]", ")").split())[:120]
    if security.secret_scan({"caption": [alt, str(evidence.get("description") or "")]})["hits"]:
        return None
    name = f"{evidence.get('id', 'check')[:8]}-{media['name']}"
    return Media(name=name, content=content, mime_type=media["mime_type"], alt=("Demo: " if media["role"] == "demo" else "") + alt,
                 role=media["role"])


__all__ = ["MAX_SCREENSHOTS", "select"]
