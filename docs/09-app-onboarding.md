# App onboarding and hosted test environments

Open **Apps → Add an app** in Factory Console. Choose an accessible GitHub repository, or describe a new app and its intended private repository. The default onboarding cap is $10. Discovery starts after the form is submitted; verification and publication are separate actions.

The App Onboarding managed agent reads pinned committed source, dependency declarations, existing CI/tests and setup recipes. It proposes a schema-valid plan and asks only unresolved questions. For a new app it proposes a minimal working scaffold. Discovery has no network access, no credentials, and cannot execute the application. An agent proposal is not evidence that the app works.

## The reviewed contract

`onboarding_plan.schema.json` describes journeys, planned capabilities, questions, proposed files and limitations. Existing-app changes are confined to onboarding recipe paths; application, CI and security-policy changes require separate work. A recipe can be reused without a new commit or PR. Answering questions invalidates the reviewed plan fingerprint and requires another discovery pass.

`test_environment.schema.json` defines a Linux runtime, prepare/start/seed/verify/stop commands, service health checks, network hosts, declared secret references and one of:

- Browser journeys, required viewports, screenshots and an optional short demo.
- Functional API, worker, CLI or library checks with named observations.

Specialized native/mobile/GPU runtimes must report unsupported capabilities. A browser test does not stand in for an unavailable native test. Onboarding cannot promise universal one-click setup: some applications need a test identity, an isolated service or a repository-approved adapter.

## Where execution happens

Factory stores the portable recipe in the repository. It creates or reuses a fingerprinted environment definition in Anthropic Console **Environments**, then creates a fresh hosted session for each verification. A changed or manually altered environment definition is not overwritten. App processes, databases, browser automation and test data stay in that sandbox. The Console may run on the user's computer; it only stages source archives and downloads evidence.

The controller uploads an immutable source archive and its own lifecycle harness. The agent runs that harness without editing it or the app. The harness checks readiness, seeds synthetic data, runs checks, captures results and stops services. The controller binds acceptance to source/profile hashes, expected phases and required evidence. Completed sessions are archived. There is no promise of an externally reachable live preview URL; screenshots and recordings are downloaded to the Console.

The original `local` and `platform` verification modes predate this path. Do not use them when the requirement is hosted-only application execution. A passing onboarding baseline alone does not approve future changes or satisfy their acceptance criteria: each candidate still needs current command and visual evidence. Post-merge dev deployment verification is a separate capability: managed runs fail closed until a hosted dev-test adapter is configured, and never fall back to executing those checks on the Console host.

After the setup is committed to the target branch, **Request a change** binds the new run to that app's verified recipe and explicit test vaults. The backend checks the repository and profile fingerprint; browser form fields cannot substitute another repository or unreviewed recipe. Managed verification runs the candidate's test commands and both `local` and `preview` e2e entries inside a fresh cloud sandbox. Results are bound to the exact source archive, commit, recipe and specification. The baseline and changed-behavior evidence are separate. UI criteria require real per-step screenshots, matching viewports and a controller-issued visual request; `factory:browser` is rejected in hosted mode.

Automated acceptance criteria are proven by the controller's test reports or the actual e2e journey assigned by the approved spec, never by the implementer's claim. pytest commands write JUnit through `PYTEST_ADDOPTS`, and the harness also collects pytest node IDs. A direct `bun test ...` test-plan command runs with the harness's own `--reporter=junit --reporter-outfile` inserted after `bun test`. Its IDs are `path/x.test.ts::outer describe > inner describe > test name`, read from the report's nested suites. Skipped, todo and name-filtered tests count as not passed. A report whose case count disagrees with its declared total is rejected. Bun has no verified collect-only mode, so the report itself is the inventory. Package scripts (`bun run test`), pipelines, environment prefixes, wrappers and commands that choose their own reporter give no Bun evidence, and each such row records the reason. In managed runs, an automated criterion without a matching passing test in a report fails `ac_test_mapping`. The report shape was checked against Bun 1.3.14 and verified in a hosted Evergreen run with all 12 existing model tests passing. An e2e journey itself can be mapped as `e2e:0` (the zero-based entry index in the approved spec). It must name the criterion in `covers_ac` and have a matching passing controller result; a visual entry also needs its exact current browser evidence. A baseline result or a different entry cannot substitute for it.

The managed verifier records its session, phases, sanitized exit results, collected pytest node IDs, per-command test outcomes and browser media. Raw command logs remain in the sandbox to avoid exporting credentials. This limits automatic failure diagnosis; the session link is available for deeper inspection. Controller-side dependency audits and SAST are explicitly skipped in managed mode until they have hosted adapters. No reachable preview deployment is claimed.

## Credentials

Only names declared by the reviewed recipe get write-only inputs. Values go to Anthropic credential vaults and are never persisted in the app record, agent prompt or browser storage. Each declaration has a separate vault scoped to allowed hosts and header/body substitution. A changed scope requires reconnection and old scopes are not attached to new sessions. Forge credentials are never attached to the test environment.

Opaque vault substitution works for supported outbound requests, not local signing or encryption. Generate synthetic JWT/encryption secrets inside the sandbox. Test integrations may require separately scoped provider credentials; a no-key baseline must say what it excludes. Never seed real conversations or production accounts.

## Evidence, budgets and recovery

The app page records every session and its reported cost. A pending session reserves its cap. Missing usage is charged conservatively against the cap, not displayed as free. Controller restart recovery interrupts orphaned remote work before a retry becomes available.

**Improve or repair the setup** sends feedback, the previous proposal and failure evidence back to the onboarding agent. It invalidates the previous plan fingerprint and verification, so a repaired proposal must be verified again before publication. Failed phases are visible without downloading the technical report.

Readiness means the declared baseline passed, with any limitations shown. It does not mean all features or production authentication were tested. Inspect screenshots and play the recording. The reviewed source and plan fingerprint remain attached to the result. Publishing creates a draft setup PR; it does not merge it. Setup publication, Gate 1 scope, Gate 2 specs and final code merges remain separate decisions.

## Operator setup

Deploy `agents/app_onboarding.md` and register its ID in `claude-lock.json`. When adding only this agent, use a separate lock file with `ant apply` and copy that one confirmed registration into the main lock; review the plan so unrelated agent definitions are not reconciled accidentally. Existing Factory/GitHub credentials remain required for Console source access and draft PR creation.

`factory environment check` validates a committed recipe. `factory environment verify` runs a bounded hosted verification and writes evidence to a fresh output directory. It does not run app commands locally.
