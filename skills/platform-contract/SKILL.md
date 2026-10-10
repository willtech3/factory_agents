---
name: platform-contract
description: The contract between an application repository and the deployment platform. This version describes the personal-GitHub setup the factory runs against today (docs/07); a platform team replaces it with theirs. Loaded by the decomposer, spec writer, spec judge, implementer, and reviewer so that every work item is deployable through the existing platform without any agent authoring infrastructure.
---

# Platform contract (personal GitHub setup)

This repository is built and verified by the software factory's orchestrator on a developer machine. There is no container platform, no cloud project and no dev environment behind it yet. What follows is exactly what the orchestrator does, so a work item that respects it is deployable as far as this platform goes.

## What the platform provides

- **Source control**: GitHub, this repository. The factory works on branches named `factory/<run_id>/<spec_id>` cut from `main` (or from a dependency's branch when that dependency is still an open pull request) and opens one pull request per work item.
- **Preview environment**: the orchestrator starts the service itself from `service_manifest.json` (`runtime.entrypoint`, with the manifest's `runtime.port` replaced by a free port that is also exported as `PORT`), waits up to 60 seconds for `health.path` to answer `health.expect_status` (default 200), and runs every `test_plan.e2e` entry whose target is `preview` with `$BASE_URL` set to that instance. No agent can reach that instance; the implementer runs the `local` entries against a server it starts itself.
- **CI**: `.github/workflows/preview.yml`, when present, runs on pushes to `factory/**` and `main` and records a placeholder deployment. The platform owns that file.
- **Dev deployment**: none. Nothing happens after a pull request merges.
- **Shell for verification**: spec commands and end-to-end entries run under the newest bash 4+ on the orchestrator host, also as `sh` for nested `sh -c` calls. macOS's own /bin/sh is bash 3.2, which mis-parses escaped quotes inside `"$( ... )"`; keep end-to-end checks in small Python scripts under tests/ rather than long inline shell with nested quoting.
- **Interpreter and packages for verification**: the orchestrator runs the spec's `test_plan.commands` and the service entrypoint from a clean clone with its own Python ({sys_version}) first on `PATH` and **no install step**. Importable there: fastapi, starlette, uvicorn, pydantic, pytest, httpx, anyio, jsonschema, typer, and the standard library. Anything else fails at import time in verification even if it works in the sandbox.

## What the application must define

- `service_manifest.json` at the repository root, valid against the platform's service manifest schema. Required: `service` (string), `runtime` with `kind` (a short name for the runtime family; `python` is right for a process started by a command on this platform), `version`, `entrypoint` (the command the platform runs, for example `uvicorn app.main:app --host 0.0.0.0 --port 8000`) and `port`; `health` with `path` (and optionally `expect_status`, `timeout_seconds`); `env` listing every environment variable the service reads (an empty list when there are none). `migrations` and `resources` are optional.
- A health endpoint at `health.path` that answers 200 as soon as the process is up.
- Tests runnable as the spec's `test_plan.commands` say, from the repository root, with the packages above and nothing installed: set pytest's `pythonpath` to the repository root in `pyproject.toml` so the application package imports.
- A `pyproject.toml` naming the runtime and test dependencies, for humans and for a future platform that does install them.

## What agents must never touch

- `.github/workflows/` (the platform's), IAM, networking, DNS, containers, cloud projects, secrets. If an item seems to need any of that, it is an open question for the owner, not a work item.
- Ports other than the manifest's `runtime.port`; hard-coded hosts or URLs for the preview.
- Dependencies outside the importable set above, unless the outcome explicitly provides for an install step.

## How the factory uses this skill

The decomposer checks every work item is deployable through the above and turns platform work into open questions. The spec writer fills `test_plan.commands` and `test_plan.e2e` (targets `local` and `preview`) against this contract. The spec judge rejects specs that assume a platform feature this contract does not provide. The implementer writes the manifest and the health endpoint in the first item that lands in a new repository. The reviewer checks the branch starts from the manifest entrypoint and answers the health path.
