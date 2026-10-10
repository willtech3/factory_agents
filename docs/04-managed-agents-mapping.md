# Mapping onto Claude Managed Agents

How each factory concept lands on a platform primitive, and where the orchestrator has to do work the platform does not.

| Factory concept | Platform primitive | Notes |
|---|---|---|
| Agent role (decomposer, judge, ...) | **Agent** object, versioned | Defined as files under `agents/`, applied with `ant apply`, IDs recorded in `claude-lock.json`. Sessions pin to a version; evals compare versions. |
| Stage execution | **Session** | One session per stage per work item. `metadata` carries `run_id`, `stage`, `work_item_id` so the Console can be filtered by run. |
| Sandbox and tool execution | **Environment** (`cloud`, `limited` networking) | One environment, `environments/factory-cloud.yaml`. Package managers on; only the forge host reachable (`github.com`, `api.github.com`; the GitLab host for that forge); web tools disabled on every agent. |
| "Keep working until the rubric is met" | **Outcome** (`user.define_outcome` + rubric, `max_iterations`) | Writing sessions (decomposer, spec writer, implementer) are kicked off as outcomes with the matching rubric. The platform grader is the cheap inner loop; the factory's judges are the gate inputs. |
| Independent judgement | Separate **session** with a different **agent** | The platform grader's reasoning is opaque, so cross-artifact judgement (spec coherence, merge readiness) runs as its own agent whose verdict we can read, store, and evaluate. |
| In-stage fan-out | **Multiagent** coordinator roster (`self`, researcher) | One level deep by platform rule. Threads share the container filesystem; coordination is through files. |
| Human question mid-stage | **Custom tool** `ask_human` → `agent.custom_tool_use` → session idles → `user.custom_tool_result` | The orchestrator holds the SSE stream, posts the question to humans, and answers when a reply arrives. Nothing listens publicly. |
| Change request creation | **Custom tool** `open_change_request` executed host-side | The forge token never enters the sandbox; one code path for GitHub PRs and GitLab MRs ([ADR-0008](adr/0008-pluggable-forge-github-default.md)). |
| Repository context (GitHub, default) | **`github_repository` session resource** (`url`, `mount_path: /workspace/repo`, `authorization_token`, `checkout: {type: branch, name}`) | Cloned through Anthropic's git proxy, which injects the token for clone and push; repositories are cached across sessions; `.claude/skills` in the repo are loaded. The orchestrator passes the same `FORGE_TOKEN` it uses for the REST API. No vault needed. |
| Repository context (GitLab) | **Vault** `environment_variable` credential (`GITLAB_TOKEN`, header-only, host-scoped) + `bash` clone | There is no `gitlab_repository` resource; the token is substituted at egress and the sandbox sees a placeholder. See [ADR-0003](adr/0003-gitlab-integration.md) for the verification spike. |
| Human gate surface | Nothing on the platform side; `surfaces/` (tracker, pages) | Gates are orchestrator state; Linear issues and local HTML pages are where humans read and decide ([ADR-0007](adr/0007-pluggable-gate-surfaces.md)). |
| Repo-specific agent guidance | **Skills** from the repository's `.claude/skills/` | Loaded at session start; build commands, conventions, test layout belong here, not in agent prompts. Audit before mounting repos with external contributors. |
| Platform contract | **Skill** `platform-contract` in the repository's `.claude/skills/` | Owned by the platform team; `skills/platform-contract/SKILL.md` here is a placeholder with the expected headings. Read by decomposer, spec writer, spec judge, implementer, reviewer. |
| Preview environment (end-to-end surface) | Nothing on the platform side; the user's CI (GitHub Actions, `templates/github-actions-preview.yml`; or GitLab CI) deploys every branch | The orchestrator reads the URL from the forge's deployment record (GitHub Deployments API `environment_url`; GitLab environment `external_url`) and uploads `preview.json` to implementer and reviewer sessions. The preview domain must be in the environment's `allowed_hosts` for the sandbox to reach it. |
| Observed deploy to dev (Stage 5) | Nothing on the platform side; orchestrator polling plus a forge CI webhook | `stages/deploy_observe.py`; events `deploy.*`, `e2e.dev_*`; `DEPLOY_FAILURE` gate. No session runs. |
| Per-run spend cap | **Session budget** (`budget.max_list_cost`) | Hard, platform-enforced, pre-request gate. A paused session reports `budget_reached`; the orchestrator escalates. |
| Progress and audit | **Events** stream + `events.list` | Stream-first, consolidated with history on reconnect, deduped by event id. `span.outcome_evaluation_end` gives grader results; `span.model_request_end` gives per-request usage. |
| State-change notifications | **Webhooks** (`session.status_idled`, `session.outcome_evaluation_ended`, ...) | Alternative to holding streams for long-parked sessions. Thin payloads; fetch the resource. Not a durable log: reconcile by listing. |
| Trace inspection | **Console session viewer** | Every artifact's `produced_by.session_id` links here. |
| Cross-run learning (future) | **Memory store** (read-only mounts) | A per-repository store of conventions and past decisions, curated by humans, read-only to agents. Not in v1: memory scaffolds hurt reliability in one 2026 study ([academic-2026.md §7](research/academic-2026.md)). |
| Pinning model or effort for one run | `agent_with_overrides` on session create | Used for canaries (a new model for the reviewer on one run) without versioning the agent. The override replaces `model` in full, so set `effort` inside it. |

