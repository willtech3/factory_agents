"""Onboarded apps as the workspace sees them: one Factory execution environment per app.

Onboarding turns an app's reviewed recipe into an environment definition in the Anthropic workspace (Claude
Console, Environments), and every later hosted session for that app runs in it. The workspace is what every
Console, checkout and worktree shares; this Console's run directory is not. So the Apps tab reads apps back
from the workspace, and the workspace is where leftovers are retired: a new recipe fingerprint gets a new
definition, and once a recipe verifies, the app's older definitions are archived, so a settled app keeps one.

Only environments Factory created (metadata factory_purpose) are read or archived, never one that a running
session uses, and never on a request alone: every archive is re-checked against a fresh read of the workspace.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

from .errors import Refusal
from .test_environments import app_labels, factory_environments, label_environment

# The shared, network-less environment discovery sessions read source in. One per workspace, not one per app.
DISCOVERY_PROFILE = {"name": "app-discovery", "network": {"allowed_hosts": [], "allow_package_managers": False}}
NAME = re.compile(r"factory-(?P<stem>.+)-(?P<digest>[0-9a-f]{8})-[0-9a-f]{6}")
CACHE_SECONDS = 30
MAX_SESSION_PAGES = 5


def describe(env, links: dict) -> dict:
    """One environment, attributed to an app by its labels, else by this Console's records, else by its name.

    A definition created without an app (a `factory environment` trial) carries only the recipe name in its own name
    (factory-<recipe>-<fingerprint>-<suffix>); those of one recipe are grouped as one app."""
    meta = dict(env.metadata or {})
    link = links.get(env.id) or {}
    match = NAME.fullmatch(env.name or "")
    digest = meta.get("profile_sha256", "")
    discovery = meta.get("factory_purpose") == "discovery"
    repository = (meta.get("factory_repository") or link.get("repository_url") or "").removesuffix(".git").rstrip("/")
    app_id = meta.get("factory_app_id") or link.get("app_id") or ""
    stem = match["stem"] if match else env.name
    key = ("discovery" if discovery else f"repo:{repository.lower()}" if repository
           else f"app:{app_id}" if app_id else f"name:{stem}")
    return {"id": env.id, "name": env.name, "created_at": str(env.created_at), "updated_at": str(env.updated_at),
            "purpose": "discovery" if discovery else "test", "profile_sha256": digest, "key": key,
            "app_id": app_id, "app_name": meta.get("factory_app_name") or link.get("name") or "", "recipe": stem,
            "repository_url": repository, "verified_at": meta.get("factory_verified_at") or link.get("verified_at") or "",
            "local_app_id": link.get("app_id") or "", "current": bool(link.get("current")), "in_use": False}


def arrange(envs: list[dict], pinned: set[str]) -> tuple[list[dict], list[dict], dict | None]:
    """Group environments by app and keep one each: the one in use, else this Console's verified one, else the
    newest verified, else the newest. The rest are leftovers to retire; a pinned (in-use) one never is."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for e in envs:
        e["in_use"] = e["id"] in pinned
        groups[e["key"]].append(e)
    apps, retire, discovery = [], [], None
    for key, members in groups.items():
        members.sort(key=lambda e: (e["in_use"], e["current"], bool(e["verified_at"]), e["created_at"]), reverse=True)
        keep = members[0]
        leftovers = [e for e in members[1:] if not e["in_use"]]
        for e in leftovers:
            retire.append({**e, "reason": "duplicate" if e["profile_sha256"] == keep["profile_sha256"] else "superseded",
                           "replaced_by": keep["id"], "replaced_by_name": keep["name"]})
        if key == "discovery":
            discovery = keep
            continue
        apps.append({
            "key": key, "environment": keep, "environment_count": len(members), "leftover_count": len(leftovers),
            "name": next((e["app_name"] for e in members if e["app_name"]), keep["recipe"]),
            "repository_url": keep["repository_url"] or next((e["repository_url"] for e in members if e["repository_url"]), ""),
            "app_id": keep["app_id"], "local_app_id": keep["local_app_id"] or next((e["local_app_id"] for e in members if e["local_app_id"]), ""),
            "verified_at": keep["verified_at"],
        })
    apps.sort(key=lambda a: a["environment"]["updated_at"], reverse=True)
    retire.sort(key=lambda e: (e["key"], e["created_at"]))
    return apps, retire, discovery


