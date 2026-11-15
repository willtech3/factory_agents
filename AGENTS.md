# Working in factory_agents

Start in the primary checkout on `main`, pulled from `origin/main`. Use one ordinary branch per PR. You don't need a worktree. PRs are squash-merged, with a plain-sentence title; the merge adds the PR number.

Read [README.md](README.md) first: the quick start has the setup and the exact commands for CI's three gates (tests, ruff, and the eval receipts gate). [docs/repo-map.md](docs/repo-map.md) says what each module does and what is live, stubbed or unused. Open GitHub issues hold the known gaps and deferred decisions.

## Environment

- **pip, not uv.** Install into `orchestrator/.venv` with `pip install -e ".[dev]"`, as CI does. `uv.lock` is gitignored: nothing reads it, so a committed copy would drift from what CI installs.
- **Paid sessions.** Tests and the Console need no API key. With a key in `.env`, `factory run` and `factory resume` start paid agent sessions, so run them only when the owner asks.
- **Local-only test failure.** `test_bun_verification` fails when `bun` resolves to a broken shim (mise prints "bun is not provided by the active node"). That is the machine, not the code.
- **Browser CI job.** It is pinned to `ubuntu-24.04`. Playwright is pinned to the hosted sandbox's version (`browser` extra in `orchestrator/pyproject.toml`), and that version cannot install Chromium on newer images, so move the two together.

## Invariants that catch changes

- **Artifacts are immutable.** `RunStore.put_artifact` refuses different bytes under an existing name. Every attempt, round or retry writes its own name. `replace=True` is only for the controller's own pointer files.
- **Resume replays.** `factory resume` re-walks the pipeline, and finished steps return their recorded results. A stage transition emitted on replay must be legal from wherever the item already is: use `enter_item_stage`, and check `WORK_ITEM_TRANSITIONS` in `state.py`.
- **Merge-judge policy.** Any merge-judge verdict other than `ready` escalates the spec to the owner's build-recovery gate ([docs/01-pipeline.md](docs/01-pipeline.md), Failure handling). Sending `another_round` back to the implementer automatically is a policy change; propose it as one.
- **Prompt, schema and rubric contracts.** An agent prompt (`agents/*.md`) and the schema it writes against (`schemas/`) change together: a schema's `additionalProperties: false` refuses any field the prompt asks for but the schema lacks. A changed prompt or rubric needs an eval receipt ([evals/README.md](evals/README.md)).
