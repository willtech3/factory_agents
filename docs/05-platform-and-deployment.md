# Platform and deployment: the preview environment, the observed dev deploy, and why agents never author infrastructure

The first version of this design ended at merge. Two gaps followed from that: nothing said what the platform expects of a change, so a spec could be "complete" and undeployable; and nothing required anyone to exercise the running system, so review judged code, not behaviour. This page closes both without adding a deployer agent. The platform already exists (at work: a GCP project, a Workload Identity Federation (WIF) identity and service account that GitLab CI runs as, shared CI templates, per-branch preview environments, a `dev` target; for the personal end-to-end test: a GitHub Actions workflow from `templates/github-actions-preview.yml` with a placeholder deploy step). The factory uses it and watches it; it does not build it. Which forge's CI and deployment records it reads is configuration ([ADR-0008](adr/0008-pluggable-forge-github-default.md)).

## 1. The platform contract is a skill, and here it is a placeholder

[`skills/platform-contract/SKILL.md`](../skills/platform-contract/SKILL.md) is a placeholder with frontmatter and the headings the factory expects: what the platform provides, what the application must define (including a **service manifest at a fixed path**), what agents must never touch, and how the factory uses the answers. **The platform team's existing skill replaces the file.** Nothing in this repository authors contract content.

[`schemas/service_manifest.schema.json`](../schemas/service_manifest.schema.json) is the same kind of placeholder: a minimal, obviously extensible schema (service name, runtime, health endpoint, env vars with a secret flag, migrations command, resources) that the real schema replaces under the same filename. The orchestrator's `manifest_valid` check validates the manifest in the application repository against whichever schema is at that path; [`examples/ledgerline/service_manifest.json`](../examples/ledgerline/service_manifest.json) shows the shape.

Five agents read the skill, one or two lines each: the decomposer so every work item is independently deployable to a preview; the spec writer so manifest changes are declared and reserved work is never specified; the spec judge for rubric criteria S11 and S12; the implementer and reviewer so neither touches what the contract reserves. The skill is loaded from the target repository's `.claude/skills/` at session start like any other skill (see [04-managed-agents-mapping.md](04-managed-agents-mapping.md)); the copy here is the template.

## 2. The preview environment is the end-to-end surface

Every factory branch `factory/<run_id>/<spec_id>` gets what every branch gets: the platform's CI deploys it to a per-branch preview environment and records the URL where the forge keeps it (a GitHub Deployment status's `environment_url`, or a GitLab environment's `external_url`). The factory adds no CI of its own; `templates/github-actions-preview.yml` is the minimal workflow shape for GitHub, with the deploy step left to the platform.

Three things change because that surface exists:

