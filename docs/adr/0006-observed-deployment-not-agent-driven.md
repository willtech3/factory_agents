# ADR-0006: Deployment is observed, not agent-driven

**Status:** accepted, with a verification spike · **Date:** 2026-10-09

## Context
The original design ended at merge, and review judged code rather than the running system. Two fixes were considered: a deployer agent with infrastructure authority, or using the platform that already exists (GCP project, WIF identity and service account for GitLab CI, CI templates, per-branch preview environments, a `dev` target) and having the orchestrator watch it. The GitLab instance is self-hosted and was not reachable while this was designed.

Evidence: agents declare victory without end-to-end verification ("Effective harnesses for long-running agents", practitioners.md §3); solve rates drop sharply when the cycle includes running the result (SWE-Cycle, academic-2026.md §5); boundary-crossing actions should be routed to humans or a dedicated reviewer rather than taken by the working agent (OpenAI auto-review, practitioners.md §4; 12-Factor Agents Factor 7, practitioners.md §1b); the acceptance oracle must sit outside the agent (academic-2026.md §2, §4).

## Decision
1. **No deployer agent and no agent-authored infrastructure.** The platform contract (a skill the platform team owns; `skills/platform-contract/SKILL.md` is a placeholder) names what the app must define, including a service manifest at a fixed path, and what agents must never touch (IAM, networking, the platform repository, CI templates, promotion beyond dev).
2. **The preview environment is the end-to-end surface.** Specs mark criteria `user_visible` and declare `test_plan.e2e`; implementer and reviewer run them against the preview URL; the orchestrator re-runs them and the result is deterministic (`e2e_coverage`, `manifest_valid`, `e2e_preview`; rubric S11, S12, I9).
3. **Stage 5 observes the dev deploy.** After `cr.merged` the orchestrator watches the platform's pipeline for the merge commit (polling the forge), records `deploy.*` and `e2e.dev_*` events, runs the spec's e2e entries plus an optional smoke suite against the dev URL, and on failure opens a `DEPLOY_FAILURE` gate with the log excerpt that can route the item back to `IMPLEMENTING`. Work items end at `DEPLOYED_DEV`; promotion beyond dev is human.
4. **The environment URL always comes from the forge's deployment record** (GitHub Deployments API `environment_url`, GitLab environment `external_url`), never from an agent's report.

## Why not the alternatives
- **Deployer agent.** It would need credentials the sandbox must not hold (ADR-0003's principle), its actions fall outside every deterministic check, and it would control the evidence of its own success. It also duplicates a pipeline that already exists.
- **End at merge, as before.** Leaves the two gaps open; the merged-but-broken case is exactly where SWE-Cycle's drop hides.
- **Have the orchestrator trigger the pipeline.** The merge already triggers it; a second trigger adds an action the human did not approve (ADR-0005).

## Open verification (roadmap spike)
Against the self-hosted instance: pipelines-by-sha, jobs and trace, environments with `external_url`, deployments-by-environment, and the Pipeline Hook payload. Confirm the preview environment naming convention and that the deploy jobs register `environment:url`. Confirm the sandbox can reach the preview domain once it is in `allowed_hosts`.

## Consequences
- The factory depends on the platform's pipeline being reliable; a flaky pipeline shows up as `DEPLOY_FAILURE` gates, and the `approve` option (watch the next pipeline once) exists for that.
- A dev failure after merge produces a follow-up branch and change request, not a revert; reverts remain a human decision on the forge.
- `test_results.json` handed to the merge judge and integrator now carries `commands`, `e2e`, and `preview` sections. Since #25 the integrator no longer receives it; the controller renders the change request's testing section from it (`pr_description.py`).
- `success_metrics[]` gained optional `measured_at` and `measured_value` so a human can record the outcome later; no agent sets them.
