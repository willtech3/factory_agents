# Roadmap: from design to a first real run

The design is complete enough to iterate on. What exists has not run against the live platform. The order below is chosen so that each step de-risks the next and produces measurable output.

## Spike 1: platform plumbing (1 to 2 days)
Goal: one `decomposer` session end to end on the Ledgerline example outcome against a real (fork of a) repository, following [07-personal-e2e-setup.md](07-personal-e2e-setup.md) with a personal GitHub repository and Linear workspace.
- `ant apply --dry-run agents environments`, review the plan, apply. Commit `claude-lock.json`.
- `ANTHROPIC_API_KEY` (and `ANTHROPIC_WORKSPACE_ID`) in `.env`; `factory doctor` reports the agents found on the platform. Start with a **design run** (`factory run --design-only`, or the Console's Start design run) on `examples/sample-app/outcome.json`: decomposer, spec writers, judge, two gates, no repository.
- Fine-grained GitHub PAT (or `gh auth token`), Linear API key and team id, `factory doctor`.
- Run `factory run <your outcome.json>`; confirm the `github_repository` resource mounts the repository at `/workspace/repo` and that `git push` from the sandbox works through the proxy.
- Confirm the Linear mapping live: project and issues appear, the gate comment carries the hashes, a label plus comment is read back as a decision, `feedback.md` renders. Fix GraphQL field names in `surfaces/tracker.py` as needed.
- (Work setup, later) Create the vault and the `GITLAB_TOKEN` environment-variable credential. **Verify** that git over HTTPS to your GitLab accepts `Authorization: Bearer <PAT>` through the egress substitution. If it does not, implement the `push_branch` host-side custom tool fallback in ADR-0003.
- Fix whatever the live SDK surface disagrees with in `sessions.py` (field names on events, `files.list` shape, `usage.list_cost`). Expect some.
- Measure: cost, wall time, outcome iterations, whether `decomposition.json` validated first time.

## Spike 2: the spec loop and Gate 2 (2 to 3 days)
- Run specify on the decomposition from spike 1. Watch the judge's round-1 verdict closely: it is the first real test of the rubric anchors.
- Read `gate_2.html`; decide from Linear with `approve-subset`; test the request-changes round-trip through tracker comments. Then try `FACTORY_GATE_SURFACE=forge` once: design branch, multi-file commit through the Git Data API, draft PR, inline comments harvested into `feedback.md`.
- Wire `evals/run_eval.py::produce` to the driver and grade the spike's outputs with `--from-artifacts` first.

## Spike 2a: GitHub Actions and Deployments (half a day)
Goal: confirm `forge/github.py`'s read-only calls against the personal repository with `templates/github-actions-preview.yml` installed: workflow run by branch and head sha, run by id, failed job log, deployments by `ref` and by `environment` with `environment_url` on the latest status. Then replace the template's placeholder deploy step with something real (a Fly or Render app is enough) so `e2e_preview` can pass.

## Spike 2b: the self-hosted GitLab pipeline and environment API (1 day, work setup)
Goal: confirm the read-only calls Stage 5 and the preview lookup depend on, against the real instance. `forge/gitlab.py`'s pipeline, job, environment, and deployment methods and `parse_pipeline_webhook` follow the GitLab REST docs and are marked unverified.
- With `FORGE_URL` pointed at the self-hosted host: list pipelines by `ref` and `sha`; fetch a pipeline, its jobs, and a failed job's trace; list environments by `name` and `search` and confirm `external_url` is populated by the platform's deploy jobs (`environment:url` in the CI template); list deployments by environment.
- Learn the preview environment naming convention and set `FACTORY_PREVIEW_ENVIRONMENT_PREFIX`; learn the fixed service-manifest path and set `FACTORY_SERVICE_MANIFEST_PATH`; confirm the dev environment name (`FACTORY_DEV_ENVIRONMENT`).
- Register a Pipeline Hook pointing at a throwaway endpoint and compare the payload with what `parse_pipeline_webhook` expects.
- Add the GitLab host plus the preview and dev domains to `environments/factory-cloud.yaml` `allowed_hosts` and confirm a sandbox can `curl` a preview URL.
- Implement `surfaces/jira.py` from its docstring mapping and run one gate through Jira.
- Replace `skills/platform-contract/SKILL.md` with the platform team's real skill and `schemas/service_manifest.schema.json` with their schema; re-run the tests.

## Spike 3: one implementation through review and change request (3 to 5 days)
- Implement one spec on the fork. Replace `LocalVerifier` with a CI job on the factory branch (same interface: commands, exit codes, diff).
- Watch the reviewer: are findings falsifiable, do refutation conditions make sense, how many are withdrawn? Tune `reviewer.md` from what you see, one rule per observed failure.
- Watch the preview loop: does `preview.json` become `ready` before the implementer finishes, do the `preview` e2e entries run in the sandbox, does `e2e_preview` agree with what the implementer reported?
- Open the pull request; confirm the PR links to Factory’s external run record and its head equals the verified and judged candidate. Have a real engineer review it and time them. Merge it with `factory watch <run_id>` running (or started from the Console's run page) and watch Stage 5 end at `DEPLOYED_DEV`; break the next one deliberately and confirm the `DEPLOY_FAILURE` gate carries a useful log excerpt.

## Milestone A: unattended end-to-end on factory-bench `lintr` (week 3)
- Build the smallest `factory-bench` repo (the Python CLI) with two outcomes and hidden suites.
- Run unattended with scripted gate decisions, 5 times. Report credible intervals, cost, interventions.
- Decide the first open eval questions in `06-evaluation.md` (reviewer passes 1 vs 2; outcome iterations 1 vs 4).

## Milestone B: first production outcome (week 5 to 6)
- A low-risk internal outcome, owner and engineer briefed, L3 telemetry on.
- Factory Console v0 exists (`factory web`: outcomes, board, inbox, Gate 1 and 2 review; [08-factory-console.md](08-factory-console.md)). Add what a production run needs: authentication, Gate 3 and Stage 5 views, agent versions.
- Slack notifications for gates and questions (deep links to the page and the tracker issue).
- The change-request watcher exists as a polling process (`factory watch`, started from the Console too). Add a forge webhook endpoint to shorten the poll, and the follow-up implementer round that a deploy-failure `request_changes` asks for (today the item sits at `implementing` with its feedback file).
- `ClaudeArtifactPublisher`: publish gate pages as claude.ai artifacts once there is a way to do it from the orchestrator.

## Milestone C: evals that gate changes (week 8)
- `outcomes-golden` to 12 items, `spec-defects` to 60, `review-mutations` to 40, with human labels and kappa.
- CI: L0 on every commit; L1 on changes to `agents/` or `rubrics/`.

## Deliberately deferred
- **Tiered or automatic approval** of any gate. Needs six months of gate telemetry first.
- **Memory stores** for cross-run learning. Evidence is mixed; start with a human-curated read-only conventions store per repo once there are enough runs to curate from.
- **Multiple target repositories per outcome.** Schema allows it; orchestrator handles the first only.
- **Stacked change requests** for dependent specs. Currently serial: a dependent spec waits for the upstream merge.
- **Self-hosted sandboxes.** Only if a repository cannot be cloned into Anthropic's cloud environment.
- **Promotion beyond dev, reverts, and any infrastructure change.** Human and platform-owned by design (ADR-0006); not a future feature.

## Known gaps in what is here

- `sessions.py`, `forge/github.py`, `forge/gitlab.py`, `surfaces/tracker.py`, and the stage modules are written against documented APIs and have not been executed live. Treat field names as hypotheses until spike 1.
- `run_eval.py::produce` is a stub.
- The Factory Console is v0: local, unauthenticated, Gates 1 and 2 only. Session progress is the event log, not a live stream.
- `LocalVerifier` runs test commands and e2e entries on the orchestrator host; production must move this to CI.
- Both forges' pipeline, job, environment, and deployment reads and the webhook parsers are unverified (spikes 2a and 2b). `preview_info` fails closed when they misbehave, which blocks merges until fixed.
- Stage 5 is driven by the polling change-request watcher (`factory watch`, `watcher.py`); the forge webhook endpoint that would shorten the poll is not in this repository, and a deploy-failure `request_changes` leaves the item at `implementing` with its feedback file without starting the follow-up round. The watcher's GitHub reads (`merge_commit_sha`, workflow runs, deployments) are as unverified live as the rest of `forge/github.py`.
- `surfaces/jira.py` is a stub; `ClaudeArtifactPublisher` writes local files.
- `skills/platform-contract/SKILL.md` and `schemas/service_manifest.schema.json` are placeholders; the platform team's versions replace them.
- `merge_findings` de-duplicates by file and the first three characters of the line number, which is crude; good enough until real findings show what collides.
- Human comments at Gate 2 are broadcast to every spec's writer; per-spec routing from inline CR positions or `spec_x:` prefixes in tracker comments is straightforward and should be done in spike 2.
