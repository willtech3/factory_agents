"""Configuration: agent/environment IDs from claude-lock.json, secrets from the environment.

`ant apply` writes `claude-lock.json` at the repo root with one entry per resource file.
The orchestrator never calls `agents.create()`; it only reads IDs here.

Forge and gate-surface selection (ADR-0007, ADR-0008):

    FACTORY_FORGE=github|gitlab          default github
    FORGE_URL / FORGE_TOKEN / FORGE_REPO  with GITHUB_* and GITLAB_URL/GITLAB_TOKEN/GITLAB_PROJECT as aliases
    FACTORY_GATE_SURFACE=tracker|file|web  default tracker when LINEAR_API_KEY is set, else web;
                                          web (or file) = decide in the Factory Console / CLI only.
                                          There is no forge surface: the factory opens no design PR (ADR-0011)
    FACTORY_PAGE_PUBLISHER=local|artifact default local
    LINEAR_API_KEY / LINEAR_TEAM_ID       the tracker surface (Linear); JIRA_* is a stub
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

AGENT_FILES = {
    "app_onboarding": "./agents/app_onboarding.md",
    "codebase_researcher": "./agents/codebase_researcher.md",
    "decomposer": "./agents/decomposer.md",
    "spec_writer": "./agents/spec_writer.md",
    "spec_judge": "./agents/spec_judge.md",
    "implementer": "./agents/implementer.md",
    "reviewer": "./agents/reviewer.md",
    "security_reviewer": "./agents/security_reviewer.md",
    "merge_judge": "./agents/merge_judge.md",
    "integrator": "./agents/integrator.md",
}
ENVIRONMENT_FILE = "./environments/factory-cloud.yaml"
# Agents a run can do without: when not applied, the orchestrator skips their pass and says so.
OPTIONAL_AGENTS = {"security_reviewer", "app_onboarding"}

FORGES = ("github", "gitlab")
GATE_SURFACES = ("tracker", "file", "web")   # web = the Factory Console over the file queue
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
    gate_surface: str                   # tracker | file | web
    page_publisher: str                 # local | artifact
    linear_api_key: str | None
    linear_team_id: str | None
    linear_project_id: str | None       # optional: reuse one Linear project for every run
    deploy_workflow: str | None         # GitHub only: workflow file that deploys (scopes pipeline lookups)
    service_manifest_path: str          # fixed path in the app repo, named by the platform contract
    dev_environment_name: str           # environment the default branch deploys to
    preview_environment_prefix: str     # environment name prefix for per-branch previews
    preview_mode: str                   # platform (the forge's deployment record) | local (the orchestrator starts the service) | managed (an Anthropic-hosted sandbox from the app's test recipe)
    smoke_commands: tuple[str, ...]     # optional suite run against dev after every deploy ($BASE_URL substituted)
    console_workspace: str
    workspace_id: str | None            # ANTHROPIC_WORKSPACE_ID: required when the API key is not scoped to a workspace
    runs_dir: Path
    schemas_dir: Path
    rubrics_dir: Path
    templates_dir: Path
    pin_agent_versions: bool
    skills_dir: Path = REPO_ROOT / "skills"   # platform-contract skill, uploaded into every session and seeded into repositories
    # preview_mode "managed": every verification of a candidate head runs in a hosted sandbox built from the app's recipe
    app_id: str | None = None                 # FACTORY_APP_ID: the onboarded app this run changes (recorded, not an authority)
    test_profile_sha256: str | None = None    # FACTORY_TEST_PROFILE_SHA256: the approved recipe; the candidate's must hash to it
    test_vault_ids: tuple[str, ...] = ()      # FACTORY_TEST_VAULT_IDS: explicit test-only vaults; never the forge's vault
    managed_verify_budget_usd: float = 4.0    # FACTORY_MANAGED_VERIFY_BUDGET_USD: cap per hosted verification session
    artifact_base_url: str = "http://127.0.0.1:8772"  # Factory Console, or an externally hosted artifact service

    def console_session_url(self, session_id: str) -> str:
        return f"https://platform.claude.com/workspaces/{self.console_workspace}/sessions/{session_id}"

    @property
    def repo_url(self) -> str:
        return f"{self.forge_url.rstrip('/')}/{self.forge_repo}"


PREVIEW_MODES = ("platform", "local", "managed")


def _float_env(name: str, default: float) -> float:
    """A float from the environment; an unparsable value becomes NaN, which `missing` reports instead of guessing."""
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return float("nan")


def managed_gaps(cfg: Config) -> list[str]:
    """What is wrong with the preview choice. Managed mode needs a sane per-session cap and well-formed references;
    the recipe, its fingerprint and the vaults' contents are checked against the candidate at verification time."""
    if cfg.preview_mode not in PREVIEW_MODES:
        return [f"FACTORY_PREVIEW={cfg.preview_mode!r} (expected {'|'.join(PREVIEW_MODES)})"]
    if cfg.preview_mode != "managed":
        return []
    gaps = []
    if not (0 < cfg.managed_verify_budget_usd <= 60):
        gaps.append("FACTORY_MANAGED_VERIFY_BUDGET_USD (a cap above 0 and at most 60 USD per hosted verification)")
    if cfg.test_profile_sha256 and not re.fullmatch(r"[0-9a-f]{64}", cfg.test_profile_sha256):
        gaps.append("FACTORY_TEST_PROFILE_SHA256 (the approved recipe's 64-character sha256)")
    if cfg.vault_id and cfg.vault_id in cfg.test_vault_ids:
        gaps.append("FACTORY_TEST_VAULT_IDS must not include the forge vault FACTORY_VAULT_ID")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v) for v in cfg.test_vault_ids):
        gaps.append("FACTORY_TEST_VAULT_IDS (comma-separated vault ids)")
    return gaps


