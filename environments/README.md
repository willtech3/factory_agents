# Environments and credentials

`factory-cloud.yaml` is the single environment every stage session uses. It is applied with the agents:

```bash
ant apply --dry-run -v agents environments
```

## Allowed hosts

Networking is `limited`: package managers plus the forge host. The file ships with `github.com` and `api.github.com` (the default forge, [ADR-0008](../docs/adr/0008-pluggable-forge-github-default.md)); the self-hosted GitLab host is a commented alternative for the gitlab forge. Two more hosts must be reachable from the sandbox, because the implementer and reviewer run the spec's `test_plan.e2e` entries against them: the **preview environment domain** (whatever your CI records as the deployment's `environment_url`, typically a wildcard such as `*.preview.example.com`) and, if you want agents to be able to read dev, the **dev host**. Stage 5's dev checks run from the orchestrator, not the sandbox, so the dev host is optional there.

Other platform configuration the orchestrator needs: `FACTORY_SERVICE_MANIFEST_PATH` (the fixed manifest path from the platform contract), `FACTORY_PREVIEW_ENVIRONMENT_PREFIX`, `FACTORY_DEV_ENVIRONMENT`, `FACTORY_DEPLOY_WORKFLOW` (GitHub: the workflow file to scope pipeline lookups to), and the optional `FACTORY_SMOKE_COMMANDS` (semicolon-separated, `$BASE_URL` substituted). See `orchestrator/factory/config.py`.

## GitHub access (default; no vault)

Sessions mount the repository with the platform's native `github_repository` resource: the orchestrator passes `FORGE_TOKEN` as the resource's `authorization_token`, Anthropic's git proxy injects it for clone and push, and nothing in the sandbox can read it. The same token is the orchestrator's own credential for the REST API (change requests, labels, workflow runs, deployments). A fine-grained personal access token scoped to the one repository is enough; the permissions are listed in [docs/07-personal-e2e-setup.md](../docs/07-personal-e2e-setup.md).

## GitLab access (second forge; vault credential)

There is no GitLab equivalent of the `github_repository` resource, so GitLab is handled in two parts (see [ADR-0003](../docs/adr/0003-gitlab-integration.md)):

1. **Clone and push from the sandbox** with a vault `environment_variable` credential named `GITLAB_TOKEN`, scoped to the GitLab host, header-only injection. The orchestrator's session kickoff tells the agent to configure git with an `http.extraHeader` carrying the placeholder, which the egress proxy substitutes. **The roadmap's work-setup spike verifies that your GitLab accepts bearer PATs on git-over-HTTPS; if it does not, fall back to a host-side `push_branch` custom tool.**
2. **Change request creation** is the `open_change_request` custom tool executed by the orchestrator with its own token (`FORGE_TOKEN`, alias `GITLAB_TOKEN`). The sandbox never sees it.

The vault itself is created once per workspace and referenced by ID at session create (`FACTORY_VAULT_ID`, required only for the gitlab forge). Vault files are intentionally not committed here because `ant apply` manages vaults as containers only and credentials are added separately:

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

Set `FACTORY_FORGE=gitlab`, `FORGE_URL`, `FORGE_REPO=group/project`, and swap the host in `factory-cloud.yaml`.