class AppEnvironments:
    """The Apps tab's view of the workspace, and the only code that archives an app's environments."""

    def __init__(self, cfg, onboarding):
        self.cfg, self.onboarding = cfg, onboarding
        self._lock = threading.Lock()
        self._cache: tuple[float, dict] | None = None

    @property
    def client(self):
        return self.onboarding.client

    def console_url(self, environment_id: str) -> str:
        return f"https://platform.claude.com/workspaces/{self.cfg.console_workspace}/environments/{environment_id}"

    def _active(self) -> set[str]:
        """Environments a session is running in right now, started by any Console."""
        page = self.client.beta.sessions.list(statuses=["running", "rescheduling"], limit=100, timeout=30)
        ids: set[str] = set()
        for _ in range(MAX_SESSION_PAGES):
            ids.update(s.environment_id for s in page.data if getattr(s, "environment_id", None))
            if not page.has_next_page():
                break
            page = page.get_next_page()
        return ids

    def _read(self) -> dict:
        links = self.onboarding.environment_links()
        envs = [describe(e, links) for e in factory_environments(self.client) if e.id != self.cfg.environment_id]
        pinned = self._active() | {i for i, link in links.items() if link.get("busy")}
        apps, retire, discovery = arrange(envs, pinned)
        for e in envs + retire:
            e["console_url"] = self.console_url(e["id"])
        return {"configured": True, "error": None, "apps": apps, "retire": retire, "discovery": discovery,
                "environments": envs, "checked_at": datetime.now(timezone.utc).isoformat()}

    def snapshot(self, refresh: bool = False) -> dict:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return {"configured": False, "error": "Set ANTHROPIC_API_KEY to read the apps' execution environments from the workspace.",
                    "apps": [], "retire": [], "discovery": None, "environments": [], "checked_at": None}
        with self._lock:
            if not refresh and self._cache and time.time() - self._cache[0] < CACHE_SECONDS:
                return self._cache[1]
            try:
                out = self._read()
            except Exception as exc:  # noqa: BLE001  (SDK errors can carry request details; report the kind only)
                out = {"configured": True, "error": f"The workspace's environments could not be read ({type(exc).__name__}).",
                       "apps": [], "retire": [], "discovery": None, "environments": [],
                       "checked_at": datetime.now(timezone.utc).isoformat()}
            self._cache = (time.time(), out)
            return out

    def _archive(self, ids: list[str]) -> tuple[list[str], list[dict]]:
        archived, failed = [], []
        for environment_id in ids:
            try:
                self.client.beta.environments.archive(environment_id, timeout=60)
                archived.append(environment_id)
            except Exception as exc:  # noqa: BLE001
                failed.append({"id": environment_id, "error": type(exc).__name__})
        with self._lock:
            self._cache = None
        return archived, failed

    def _fresh(self) -> dict:
        current = self.snapshot(refresh=True)
        if current["error"]:
            raise Refusal(502 if current["configured"] else 409, current["error"])
        return current

    def cleanup(self, environment_ids: list[str]) -> dict:
        """Archive the leftovers the Console listed and a person confirmed, if they are still leftovers now."""
        allowed = {e["id"] for e in self._fresh()["retire"]}
        wanted = [i for i in dict.fromkeys(environment_ids) if i in allowed]
        if not wanted:
            raise Refusal(409, "None of those environments is a leftover any more. The list has been refreshed.")
        archived, failed = self._archive(wanted)
        return {"archived": archived, "failed": failed, "skipped": [i for i in environment_ids if i not in wanted],
                "environments": self.snapshot(refresh=True)}

    def archive(self, environment_id: str) -> dict:
        """Retire one app's environment, e.g. an app that is no longer worked on. Its recipe stays in the repository;
        the next hosted session for it creates a fresh definition."""
        env = next((e for e in self._fresh()["environments"] if e["id"] == environment_id), None)
        if env is None or env["purpose"] != "test":
            raise Refusal(404, "Not a Factory app environment in this workspace")
        if env["in_use"]:
            raise Refusal(409, "A session is running in this environment. Archive it once that session ends.")
        if env["current"]:
            raise Refusal(409, "An app on this Console is verified in this environment. It stays while that app uses it.")
        archived, failed = self._archive([environment_id])
        if failed:
            raise Refusal(502, f"The environment could not be archived ({failed[0]['error']}).")
        return {"archived": archived, "environments": self.snapshot(refresh=True)}

    def settle(self, app: dict, environment_id: str) -> list[str]:
        """After an app's recipe verified in `environment_id`: label that environment as the app's verified one and
        archive the app's other definitions (earlier recipes, duplicates). Returns what was archived."""
        envs = factory_environments(self.client)
        verified = next((e for e in envs if e.id == environment_id), None)
        if verified is None:
            return []
        label_environment(self.client, verified, {**app_labels(app), "factory_verified_at": datetime.now(timezone.utc).isoformat()})
        links = self.onboarding.environment_links()
        described = [describe(e, links) for e in envs if e.id != self.cfg.environment_id]
        pinned = self._active() | {i for i, link in links.items() if link.get("busy")} | {environment_id}
        key = next(e["key"] for e in described if e["id"] == environment_id)
        _, retire, _ = arrange(described, pinned)
        archived, _ = self._archive([e["id"] for e in retire if e["key"] == key])
        return archived


def links_from_records(folders) -> dict[str, dict]:
    """env id -> the local app that used it, from every verification request this Console sent (evidence-*/request.json)
    and each app's current verification. A current, ready app wins over a busy one, which wins over history."""
    links: dict[str, dict] = {}
    for folder in folders:
        try:
            r = json.loads((folder / "app.json").read_text())
        except (OSError, ValueError):
            continue
        verification = r.get("verification") or {}
        ready = r.get("status") in ("ready", "ready_limited") and verification.get("status") == "passed"
        requests = []
        for p in folder.glob("evidence-*/request.json"):
            try:
                requests.append((p.stat().st_mtime, json.loads(p.read_text()).get("environment_id")))
            except (OSError, ValueError, AttributeError):
                continue
        requests.sort()
        latest = requests[-1][1] if requests else None
        for mtime, environment_id in requests + [(float("inf"), verification.get("environment_id"))]:
            if not environment_id:
                continue
            current = ready and environment_id == verification.get("environment_id")
            busy = r.get("status") == "verifying" and environment_id == latest
            rank = (current, busy, mtime)
            if environment_id in links and links[environment_id]["_rank"] >= rank:
                continue
            links[environment_id] = {"app_id": r["app_id"], "name": r.get("name", ""), "repository_url": r.get("repository_url", ""),
                                     "current": current, "busy": busy, "verified_at": r.get("updated_at", "") if current else "",
                                     "_rank": rank}
    return {k: {x: y for x, y in v.items() if x != "_rank"} for k, v in links.items()}