def _first(*names: str, default: str | None = None) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def load_dotenv(path: Path | None = None, override: bool = False) -> list[str]:
    """Load KEY=VALUE lines from the repo's .env into os.environ (never overriding what is set). Returns the keys loaded.
    No dependency, no interpolation; quotes around values are stripped. Values are never logged."""
    path = path or REPO_ROOT / ".env"
    loaded: list[str] = []
    if not path.is_file():
        return loaded
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and (override or key not in os.environ):
            os.environ[key] = value
            loaded.append(key)
    return loaded


def load(lock_path: Path | None = None, dotenv_path: Path | None = None) -> Config:
    load_dotenv(dotenv_path)
    lock_path = lock_path or REPO_ROOT / "claude-lock.json"
    agents: dict[str, AgentRef] = {}
    env_id = os.environ.get("FACTORY_ENVIRONMENT_ID", "")
    if lock_path.exists():
        lock = json.loads(lock_path.read_text())
        resources = lock.get("resources", {})
        for key, file in AGENT_FILES.items():
            entry = resources.get(file)
            if entry:
                v = entry.get("version")
                agents[key] = AgentRef(id=entry["id"], version=int(v) if v not in (None, "") else None)   # ant writes "1"
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
    gate_surface = (os.environ.get("FACTORY_GATE_SURFACE") or ("tracker" if linear_key else "web")).lower()

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
        preview_mode=(os.environ.get("FACTORY_PREVIEW") or "platform").lower(),
        app_id=os.environ.get("FACTORY_APP_ID") or None,
        test_profile_sha256=(os.environ.get("FACTORY_TEST_PROFILE_SHA256") or "").strip().lower() or None,
        test_vault_ids=tuple(v.strip() for v in os.environ.get("FACTORY_TEST_VAULT_IDS", "").split(",") if v.strip()),
        managed_verify_budget_usd=_float_env("FACTORY_MANAGED_VERIFY_BUDGET_USD", 4.0),
        artifact_base_url=os.environ.get("FACTORY_ARTIFACT_BASE_URL", "http://127.0.0.1:8772").rstrip("/"),
        smoke_commands=tuple(c for c in os.environ.get("FACTORY_SMOKE_COMMANDS", "").split(";") if c.strip()),
        console_workspace=os.environ.get("ANTHROPIC_WORKSPACE", "default"),
        workspace_id=os.environ.get("ANTHROPIC_WORKSPACE_ID"),
        runs_dir=Path(os.environ.get("FACTORY_RUNS_DIR", REPO_ROOT / "runs")).resolve(),   # absolute: subprocesses start elsewhere
        schemas_dir=REPO_ROOT / "schemas",
        rubrics_dir=REPO_ROOT / "rubrics",
        templates_dir=REPO_ROOT / "templates",
        skills_dir=REPO_ROOT / "skills",
        pin_agent_versions=os.environ.get("FACTORY_PIN_VERSIONS", "1") == "1",
    )


