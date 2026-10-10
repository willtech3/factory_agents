# ADR-0008: Pluggable forge, GitHub default, GitLab second

**Status:** accepted · **Date:** 2026-10-10 · Supersedes the forge-specific parts of [ADR-0003](0003-gitlab-integration.md), which stays in force for the gitlab forge

## Context
The orchestrator was hard-wired to GitLab: a `GitLabClient`, `GITLAB_*` configuration, the vault bearer-header clone trick, `mr.*` event kinds, an `open_merge_request` tool, and GitLab pipeline and environment reads for preview and dev URLs. The user's first end-to-end test runs on a personal GitHub account; the later work setup is self-hosted GitLab. Managed Agents also has a native `github_repository` session resource (clone through Anthropic's git proxy, token never in the sandbox, repositories cached) that the GitLab path has to emulate by hand.

## Decision
1. **A `Forge` protocol** (`orchestrator/factory/forge/__init__.py`) with the slice the orchestrator uses: branches and multi-file commits, change requests (create, state, approval, comments, inline discussions, labels, comments), read-only pipeline and deployment lookups (latest pipeline for a branch or sha, pipeline by id, failed-job log excerpt, environment by name, preview URL for a branch, deployment for a sha), the session repository resource and sandbox git instructions, and `render_feedback_md`. A *change request* means a GitHub pull request or a GitLab merge request; event kinds are `cr.opened|merged|closed`, the work-item stage is `cr_open`, the integrator's tool is `open_change_request`, the template is `templates/cr_template.md`.
2. **`GitHubForge` is the default** (`FACTORY_FORGE=github`): httpx against the REST API; multi-file commits through the Git Data API; PRs through the Pulls and Issues APIs; pipelines are Actions workflow runs (optionally scoped to `FACTORY_DEPLOY_WORKFLOW`); preview and dev URLs come from the Deployments API's `environment_url`, which `templates/github-actions-preview.yml` records. Statuses are normalised to one vocabulary (`success|failed|canceled|skipped|running|pending|manual`). All pipeline and deployment reads are unverified live.
3. **`GitLabForge`** is the former client moved behind the protocol, unchanged in behaviour.
4. **Configuration** is forge-neutral: `FORGE_URL`, `FORGE_TOKEN`, `FORGE_REPO` (`owner/repo` or `group/project`), with `GITHUB_*` and `GITLAB_URL/GITLAB_TOKEN/GITLAB_PROJECT` accepted as aliases. `make_forge(cfg)` selects the implementation.
5. **Sessions get the repository the forge's way.** GitHub: `Forge.session_repository_resource` returns the native `github_repository` resource (`url`, `mount_path: /workspace/repo`, `authorization_token` from `FORGE_TOKEN`, `checkout: {type: branch, name}`); the kickoff tells the agent the repository is mounted and to push with plain `git push`. No vault is needed and the bearer-header instruction is gone from that path. GitLab: no resource; the ADR-0003 vault credential and bearer-header clone instructions remain, and `FACTORY_VAULT_ID` stays required.
6. **Change requests are opened host-side** by the orchestrator through the `open_change_request` custom tool with its own token, on both forges. The GitHub MCP server with a vault OAuth credential would also work, but a custom tool keeps one code path, one token, and deterministic labels and reviewers.

## Why not the alternatives
- **GitHub MCP server in the integrator agent.** Puts PR creation inside the sandbox under the agent's judgement and needs a second credential type (vault `mcp_oauth`). The custom tool is simpler and is already how the design worked.
- **A git-level abstraction only (push and let CI do the rest).** The orchestrator needs CR state for Gate 3, comments for feedback, and deployment URLs for the end-to-end checks; those are forge APIs, not git.
- **Keeping GitLab names and mapping GitHub onto them.** Cheap today, confusing forever. The rename touched schemas, state, docs, and tests once.

## Consequences
- `RunContext.gitlab` is `RunContext.forge`; `LocalVerifier` clones with `Forge.authenticated_url`.
- `environments/factory-cloud.yaml` allows `github.com` and `api.github.com`; the GitLab host is a commented alternative.
- Gate 3 on GitHub is a PR review: `change_request_approved` treats the latest non-comment review per user as that user's position. On a personal repository the owner cannot request a review from themselves (GitHub 422); the forge tolerates it.
- `parse_pipeline_webhook` dispatches on payload shape (GitLab `object_kind: pipeline` vs GitHub `workflow_run`).
- Pre-ADR names (`GitLabClient`, `on_mr_merged`) remain as aliases for one release.
- A repository can be created by the factory (`Forge.repository_exists` / `create_repository`, `ensure_repository`) when the outcome marks it `create: true`; the forge is then bound to the outcome's repository, and `FORGE_REPO` is derived from the outcome's URL when unset.
