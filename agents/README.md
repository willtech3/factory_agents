# Agent definitions

One file per agent in the format `ant apply` consumes: YAML frontmatter is the Managed Agents create body, the Markdown body is the system prompt. Sync with:

```bash
ant apply --dry-run -v agents environments
```

Review the plan, then re-run with `--yes` once you agree with it. `claude-lock.json` is written at the repo root and must be committed; the orchestrator reads agent and environment IDs from it (see `orchestrator/factory/config.py`).

## Roster

| Agent | Stage | Model | Writes code? | Independent context from | Why it exists |
|---|---|---|---|---|---|
| `codebase_researcher` | all | Sonnet 5.5, medium | no | the goal (it is told what to document, not why) | Goal-blind factual research; keeps reading out of the lead agents' context |
| `decomposer` | 1 | Opus 5.5, high | no | - | Turns an outcome into a DAG of work items for Gate 1 |
| `spec_writer` | 2 | Sonnet 5.5, high | no | - | Writes one spec per work item |
| `spec_judge` | 2 | Opus 5.5, high | no | spec_writer (different model tier, never sees the writer's reasoning) | Grades specs per rubric and judges whether the set achieves the outcome |
| `implementer` | 3 | Opus 5.5, xhigh | yes | - | Implements one spec in vertical slices with tests |
| `reviewer` | 3 | Opus 5.5, high | no | implementer (fresh session per round, sees only diff + spec) | Adversarial review with falsifiable findings |
| `security_reviewer` | 3 | Fable 5.1, high | no | implementer (fresh session per round, sees only diff + spec + the orchestrator's pre-checks) | Security review: trust boundaries, attacker-controlled input, authorisation, secrets, dependencies; one CWE per finding; reproduced findings stand whatever their label |
| `merge_judge` | 3 | Opus 5.5, high | no | both | Audits the reviewer/implementer exchange; decides ready / another round / escalate |
| `integrator` | 4 | Sonnet 5.5, medium | no (pushes) | - | Opens the change request (PR or MR) with the short reviewer-first description; the evidence is in the run record |

## Conventions every prompt follows

- **Instruction budget.** Prompts stay under about 80 lines. Anything longer belongs in a skill under `.claude/skills/` in the target repo or in `rubrics/`. Each rule should trace to a failure we observed or a cited source in `docs/research/`.
- **Artifacts, not chat.** Agents coordinate through files under `/mnt/session/outputs/`, validated against `schemas/` by the agent and again by the orchestrator on receipt. No agent reads another agent's transcript.
- **Rules live in the registry.** Each prompt ends in a `## Rules` block generated from `orchestrator/factory/rules.py` (`factory rules`; [ADR-0015](../docs/adr/0015-rule-registry.md)). Prose cites a rule's id instead of restating it, and the block is edited only through the registry.
- **Judges never author.** Spec judge and merge judge report; they do not rewrite specs or code. This keeps the role separation that makes their verdicts worth anything.
- **Tests are a contract.** Implementers may add tests and may not modify or delete existing tests unless the spec's `allowed_paths` explicitly includes them. The orchestrator enforces this deterministically.
- **Say so when stuck.** Every agent has an explicit way to stop and report rather than improvise: `ask_human` for the planning agents, an `open_questions` block for writers, `escalate` results for judges.
- **Web off by default.** Only `codebase_researcher` may be given web access, per session, when a spec involves an external API.
- **The platform is not ours.** Decomposer, spec writer, spec judge, implementer, and reviewer read the `platform-contract` skill (placeholder in `skills/`; the platform team's version replaces it). Nobody deploys; nobody touches IAM, networking, CI templates, or the platform repository. Implementer and reviewer run `test_plan.e2e` against the preview URL the orchestrator hands them in `preview.json`.

## Self-preference caveat

All agents run on Claude. Research on judge self-preference bias (see `docs/research/academic-2026.md`, Yang et al. 2604.22891) says capability does not protect against it. Mitigations used here: different model tier for writer and judge where possible, decomposed per-criterion rubrics, judges never see the author's self-assessment, and execution-grounded evidence requirements. For high-stakes repositories, `reviewer` can be pinned to a different model (for example Claude Fable 5.1) via a per-session `agent_with_overrides`; see `docs/04-managed-agents-mapping.md`.
