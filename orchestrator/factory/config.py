"""Configuration: agent/environment IDs from claude-lock.json, secrets from the environment.

`ant apply` writes `claude-lock.json` at the repo root with one entry per resource file.
The orchestrator never calls `agents.create()`; it only reads IDs here.

Forge and gate-surface selection (ADR-0007, ADR-0008):

    FACTORY_FORGE=github|gitlab          default github
    FORGE_URL / FORGE_TOKEN / FORGE_REPO  with GITHUB_* and GITLAB_URL/GITLAB_TOKEN/GITLAB_PROJECT as aliases
    FACTORY_GATE_SURFACE=forge|tracker|file|web   default tracker when LINEAR_API_KEY is set, else forge;
                                          web (or file) = decide in the Factory Console / CLI only
    FACTORY_PAGE_PUBLISHER=local|artifact default local
    LINEAR_API_KEY / LINEAR_TEAM_ID       the tracker surface (Linear); JIRA_* is a stub
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

AGENT_FILES = {
    "codebase_researcher": "./agents/codebase_researcher.md",
    "decomposer": "./agents/decomposer.md",
    "spec_writer": "./agents/spec_writer.md",
    "spec_judge": "./agents/spec_judge.md",
    "implementer": "./agents/implementer.md",
    "reviewer": "./agents/reviewer.md",
    "merge_judge": "./agents/merge_judge.md",
    "integrator": "./agents/integrator.md",
}
ENVIRONMENT_FILE = "./environments/factory-cloud.yaml"

FORGES = ("github", "gitlab")
GATE_SURFACES = ("forge", "tracker", "file", "web")   # web = the Factory Console over the file queue
PAGE_PUBLISHERS = ("local", "artifact")
DEFAULT_FORGE_URL = {"github": "https://github.com", "gitlab": "https://gitlab.com"}


@dataclass(frozen=True)
class AgentRef:
    id: str
    version: int | None


@dataclass(frozen=True)
class Config:
    agents: dict[str, AgentRef]
    environment_id: str
    vault_id: str | None                # GitLab forge only: sandbox git credential (ADR-0003)
    forge: str                          # github | gitlab
    forge_url: str                      # https://github.com, or the self-hosted GitLab host
    forge_token: str | None             # orchestrator-side token; also the github_repository authorization_token
    forge_repo: str                     # "owner/repo" (GitHub) or "group/project" (GitLab)
    gate_surface: str                   # forge | tracker | file
    page_publisher: str                 # local | artifact
    linear_api_key: str | None
    linear_team_id: str | None
    linear_project_id: str | None       # optional: reuse one Linear project for every run
    deploy_workflow: str | None         # GitHub only: workflow file that deploys (scopes pipeline lookups)
    service_manifest_path: str          # fixed path in the app repo, named by the platform contract
    dev_environment_name: str           # environment the default branch deploys to
    preview_environment_prefix: str     # environment name prefix for per-branch previews
    smoke_commands: tuple[str, ...]     # optional suite run against dev after every deploy ($BASE_URL substituted)
    console_workspace: str
    runs_dir: Path
    schemas_dir: Path
    rubrics_dir: Path
    templates_dir: Path
    pin_agent_versions: bool

    def console_session_url(self, session_id: str) -> str:
        return f"https://platform.claude.com/workspaces/{self.console_workspace}/sessions/{session_id}"

    @property
    def repo_url(self) -> str:
        return f"{self.forge_url.rstrip('/')}/{self.forge_repo}"


def _first(*names: str, default: str | None = None) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def load(lock_path: Path | None = None) -> Config:
    lock_path = lock_path or REPO_ROOT / "claude-lock.json"
    agents: dict[str, AgentRef] = {}
    env_id = os.environ.get("FACTORY_ENVIRONMENT_ID", "")
    if lock_path.exists():
        lock = json.loads(lock_path.read_text())
        resources = lock.get("resources", {})
        for key, file in AGENT_FILES.items():
            entry = resources.get(file)
            if entry:
                agents[key] = AgentRef(id=entry["id"], version=entry.get("version"))
        env_entry = resources.get(ENVIRONMENT_FILE)
        if env_entry and not env_id:
            env_id = env_entry["id"]
    # Explicit env vars win, so a developer can point one agent at an experimental version.
    for key in AGENT_FILES:
        override = os.environ.get(f"FACTORY_AGENT_{key.upper()}_ID")
        if override:
            agents[key] = AgentRef(id=override, version=None)

    forge = (os.environ.get("FACTORY_FORGE") or "github").lower()
    if forge == "github":
        forge_url = _first("FORGE_URL", "GITHUB_URL", default=DEFAULT_FORGE_URL["github"])
        forge_token = _first("FORGE_TOKEN", "GITHUB_TOKEN")
        forge_repo = _first("FORGE_REPO", "GITHUB_REPO", "GITHUB_REPOSITORY", default="")
    else:
        forge_url = _first("FORGE_URL", "GITLAB_URL", default=DEFAULT_FORGE_URL.get(forge, ""))
        forge_token = _first("FORGE_TOKEN", "GITLAB_TOKEN")
        forge_repo = _first("FORGE_REPO", "GITLAB_PROJECT", default="")
    linear_key = os.environ.get("LINEAR_API_KEY")
    gate_surface = (os.environ.get("FACTORY_GATE_SURFACE") or ("tracker" if linear_key else "forge")).lower()

    return Config(
        agents=agents,
        environment_id=env_id,
        vault_id=os.environ.get("FACTORY_VAULT_ID"),
        forge=forge,
        forge_url=forge_url or "",
        forge_token=forge_token,
        forge_repo=forge_repo or "",
        gate_surface=gate_surface,
        page_publisher=(os.environ.get("FACTORY_PAGE_PUBLISHER") or "local").lower(),
        linear_api_key=linear_key,
        linear_team_id=os.environ.get("LINEAR_TEAM_ID"),
        linear_project_id=os.environ.get("LINEAR_PROJECT_ID"),
        deploy_workflow=os.environ.get("FACTORY_DEPLOY_WORKFLOW"),
        service_manifest_path=os.environ.get("FACTORY_SERVICE_MANIFEST_PATH", "service_manifest.json"),
        dev_environment_name=os.environ.get("FACTORY_DEV_ENVIRONMENT", "dev"),
        preview_environment_prefix=os.environ.get("FACTORY_PREVIEW_ENVIRONMENT_PREFIX", "preview/"),
        smoke_commands=tuple(c for c in os.environ.get("FACTORY_SMOKE_COMMANDS", "").split(";") if c.strip()),
        console_workspace=os.environ.get("ANTHROPIC_WORKSPACE", "default"),
        runs_dir=Path(os.environ.get("FACTORY_RUNS_DIR", REPO_ROOT / "runs")),
        schemas_dir=REPO_ROOT / "schemas",
        rubrics_dir=REPO_ROOT / "rubrics",
        templates_dir=REPO_ROOT / "templates",
        pin_agent_versions=os.environ.get("FACTORY_PIN_VERSIONS", "1") == "1",
    )


def missing(cfg: Config) -> list[str]:
    """Reconciliation before the first run: every agent and resource must exist."""
    gaps = [f"agent:{k}" for k in AGENT_FILES if k not in cfg.agents]
    if not cfg.environment_id:
        gaps.append("environment_id (run `ant apply agents environments`)")
    if cfg.forge not in FORGES:
        gaps.append(f"FACTORY_FORGE={cfg.forge!r} (expected github|gitlab)")
    if not cfg.forge_token:
        gaps.append("FORGE_TOKEN (orchestrator-side; GITHUB_TOKEN or GITLAB_TOKEN also accepted)")
    if not cfg.forge_repo:
        gaps.append("FORGE_REPO (owner/repo or group/project)")
    if cfg.forge == "gitlab" and not cfg.vault_id:
        gaps.append("FACTORY_VAULT_ID (sandbox git access for the gitlab forge, ADR-0003)")
    if cfg.gate_surface not in GATE_SURFACES:
        gaps.append(f"FACTORY_GATE_SURFACE={cfg.gate_surface!r} (expected forge|tracker|file|web)")
    if cfg.gate_surface == "tracker":
        if not cfg.linear_api_key:
            gaps.append("LINEAR_API_KEY (tracker gate surface)")
        if not cfg.linear_team_id:
            gaps.append("LINEAR_TEAM_ID (tracker gate surface)")
    if cfg.page_publisher not in PAGE_PUBLISHERS:
        gaps.append(f"FACTORY_PAGE_PUBLISHER={cfg.page_publisher!r} (expected local|artifact)")
    return gaps