def anthropic_client(cfg: Config):
    """The SDK client the orchestrator uses. A key that is not scoped to a workspace needs the workspace id header."""
    import anthropic
    headers = {"anthropic-workspace-id": cfg.workspace_id} if cfg.workspace_id else None
    return anthropic.Anthropic(default_headers=headers)


def platform_check(cfg: Config) -> dict:
    """One cheap live call per agent: does the platform have the agents the lock file names? Returns
    {ok, error, agents: [{key, id, version, found, name}]}. Never raises; never logs the key."""
    out: dict = {"ok": False, "error": None, "agents": [], "environment_id": cfg.environment_id}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        out["error"] = "ANTHROPIC_API_KEY is not set"
        return out
    try:
        client = anthropic_client(cfg)
        for key, ref in cfg.agents.items():
            try:
                a = client.beta.agents.retrieve(ref.id)
                out["agents"].append({"key": key, "id": ref.id, "version": ref.version, "found": True,
                                      "name": getattr(a, "name", None), "latest_version": getattr(a, "version", None)})
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "anthropic-workspace-id" in msg:
                    out["error"] = "the API key is not scoped to a workspace: set ANTHROPIC_WORKSPACE_ID (Console -> workspace settings)"
                    return out
                out["agents"].append({"key": key, "id": ref.id, "version": ref.version, "found": False, "error": msg[:200]})
        out["ok"] = bool(out["agents"]) and all(a["found"] for a in out["agents"])
        if not out["ok"] and not out["error"]:
            out["error"] = "some agents in claude-lock.json were not found on the platform"
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return out


def missing(cfg: Config, design_only: bool = False) -> list[str]:
    """Reconciliation before the first run: every agent and resource must exist.

    `design_only` runs (decompose, Gate 1, specify, judge, Gate 2; no code) need the platform and the gate
    surface but no forge, so forge and vault gaps are not reported for them."""
    gaps = [f"agent:{k}" for k in AGENT_FILES if k not in cfg.agents and k not in OPTIONAL_AGENTS]
    if not cfg.environment_id:
        gaps.append("environment_id (run `ant apply agents environments`)")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        gaps.append("ANTHROPIC_API_KEY (the Console workspace the agents live in)")
    if design_only:
        return gaps + _surface_gaps(cfg)
    gaps += managed_gaps(cfg)
    if cfg.forge not in FORGES:
        gaps.append(f"FACTORY_FORGE={cfg.forge!r} (expected github|gitlab)")
    if not cfg.forge_token:
        gaps.append("FORGE_TOKEN (orchestrator-side; GITHUB_TOKEN or GITLAB_TOKEN also accepted)")
    if cfg.forge == "gitlab" and not cfg.vault_id:
        gaps.append("FACTORY_VAULT_ID (sandbox git access for the gitlab forge, ADR-0003)")
    return gaps + _surface_gaps(cfg)


def missing_for_watch(cfg: Config) -> list[str]:
    """What `factory watch` needs: the forge (to read change requests, pipelines, and deployments) and the gate surface
    (to open a DEPLOY_FAILURE gate). No agent runs, so the platform key and the agent ids are not required."""
    gaps = []
    if cfg.forge not in FORGES:
        gaps.append(f"FACTORY_FORGE={cfg.forge!r} (expected github|gitlab)")
    if not cfg.forge_token:
        gaps.append("FORGE_TOKEN (orchestrator-side; GITHUB_TOKEN or GITLAB_TOKEN also accepted)")
    return gaps + _surface_gaps(cfg)


def _surface_gaps(cfg: Config) -> list[str]:
    gaps = []
    if cfg.gate_surface not in GATE_SURFACES:
        gaps.append(f"FACTORY_GATE_SURFACE={cfg.gate_surface!r} (expected tracker|file|web)")
    if cfg.gate_surface == "tracker":
        if not cfg.linear_api_key:
            gaps.append("LINEAR_API_KEY (tracker gate surface)")
        if not cfg.linear_team_id:
            gaps.append("LINEAR_TEAM_ID (tracker gate surface)")
    if cfg.page_publisher not in PAGE_PUBLISHERS:
        gaps.append(f"FACTORY_PAGE_PUBLISHER={cfg.page_publisher!r} (expected local|artifact)")
    return gaps
