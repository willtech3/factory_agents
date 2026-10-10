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

## Credentials

Only names declared by the reviewed recipe get write-only inputs. Values go to Anthropic credential vaults and are never persisted in the app record, agent prompt or browser storage. Each declaration has a separate vault scoped to allowed hosts and header/body substitution. A changed scope requires reconnection and old scopes are not attached to new sessions. Forge credentials are never attached to the test environment.

Opaque vault substitution works for supported outbound requests, not local signing or encryption. Generate synthetic JWT/encryption secrets inside the sandbox. Test integrations may require separately scoped provider credentials; a no-key baseline must say what it excludes. Never seed real conversations or production accounts.

## Evidence, budgets and recovery

The app page records every session and its reported cost. A pending session reserves its cap. Missing usage is charged conservatively against the cap, not displayed as free. Controller restart recovery interrupts orphaned remote work before a retry becomes available.

Readiness means the declared baseline passed, with any limitations shown. It does not mean all features or production authentication were tested. Inspect screenshots and play the recording. The reviewed source and plan fingerprint remain attached to the result. Publishing creates a draft setup PR; it does not merge it. Setup publication, Gate 1 scope, Gate 2 specs and final code merges remain separate decisions.

## Operator setup

Deploy `agents/app_onboarding.md` and register its ID in `claude-lock.json`. When adding only this agent, use a separate lock file with `ant apply` and copy that one confirmed registration into the main lock; review the plan so unrelated agent definitions are not reconciled accidentally. Existing Factory/GitHub credentials remain required for Console source access and draft PR creation.

`factory environment check` validates a committed recipe. `factory environment verify` runs a bounded hosted verification and writes evidence to a fresh output directory. It does not run app commands locally.
