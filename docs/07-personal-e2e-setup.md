# Running the factory end to end with a personal GitHub repo and a personal Linear workspace

This is the first real run. It uses the defaults the orchestrator now has: GitHub as the forge ([ADR-0008](adr/0008-pluggable-forge-github-default.md)) and Linear as the gate surface ([ADR-0007](adr/0007-pluggable-gate-surfaces.md)). The platform and GitHub paths have run live (2026-10-10, see the [post-mortem](postmortems/2026-10-10-first-live-runs.md)); those runs decided gates in the Console (`FACTORY_GATE_SURFACE=web`), so the Linear surface has not been confirmed live. Expect field-name surprises in `surfaces/tracker.py` and fix them as you go.

What you need: an Anthropic Console account with Managed Agents access, a personal GitHub account, a personal Linear workspace (free tier is fine), Python 3.11+, git, and the `ant` CLI (`brew install anthropics/tap/ant`; version 1.30 or later for `ant apply`).

## 0. The quickest real test: a design run

Before the full setup, you can see real agents run with only the platform configured. Put `ANTHROPIC_API_KEY` (and `ANTHROPIC_WORKSPACE_ID` when the key is not scoped to a workspace; the Console's Settings page tells you) in `.env` at the repository root, start `factory web`, open Outcomes, choose **Load the sample app outcome** (it fills the form from `examples/sample-app/outcome.json`), save, and choose **Start design run**. The decomposer asks its question and opens Gate 1, the spec writers and the judge run, Gate 2 opens, and the run completes without touching any repository. Every session appears under Settings and in the Anthropic Console. The same thing from a terminal is `factory run examples/sample-app/outcome.json --design-only`.

## 1. Apply the agents and the environment

```bash
ant auth login                                   # once per machine
ant apply --dry-run -v agents environments       # review the plan: 8 agents, 1 environment
ant apply --yes agents environments              # writes claude-lock.json (already committed for this workspace)
```

`claude-lock.json` is managed by `ant apply`: it records the organization, the workspace, and each agent's id and version, and `ant apply` refuses credentials for any other workspace. Editing a file under `agents/` or `environments/` and re-running `ant apply` creates a new version; `factory run` pins the version the lock file names. If you apply in a different workspace, commit the new lock file. `environments/factory-cloud.yaml` allows `github.com` and `api.github.com`; sessions can push through the git proxy and nothing else.

## 1b. Starting from an empty repository

The outcome's repository does not have to exist. Mark it `"create": true` in the outcome (or tick "Create this repository if it does not exist" on the Console's outcome form) and name the URL you want, such as `https://github.com/<you>/new-service`. Before the run starts, the orchestrator creates it on the code host (private unless `visibility: public`, initialised with a README) and commits the preview workflow template, then records `repository.created` on the run. The token needs repository administration rights for that one call (fine-grained: Administration read/write; classic: `repo`). The decomposer is told the repository is empty and makes the first work item the project skeleton: build and test commands, the service manifest at the contract's path, the preview workflow; everything else depends on it. `FORGE_REPO` can be left unset: it is taken from the outcome's URL, and must match it when set.

The factory seeds `.claude/skills/platform-contract/SKILL.md` (from `skills/platform-contract/`, so the agents know what this platform provides and requires) into every repository whose default branch lacks it, created or existing, and also uploads it into every session as `skills/platform-contract/SKILL.md`. A repository it creates also gets `.github/workflows/preview.yml`. The workflow needs a token with the `workflow` scope (`gh auth refresh -s workflow`); without it the run logs the refusal and continues, which is fine with `FACTORY_PREVIEW=local`.

## 2. A small target repository

The factory changes a repository other than this one. `examples/sample-app/` is a small FastAPI todo service with tests, a manifest, the preview workflow, and an outcome ("Todos with due dates and an overdue view") written for it; its README shows how to push it to a repository of yours with `gh`. If you use your own app instead, it needs two things:

- `service_manifest.json` at the root (copy `examples/ledgerline/service_manifest.json` and edit; the path is configurable with `FACTORY_SERVICE_MANIFEST_PATH`).
- `.github/workflows/preview.yml` from [`templates/github-actions-preview.yml`](../templates/github-actions-preview.yml). Its deploy step is a placeholder that only echoes; replace it with a real deploy (Fly, Render, Cloud Run, a docker host) when you want the end-to-end checks to hit a running instance. With the placeholder, the workflow still records a Deployment with an `environment_url`, so the orchestrator's plumbing (preview status, dev deploy observation) can be exercised; the e2e commands will fail against the fake URL and that failure is the correct, fail-closed behaviour.

Write an outcome for it. `examples/ledgerline/outcome.json` is the shape; change `target_repositories[0].url` to your repository, `default_branch`, `paths_in_scope`, and the owner handle to your GitHub login. Keep it small: one metric, two or three work items' worth of scope.

