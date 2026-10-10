# factory_agents

A design, and the beginnings of an implementation, for a **software factory**: a pipeline of Claude Managed Agents with humans in the loop that takes a business outcome and ends with reviewed merge requests in GitLab.

```
outcome ─► decompose ─► ▣ human ─► specify ⇄ judge ─► ▣ human ─► implement ⇄ adversarial review ─► merge judge ─► MR ─► ▣ human review + merge ─► observed deploy to dev + e2e
```

Three human gates. Judges in separate sessions from authors. Every finding falsifiable and execution-grounded. Deterministic checks between every stage that no agent can override. Coordination through schema-validated artifacts, never chat. End-to-end checks against the branch's preview environment before merge and against dev after it; no agent deploys or touches infrastructure.

## Read this first

| If you want... | Read |
|---|---|
| The whole shape in one page | [docs/00-overview.md](docs/00-overview.md) |
| How a run moves, stage by stage, with budgets and failure handling | [docs/01-pipeline.md](docs/01-pipeline.md) |
| What each agent does and why the boundaries sit where they do | [docs/02-agents.md](docs/02-agents.md), definitions in [agents/](agents/) |
| What a human sees and does, walked through on an example | [docs/03-human-in-the-loop-ux.md](docs/03-human-in-the-loop-ux.md) |
| How this maps onto Managed Agents primitives | [docs/04-managed-agents-mapping.md](docs/04-managed-agents-mapping.md) |
| The platform boundary: contract placeholder, preview environments, the observed deploy to dev | [docs/05-platform-and-deployment.md](docs/05-platform-and-deployment.md) |
| How to evaluate it: levels, datasets, deterministic and stochastic methods | [docs/06-evaluation.md](docs/06-evaluation.md), details in [evals/](evals/README.md) |
| The principles and the evidence behind each | [docs/design-principles.md](docs/design-principles.md) |
| What practitioners and 2026 research actually say | [docs/research/practitioners.md](docs/research/practitioners.md), [docs/research/academic-2026.md](docs/research/academic-2026.md) |
| Decisions and their trade-offs | [docs/adr/](docs/adr/) |
| What to build first | [docs/roadmap.md](docs/roadmap.md) |

## What is here and how real it is

- **`schemas/`** JSON Schema for every artifact handed between stages. Validated by tests; the example run in `examples/ledgerline/` conforms.
- **`agents/`** One `ant apply` file per agent (YAML frontmatter is the Managed Agents config, Markdown body is the system prompt). Eight agents, each under 80 lines of instructions.
- **`rubrics/`** Per-criterion rubrics with 0 to 3 anchors, used by the platform's outcome grader inside sessions and by the factory's judges between them. Includes deployability (S11), end-to-end coverage (S12), and e2e-on-preview (I9).
- **`skills/`** `platform-contract`, a placeholder for the platform team's existing skill: frontmatter and the headings the factory expects, no contract content. The matching `schemas/service_manifest.schema.json` is a placeholder too.
- **`orchestrator/`** Python. An event-sourced run state machine, hash-bound human gates, deterministic checks, a Managed Agents session driver, one module per stage (including the observed deploy to dev), a GitLab client, and a CLI. The state machine, checks, gates, graders, and deploy observation are tested (`pytest`: 57 passing). The session driver and GitLab client follow the documented APIs but have not been run live yet; the pipeline and environment reads are unverified against the self-hosted GitLab (spikes 1 and 2b on the roadmap).
- **`evals/`** A four-level evaluation plan, dataset specifications with sample rows, deterministic and stochastic graders (kappa, bootstrap and Bayesian intervals, pass^k, paired discrimination), and a runner whose session-producing step is a stub until the live spike.
- **`examples/ledgerline/`** A complete fictional run: outcome, decomposition, a spec, a judge verdict, review findings, the implementer's response, and a merge-readiness verdict.

## Running the tests

```bash
cd orchestrator && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/pytest -q
```

## Applying the agents (when ready to spike)

```bash
ant apply --dry-run -v agents environments   # review the plan
ant apply --yes agents environments          # creates agents + environment, writes claude-lock.json (commit it)
cd orchestrator && .venv/bin/factory doctor  # reconciles IDs, vault, GitLab config
```

See [environments/README.md](environments/README.md) for the GitLab credential setup and the self-hosted host and preview-domain placeholders.

## Status

Design complete to the point of being iterable. Not yet run against the live platform. The honest list of gaps is at the bottom of [docs/roadmap.md](docs/roadmap.md).
