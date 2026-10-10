# ADR-0003: GitLab integration without a native repository resource

**Status:** accepted for the `gitlab` forge only, with a verification spike · **Date:** 2026-10-09 · See [ADR-0008](0008-pluggable-forge-github-default.md): GitHub is the default forge and uses the native `github_repository` resource instead of this path.

## Context
The target forge is GitLab. Managed Agents has a `github_repository` session resource with an Anthropic-side git proxy that injects the token; there is no GitLab equivalent. We need the sandbox to clone and push, and we need to open merge requests, without the GitLab token ever being readable inside the sandbox.

## Decision
Split the problem.
1. **Clone and push from the sandbox** using a vault `environment_variable` credential `GITLAB_TOKEN`, scoped to the GitLab host, **header-only** injection. The orchestrator's kickoff tells the agent to run git with `http.extraHeader="Authorization: Bearer $GITLAB_TOKEN"`. Inside the sandbox the variable is an opaque placeholder; the egress proxy substitutes the real token into the header on requests to the allowed host only.
2. **Merge request creation and design-MR commits** via custom tools executed by the orchestrator (`open_merge_request`) and direct orchestrator calls (`gitlab.py`). The token lives only in the orchestrator's environment.
3. **Environment networking** is `limited` with the GitLab host in `allowed_hosts` and package managers allowed.

## Why not the alternatives
- **URL-embedded credentials** (`https://oauth2:TOKEN@gitlab...`): git converts these to a Basic header with the token base64-encoded, so the placeholder would not be substituted. Also URL-path secrets are explicitly not substituted by the platform.
- **Self-hosted sandbox**: solves egress control but loses the hosted toolset, repository caching, and `environment_variable` credentials (not supported there). Keep as a fallback for repositories that cannot leave the VPC.
- **Mirror to GitHub** and use the native resource: adds a mirror to operate and a second source of truth.

## Open verification (spike 1)
Confirm that the target GitLab accepts a personal or project access token as `Authorization: Bearer` on git-over-HTTPS. GitLab documents bearer tokens for the API; for git smart HTTP, OAuth2 tokens are documented via the `oauth2` basic-auth user, and PAT-as-bearer behaviour should be tested against the actual instance. If it fails, the fallback is a `push_branch` custom tool: the agent emits the branch name and the orchestrator fetches the patch from `/mnt/session/outputs/` and pushes host-side. Clone can always be done read-only via the orchestrator uploading a tarball of the repository at the base sha as a session file resource.

## Consequences
- GitLab is treated as a first-class review surface (design MR for Gates 1 and 2; code MR for Gate 3) and a webhook source for MR state.
- For GitHub, the clone step is the `github_repository` resource and change requests are still opened host-side by `open_change_request` (ADR-0008); nothing else changes.