## Session kickoff shapes

**Writing stages** (decomposer, spec writer, implementer): one `user.define_outcome` in `initial_events` with `description` (the task plus where inputs are mounted) and `rubric` (the stage rubric). No separate `user.message`.

**Judging stages** (spec judge, reviewer, merge judge, integrator): one `user.message`. These produce one artifact in one pass; an outcome loop would have them grade their own judgement.

**Uploads**: every session gets `schemas/*.schema.json` and `tools/validate.py` mounted under `/mnt/session/uploads/`, plus its stage inputs. Outputs are read back from `/mnt/session/outputs/` via `files.list(scope_id=session.id)`.

## Permission policy

Agent toolset at `auto` everywhere. The sandbox is disposable and holds no secrets, so the server's own risk evaluation is the right tier: it runs safe calls, denies high-risk ones (the agent gets an error and continues), and pauses on indeterminate ones. In unattended runs the orchestrator **denies** paused calls with a `deny_message` telling the agent to record the blocker and continue; a human can attach with `ant beta:sessions connect` to allow instead. No tool is `always_ask` because no in-sandbox action is irreversible outside it: the only irreversible actions (change-request creation, design-branch and run-record commits) are custom tools or direct calls the orchestrator executes.

## What the orchestrator does that the platform does not

1. **Cross-stage state.** Sessions are independent; the run's state machine, artifact store, and event log are ours.
2. **Human gates between sessions.** Outcomes and custom tools pause *within* a session. Parking a run for two days between stages is a session boundary, so gates are orchestrator state.
3. **Authoritative verification.** Running the spec's test commands in a clean checkout the agent did not touch, running `test_plan.e2e` against the preview URL read from the forge, and computing deterministic checks (including `manifest_valid`, `e2e_coverage`, `e2e_preview`). In production this is a CI job on the branch; locally it is `LocalVerifier`.
4. **Merging independent reviewer passes.** Two sessions, one findings file.
5. **Routing judge issues to the specs that own them**, so only failing specs are rewritten.
6. **The forge and the gate surfaces.** Change requests, the run record on the branch, the optional design CR, tracker issues and pages, comment harvesting into `feedback.md`, CR state webhooks.
7. **Evals.** Pinning agent versions, replaying frozen inputs, grading.
8. **Watching the platform's deploy.** Workflow-run and deployment reads on the forge (unverified shapes on both GitHub and GitLab), dev e2e runs, and the `DEPLOY_FAILURE` gate. The factory never deploys ([ADR-0006](adr/0006-observed-deployment-not-agent-driven.md)).

## Costs to expect (list price, rough)

Based on the Ledgerline example's provenance fields and the stage caps: a medium outcome with four work items runs roughly $6 for decomposition, $3 per spec, $5 per judge round, $7 to $15 per implementation round, $6 per reviewer pass, $3 per merge judgement, under $1 to integrate. Four specs with one review round each lands around $90 to $130 before human time. The run budget default of $150 is set from this; the first spikes should replace these numbers with measured ones.

## Platform limits that shape the design

- One level of multiagent delegation; at most 25 concurrent threads per session.
- `initial_events` accepts only `user.message` and `user.define_outcome`; one outcome at a time per session.
- `vault_ids` is create-only on sessions.
- Memory stores attach at create time only; max 8 per session.
- Web tools are not governed by environment networking; they are disabled per agent.
- Archiving agents and environments is permanent; the orchestrator never archives them, only sessions.
