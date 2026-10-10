# Environments and credentials

`factory-cloud.yaml` is the single environment every stage session uses. It is applied with the agents:

```bash
ant apply --dry-run -v agents environments
```

## Self-hosted GitLab and preview hosts (placeholders)

The GitLab instance is self-hosted. `gitlab.com` appears in `factory-cloud.yaml` and in the credential example below only as a placeholder: replace it with your host, and set `GITLAB_URL` for the orchestrator (see `orchestrator/factory/config.py`). Nothing in this repository calls GitLab until you do.

Two more hosts must be reachable from the sandbox, because the implementer and reviewer run the spec's `test_plan.e2e` entries against them: the **preview environment domain** (whatever your CI templates register as `environment:url` for per-branch previews, typically a wildcard such as `*.preview.example.internal`) and, if you want agents to be able to read dev, the **dev host**. Add them to `allowed_hosts`. Stage 5's dev checks run from the orchestrator, not the sandbox, so the dev host is optional there. Other platform configuration the orchestrator needs: `FACTORY_SERVICE_MANIFEST_PATH` (the fixed manifest path from the platform contract), `FACTORY_PREVIEW_ENVIRONMENT_PREFIX`, `FACTORY_DEV_ENVIRONMENT`, and the optional `FACTORY_SMOKE_COMMANDS` (semicolon-separated, `$BASE_URL` substituted).

## GitLab access (not in these files)

Managed Agents has a first-class `github_repository` session resource with a git proxy that injects the token. There is no GitLab equivalent today, so GitLab is handled in two parts (see `docs/adr/0003-gitlab-integration.md`):

1. **Clone and push from the sandbox** with a vault `environment_variable` credential named `GITLAB_TOKEN`, scoped to the GitLab host, header-only injection. The orchestrator's session kickoff message tells the agent to configure git with an `http.extraHeader` carrying the placeholder, which the egress proxy substitutes. **Spike 1 in the roadmap verifies that your GitLab accepts bearer PATs on git-over-HTTPS; if it does not, fall back to a host-side `push_branch` custom tool.**
2. **Merge request creation** is a custom tool (`open_merge_request`) executed by the orchestrator with its own token. The sandbox never sees it.

The vault itself is created once per workspace and referenced by ID at session create. Vault files are intentionally not committed here because `ant apply` manages vaults as containers only and credentials are added separately:

```bash
ant beta:vaults create --display-name "factory"            # once
ant beta:vaults:credentials create --vault-id vlt_... <<'YAML'
display_name: GitLab PAT (factory bot)
auth:
  type: environment_variable
  secret_name: GITLAB_TOKEN
  secret_value: <paste>
  networking: {type: limited, allowed_hosts: [gitlab.example.internal]}   # your self-hosted host
  injection_location: {header: true, body: false}
YAML
```

Record the vault ID in the orchestrator's config (`FACTORY_VAULT_ID`).
