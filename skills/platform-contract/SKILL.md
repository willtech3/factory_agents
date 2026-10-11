---
name: platform-contract
description: The contract between an application repository and the deployment platform. This version describes the personal-GitHub setup the factory runs against today (docs/07); a platform team replaces it with theirs. Loaded by the decomposer, spec writer, spec judge, implementer, and reviewer so that every work item is deployable through the existing platform without any agent authoring infrastructure.
---

# Platform contract (personal GitHub setup)

This repository is built and verified by the software factory's orchestrator on a developer machine. There is no container platform, no cloud project and no dev environment behind it yet. What follows is exactly what the orchestrator does, so a work item that respects it is deployable as far as this platform goes.

## What the platform provides

- **Source control**: GitHub, this repository. The factory works on branches named `factory/<run_id>/<spec_id>` cut from `main` (or from a dependency's branch when that dependency is still an open pull request) and opens one pull request per work item.
- **Preview environment**: the orchestrator starts the service itself from `service_manifest.json` (`runtime.entrypoint`, with the manifest's `runtime.port` replaced by a free port that is also exported as `PORT`), waits up to 60 seconds for `health.path` to answer `health.expect_status` (default 200), and runs every `test_plan.e2e` entry whose target is `preview` with `$BASE_URL` set to that instance. No agent can reach that instance: `preview.json` says `unreachable_from_sandbox` with no URL, and the implementer and the reviewers start their own instance from the same entrypoint and run the `preview` and `local` entries against it. The orchestrator's run is the one that counts.
- **CI**: `.github/workflows/preview.yml`, when present, runs on pushes to `factory/**` and `main` and records a placeholder deployment. The platform owns that file.
- **Dev deployment**: none. Nothing happens after a pull request merges.
- **Shell for verification**: spec commands and end-to-end entries run under the newest bash 4+ on the orchestrator host, also as `sh` for nested `sh -c` calls. macOS's own /bin/sh is bash 3.2, which mis-parses escaped quotes inside `"$( ... )"`; keep end-to-end checks in small Python scripts under tests/ rather than long inline shell with nested quoting.
- **Interpreter and packages for verification**: the orchestrator runs the spec's `test_plan.commands` and the service entrypoint from a clean clone on its own host, inside a fresh per-run virtual environment it builds from the repository: `runtime.setup` from the manifest when the manifest declares one (any install command, run from the repository root with that environment first on `PATH`), otherwise `pip install -e .[dev]` from `pyproject.toml` (the `dev`, `test` or `tests` extra, whichever the project declares; a pyproject with no build system has its declared dependency lists installed instead). Nothing from the orchestrator's own environment is on `PATH`. A setup that fails is a failed deterministic check the implementer must answer. Each command runs in its own process group and is killed when it ends, so a server a command backgrounds does not survive it; commands must not bind a fixed port or assume anything about the host.

## What the application must define

- `service_manifest.json` at the repository root, valid against the platform's service manifest schema. Required: `service` (string), `runtime` with `kind` (a short name for the runtime family; `python` is right for a process started by a command on this platform), `version`, `entrypoint` (the command the platform runs, for example `uvicorn app.main:app --host 0.0.0.0 --port 8000`) and `port`; `health` with `path` (and optionally `expect_status`, `timeout_seconds`); `env` listing every environment variable the service reads (an empty list when there are none). `migrations` and `resources` are optional.
- A health endpoint at `health.path` that answers 200 as soon as the process is up.
- Tests runnable as the spec's `test_plan.commands` say, from the repository root, after the install step above and nothing else: set pytest's `pythonpath` to the repository root in `pyproject.toml` so the application package imports whether or not the project itself was installed.
- A `pyproject.toml` naming the runtime dependencies and a `dev` extra with the test dependencies; the orchestrator installs exactly those. A repository that needs more than pip (another language, a system package) declares a `runtime.setup` command in the manifest instead.

## What agents must never touch

- `.github/workflows/` (the platform's), IAM, networking, DNS, containers, cloud projects, secrets. If an item seems to need any of that, it is an open question for the owner, not a work item.
- Ports other than the manifest's `runtime.port`; hard-coded hosts or URLs for the preview.
- Dependencies the repository does not declare (in `pyproject.toml` or `runtime.setup`); the verification environment has nothing else.

## How the factory uses this skill

The decomposer checks every work item is deployable through the above and turns platform work into open questions. The spec writer fills `test_plan.commands` and `test_plan.e2e` (targets `local` and `preview`) against this contract. The spec judge rejects specs that assume a platform feature this contract does not provide. The implementer writes the manifest and the health endpoint in the first item that lands in a new repository. The reviewer checks the branch starts from the manifest entrypoint and answers the health path.

### UI verification contract

For a change observable in the UI, set the criterion's `surface` to `ui` and include a `test_plan.e2e` entry with target `preview` and a `visual` object containing persona, viewport `{width,height}`, ordered steps, and environment `{name,data,authentication,required_env}`. Data is `synthetic` or `isolated-test`; authentication is `none-required`, `test-identity`, or `production-equivalent`. Environment values are never secrets; `required_env` lists names only. Use an existing repository-approved browser runner command, or `factory:browser` for a connected browser worker. The latter is a controller handoff, not a command to execute inside your session. A healthy HTTP endpoint or successful code tests do not satisfy visual acceptance. Missing browser/service/auth access is a blocker, not a reason to substitute a mock and claim end-to-end success. The controller supplies its exact commit and URL and requires observations plus screenshots. Request a short enhancement demonstration when recording is supported. Respect repository-specific browser tool requirements.