- **Specs declare end-to-end checks.** Each acceptance criterion carries `user_visible` (can a user, operator, or external system observe this through the deployed service?), and `test_plan.e2e` lists commands that run against a deployment (`target: preview`) or a locally started process (`target: local`), with `$BASE_URL` substituted. The deterministic `e2e_coverage` check fails any spec whose user-visible criterion has no e2e entry; rubric S12 has the judge check the flags are honest, and S11 that the spec is deployable through the contract.
- **Implementer and reviewer run them.** Both sessions receive `preview.json` (branch, pipeline status, URL) read by the orchestrator from the forge, never from an agent. The implementer runs the entries and records them; the reviewer runs them again and probes the deployed instance the way it probes the code. A `user_visible` criterion the reviewer could not exercise against the preview is `not_assessed`, never `met`. With `FACTORY_PREVIEW=local` there is no deployment: `preview.json` says `unreachable_from_sandbox` with no URL, the orchestrator starts the service from the manifest entrypoint on its own host for its run, and implementer and reviewers start their own instance from the same entrypoint and mark the criteria `met` or `not_met` on it (`not_assessed` only when the entrypoint will not start).
- **The orchestrator's verification is the one that counts.** After each implementer round, authoritative verification checks out the head, builds a per-run virtual environment from the repository (`runtime.setup` in the manifest, else `pyproject.toml`; a failure is the deterministic check `verification_setup`), runs the spec's test commands in it, each in its own process group, then `LocalVerifier.run_e2e` against the preview URL (or the orchestrator's own manifest-entrypoint instance in local mode), and `run_all_for_merge` includes `e2e_coverage`, `manifest_valid`, and `e2e_preview`. Not run counts as failed. Rubric I9 makes it a deterministic item the merge judge cannot override. The merge judge records its own per-criterion verdict in `merge_readiness.criteria`, and the change request renders that table beside the orchestrator's e2e rows.

The evidence is the same on both sides of the practitioner and academic divide. Anthropic's long-running-agent harness found agents "declare victory too early and mark features done without verifying them end to end" ([practitioners.md §3](research/practitioners.md)). SWE-Cycle measured the gap: solve rates "drop sharply in the full cycle" once environment, implementation, and tests are all required, which is why it introduces an execution-capable grader ([academic-2026.md §5](research/academic-2026.md)). A merged change request with green unit tests is where that drop hides.

For the sandbox to reach the preview, the environment's `allowed_hosts` must include the preview domain as well as the forge host; see [environments/README.md](../environments/README.md).

## 3. Stage 5: the observed deploy to dev

After Gate 3, the forge reports `cr.merged`. The platform's pipeline on the default branch deploys to `dev` exactly as it would for a human's merge. The orchestrator ([`stages/deploy_observe.py`](../orchestrator/factory/stages/deploy_observe.py)):

1. moves the work item `MERGED -> DEPLOYING_DEV` and records `deploy.started` with the pipeline id (a forge CI webhook can report it first; polling through the `Forge` protocol is the source of truth, because webhooks are not a durable log);
2. waits for the pipeline to reach a terminal status and records `deploy.succeeded` or `deploy.failed` with a log excerpt from the failed job;
3. on success reads the dev URL from the `dev` deployment or environment record, runs the spec's `test_plan.e2e` entries (the `preview` ones; "preview" means "a deployed instance") plus the operator's optional smoke suite (`FACTORY_SMOKE_COMMANDS`), and records `e2e.dev_passed` or `e2e.dev_failed`;
4. on success moves the item to `DEPLOYED_DEV`, the terminal state the Console shows as verified in dev;
5. on any failure opens a `DEPLOY_FAILURE` gate carrying the log excerpt. The human's options: `request_changes` routes the item back to `IMPLEMENTING` on a fresh branch with the comments as the first finding, `approve` says the failure was the platform's and the next pipeline should be watched (once), `reject` closes the item.

Promotion beyond `dev` is a human act outside the factory. The outcome's `success_metrics[]` now accept `measured_at` and `measured_value` so a human can close the loop from code to business outcome later; no agent sets them.

## 4. Forge assumptions

**GitHub (default).** `forge/github.py` reads Actions workflow runs (optionally scoped to `FACTORY_DEPLOY_WORKFLOW`) and the Deployments API. The preview URL for a branch is the newest deployment for that `ref` with an `environment_url` on its latest status; the dev URL is the latest deployment to the `dev` environment. These reads are **marked unverified**: field names follow the REST documentation (2022-11-28) and are exercised only against a fake transport in tests. The first run in [07-personal-e2e-setup.md](07-personal-e2e-setup.md) verifies them.

**Self-hosted GitLab (work).** The instance is not reachable from where this design was written, so:

- Nothing here calls it. `FORGE_URL` (alias `GITLAB_URL`) is the only place the host is configured; the GitLab host in `environments/factory-cloud.yaml` is a commented placeholder.
- The pipeline, jobs, environments, and deployments methods in `forge/gitlab.py` follow the GitLab REST documentation (GitLab 16/17 shapes) and are **marked unverified**. Field names, the environment naming convention for previews (`preview/<branch>` by default, `FACTORY_PREVIEW_ENVIRONMENT_PREFIX` to change it), and whether the deploy job registers `environment:url` are hypotheses until the roadmap spike runs them against the real instance.
- The pipeline webhook handler normalises the Pipeline Hook payload (and GitHub's `workflow_run` event); the HTTP endpoint belongs to the Factory Console, not this repository.
- Identity is the platform's: GitLab CI deploys with its WIF binding and service account. The factory holds no GCP credential and needs none.

## 5. Why agents do not author infrastructure

Nothing in the factory creates a service account, edits a firewall rule, changes a CI template, or touches the platform repository, and no agent deploys anything. Four reasons, each with its source:

1. **Boundary-crossing actions belong to humans or to the platform's own automation.** 12-Factor Agents models human contact as a tool call routed by a deterministic outer loop (Factor 7, [practitioners.md §1b](research/practitioners.md)); OpenAI's auto-review work routes exactly the sandbox-boundary actions to a reviewer and stops the trajectory on repeated denials ([practitioners.md §4](research/practitioners.md)). Infrastructure is the boundary. Here the platform team already made those decisions in templates, so the factory inherits them instead of re-deciding per run.
2. **Infrastructure drift is invisible to the deterministic checks.** `diff_scope` and `protected_tests` protect the application repository. A change to IAM or networking lives outside it and outside any test the spec can name; it would be approved by nobody and verified by nothing.
3. **An agent that can deploy will deploy to prove it is done.** The victory-declaration failure ([practitioners.md §3](research/practitioners.md)) gets worse when the agent controls the evidence. Reading the URL from the forge's deployment record, and having the orchestrator run the checks, keeps the oracle outside the agent (the harness-side acceptance oracle argument, [academic-2026.md §2 and §4](research/academic-2026.md)).
4. **Approvals bind to what was shown.** Gate decisions bind to artifact hashes (ADR-0005). A deploy triggered by an agent after approval would be a second action the human never saw. Observing the platform's own pipeline for the merged sha keeps the approved artifact and the deployed artifact the same thing.

The trade-off is accepted in [ADR-0006](adr/0006-observed-deployment-not-agent-driven.md): the factory cannot fix a broken deploy itself and depends on the platform's pipeline being reliable. When it is not, a human sees a `DEPLOY_FAILURE` gate with the log, which is the right person seeing the right thing.
