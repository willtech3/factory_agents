"""Configuration: agent/environment IDs from claude-lock.json, secrets from the environment.

`ant apply` writes `claude-lock.json` at the repo root with one entry per resource file.
The orchestrator never calls `agents.create()`; it only reads IDs here.
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


@dataclass(frozen=True)
class AgentRef:
    id: str
    version: int | None


@dataclass(frozen=True)
class Config:
    agents: dict[str, AgentRef]
    environment_id: str
    vault_id: str | None
    gitlab_url: str
    gitlab_token: str | None
    gitlab_project: str                 # "group/project"
    console_workspace: str
    runs_dir: Path
    schemas_dir: Path
    rubrics_dir: Path
    templates_dir: Path
    pin_agent_versions: bool

    def console_session_url(self, session_id: str) -> str:
        return f"https://platform.claude.com/workspaces/{self.console_workspace}/sessions/{session_id}"


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
    return Config(
        agents=agents,
        environment_id=env_id,
        vault_id=os.environ.get("FACTORY_VAULT_ID"),
        gitlab_url=os.environ.get("GITLAB_URL", "https://gitlab.com"),
        gitlab_token=os.environ.get("GITLAB_TOKEN"),
        gitlab_project=os.environ.get("GITLAB_PROJECT", ""),
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
    if not cfg.gitlab_token:
        gaps.append("GITLAB_TOKEN (orchestrator-side, for MR creation)")
    if not cfg.gitlab_project:
        gaps.append("GITLAB_PROJECT")
    if not cfg.vault_id:
        gaps.append("FACTORY_VAULT_ID (sandbox git access)")
    return gaps