## 3. GitHub token

Create a **fine-grained personal access token** (Settings → Developer settings → Fine-grained tokens) scoped to the target repository only, with these repository permissions:

| Permission | Access | Used for |
|---|---|---|
| Contents | Read and write | clone through the git proxy, implementation and optional design-branch commits |
| Pull requests | Read and write | opening the change request, reading reviews and comments |
| Actions | Read | workflow run status for `preview.json` and Stage 5 |
| Deployments | Read | `environment_url` for the preview and dev URLs |
| Metadata | Read | implied |

The same token is the `authorization_token` on the `github_repository` session resource (so the sandbox clones through Anthropic's proxy without seeing it) and the orchestrator's own token for the REST API. No vault is needed for the GitHub forge. If `gh` is logged in with the `repo` scope, `export FORGE_TOKEN=$(gh auth token)` works for a first run without creating a token by hand.

## 4. Linear API key and team id

Linear → Settings → API → Personal API keys → create one. Then find the team id: in Linear, open your team's settings, or run

```bash
curl -s https://api.linear.app/graphql -H "Authorization: $LINEAR_API_KEY" -H 'Content-Type: application/json' \
  -d '{"query":"{ teams { nodes { id name key } } }"}'
```

The team needs the usual workflow states (Backlog, Todo, In Progress, In Review, Done, Canceled). `surfaces/tracker.py` maps work-item stages to those names and falls back to state *types* when names differ. Labels `factory:*` are created on demand.

Optional: `LINEAR_PROJECT_ID` to put every run in one existing project instead of creating one per run.

## 5. Environment variables

Put them in `.env` at the repository root (ignored by git; loaded by `factory`, never overriding what is already exported) or export them:

```bash
export ANTHROPIC_API_KEY=...                       # Console key for the workspace the agents live in
export ANTHROPIC_WORKSPACE_ID=wrkspc_...           # only when the key is not scoped to a workspace (the SDK then needs the header)
export ANTHROPIC_WORKSPACE=default                 # only for Console deep links
export FACTORY_FORGE=github                        # default
export FORGE_REPO=<you>/<repo>                     # GITHUB_REPO also works
export FORGE_TOKEN=github_pat_...                  # GITHUB_TOKEN also works
export FACTORY_DEPLOY_WORKFLOW=preview.yml         # scope pipeline lookups to the template's workflow
export FACTORY_PREVIEW_ENVIRONMENT_PREFIX=preview/ # matches the template
export FACTORY_DEV_ENVIRONMENT=dev
export LINEAR_API_KEY=lin_api_...
export LINEAR_TEAM_ID=...
export FACTORY_GATE_SURFACE=tracker                # the default once LINEAR_API_KEY is set; `web` to decide only in the Console or CLI
export FACTORY_PAGE_PUBLISHER=local                # pages land in runs/<run_id>/pages/
```

Then:

```bash
cd orchestrator && .venv/bin/factory doctor
```

It prints the agent ids, the environment, `forge github https://github.com <you>/<repo> token=set`, `gate surface tracker`, a live `platform` line saying how many of the agents in `claude-lock.json` were found on the platform, and `ok`. Anything missing is listed by variable name, separately for a full run and for a design-only run.

## 6. Run

```bash
cd orchestrator && .venv/bin/factory run ../path/to/your/outcome.json
```

The run id is printed first. The process blocks at each gate; keep a second terminal for decisions. What happens, in order:

1. **Decompose.** One session; the Console link is printed. A Linear project `[factory] <title>` and a run issue appear; after the decomposition, one child issue per work item in Backlog.
2. **Gate 1.** `runs/<run_id>/pages/gate_1.html` is written (open it in a browser). The run issue gets a comment with the summary, links, the sha256 list, and the label `factory:gate-open`. Decide from either side, below.
3. **Specify and judge.** Work-item issues move to Todo. The spec-judge loop runs without you.
4. **Gate 2.** `gate_2.html` with the judge summary, the goal-coverage matrix, and a section per spec. Same decision mechanics, plus `approve-subset`.
5. **Implement, review, judge.** Issues move to In Progress. The implementer's session mounts your repository at `/workspace/repo` and pushes `factory/<run_id>/<spec_id>`; the preview workflow runs; the orchestrator clones the branch itself, runs the spec's test commands, waits for the preview deployment, runs the e2e entries, and runs the deterministic checks.
6. **Integrate.** The orchestrator retains the specification and verdicts in Factory’s artifact store, then the integrator opens a pull request at the exact reviewed implementation head. The description links to the evidence; integration adds no bookkeeping files to the application branch. The issue moves to In Review with the PR link in a comment. Gate 3 is the pull request itself.
7. **After you merge.** Run `factory watch <run_id>` (or click **Watch for merges** on the run page in the Console; it starts the same command and shows when it last checked). It polls the pull request every minute; when GitHub reports it merged, it records `cr.merged`, follows the `main` workflow run for the merge commit and the `dev` deployment, runs the spec's preview e2e entries against the dev URL from its own checkout, and ends the item at `deployed_dev` or opens a deploy-failure gate in the Inbox. Start it any time after the run, before or after you merge; it is safe to stop and restart and stops by itself when nothing is left to watch. With no `dev` deployment in the sample repository the gate is what you will see first: that is the honest result until the preview workflow's deploy step is real (spike 2a).

## 7. Deciding gates and answering questions

**From Linear.** On the issue with `factory:gate-open`: add exactly one label, `factory:approve`, `factory:request-changes`, `factory:reject`, or `factory:approve-subset`. For request-changes, add a comment first; it is handed to the agent verbatim as `feedback.md`. For approve-subset, include a line `approve: spec_a, spec_b`. The orchestrator polls every ten seconds, copies the sha256 list out of its own gate comment into the decision (so you approve exactly what the page showed), clears the labels, and moves on. A question from an agent is a `factory:question` comment; reply in its thread (or in a top-level comment that quotes the question id). Blocking questions pause the session until you do.

**From the Console.** `factory web` (in a third terminal, or before the run) serves http://127.0.0.1:8765/. The run appears on the board; the gate opens as a document with a sign-off rail; notes you leave under a work item or spec travel with Request changes. See [08-factory-console.md](08-factory-console.md). It writes to the same queue as the CLI.

**From the CLI.** `factory gates` lists open gates and questions with their artifact hashes; `factory decide <gate_id> approve|request_changes|reject|approve_subset --comment "..." [--spec spec_x]` and `factory answer <question_id> "text"`. The CLI binds the hashes automatically. Both surfaces are polled; the first decision wins.

**From the forge** (`FACTORY_GATE_SURFACE=forge`): the design PR holds the files for inline comments; the decision still comes from the CLI (or the tracker when both are configured).

## 8. What you see in the Anthropic Console

Each stage is a session titled `<run_id> <stage> ...`, with `metadata.run_id` so the sessions list can be filtered. The session view shows the `github_repository` resource, the uploaded `schemas/` and `tools/validate.py`, every tool call, the outcome-grader iterations for writing stages (`span.outcome_evaluation_end`), custom tool calls (`ask_human`, `open_change_request`) with the orchestrator's replies, and the list cost. `ant beta:sessions connect <session_id>` attaches a terminal to a live session. Every artifact's `produced_by.session_id` is the link back.

## 9. Swapping to Jira and self-hosted GitLab at work

Both seams are protocols with one implementation each to add or finish:

- **Forge.** Set `FACTORY_FORGE=gitlab`, `FORGE_URL=https://gitlab.example.internal`, `FORGE_TOKEN`, `FORGE_REPO=group/project`, and `FACTORY_VAULT_ID` (the `GITLAB_TOKEN` vault credential of [ADR-0003](adr/0003-gitlab-integration.md), because there is no native GitLab session resource). `GitLabForge` in `orchestrator/factory/forge/gitlab.py` already implements the `Forge` protocol; its pipeline and environment reads are unverified, which is roadmap spike 2b. Swap `github.com` for your host in `environments/factory-cloud.yaml`.
- **Tracker.** `orchestrator/factory/surfaces/jira.py` is a stub with every `Tracker` method and the Jira Cloud REST v3 call each one maps to. Implement it, add a `make_tracker_queue` branch keyed on `JIRA_URL`, and `TrackerQueue` does the rest: the gate comment, labels, decision parsing, and work-item mirroring are tracker-neutral. Jira has no threaded comments, so answers to questions are top-level comments that quote the question id (already supported).
- **Everything else** (gates, hashes, stages, checks, pages, event log) is unchanged. The CLI's file queue remains available alongside either tracker.

## 10. Known gaps before this is routine

- `request_changes` on a deploy-failure gate moves the item back to `implementing` and writes `<spec_id>-deploy_feedback.md`, but nothing starts that follow-up implementer round yet (the run is already complete): start a new run with the feedback in the outcome for now.
- The watcher polls; a forge webhook endpoint to shorten the minute between a merge and its detection is not in this repository.
- `ClaudeArtifactPublisher` is a stub; pages are local files.
- Human comments at Gate 2 are broadcast to every spec's writer; per-spec routing is a small follow-up.
- Reviewer requests on a personal repository are ignored by GitHub (you cannot review your own PR); the forge tolerates the 422. Use a second account or a collaborator for a real Gate 3.
