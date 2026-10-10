# The Factory Console: outcomes, runs, and the first two gates in a browser

The Console is the `web` gate surface ([ADR-0007](adr/0007-pluggable-gate-surfaces.md)): a local web app over the run store and the file queue, for the people who write outcomes and decide Gates 1 and 2. It does not replace the tracker or the code host; it is the workbench for the stages before code exists, and the place where a gate is explained before it is decided.

```bash
cd orchestrator && .venv/bin/pip install -e ".[web]"
.venv/bin/factory web                 # http://127.0.0.1:8765/
.venv/bin/factory web --demo          # also seeds a simulated run from examples/ledgerline
```

Configuration is read from the environment and from `.env` at the repository root (`ANTHROPIC_API_KEY`, `ANTHROPIC_WORKSPACE_ID`, `FORGE_REPO`, `FORGE_TOKEN`, and the rest of [07-personal-e2e-setup.md](07-personal-e2e-setup.md)); values already set in the environment win. The Console reads `runs/<run_id>/events.jsonl` and the artifacts, and writes decisions and answers to `runs/_queue/`, exactly where `factory decide` and `factory answer` write them, so a `factory run` started from the Console or from a terminal picks them up unchanged.

## The screens

**Inbox.** What is waiting on a person: gates to decide and questions from agents, with the run title, a one-line summary, and one button each. Below it, the pipeline as a numbered strip, because the stages really are a sequence: outcome, decomposing, Gate 1 (you), specifying, Gate 2 (you), building, done.

**Runs.** One row per run with its stage, work-item count, spend, and last activity. A run page shows the same strip with the current stage, what needs you (questions answered inline, gates linked), the work items with their stage and pull request, the timeline from the event log with links to each agent session in the Anthropic Console, and every file produced with its fingerprint.

**Outcomes.** Saved outcomes and a form in the owner's vocabulary: what should be true when this is done, metrics with baseline and target, constraints, the repository, the owner. Saving validates against `schemas/outcome.schema.json` and names what is missing. From a saved outcome you start a **full run** (real agents through pull requests; needs the code host), a **design run** (real decomposer, spec writers, and judge through Gate 2; no code; needs only the platform), or a **demo** (simulated agents, real gates, nothing leaves the machine).

**Gate review.** The page is built to be read top to bottom:

1. *What you are deciding*: one sentence on what the agents did and what approving starts, the decomposer's or judge's summary, and at-a-glance counts (work items, metrics covered, items that need your judgement, open questions, automatic checks; at Gate 2 the verdict, criteria, issues the judge flagged, metrics covered by criteria).
2. *What to check*: a checklist specific to the gate, with the numbers filled in ("Dunning email sequence needs your judgement", "2 questions will use their defaults").
3. The content, structured: metric coverage as a table; work items in the order they would be built, each with its intent, what it moves, what it follows, and a note field; at Gate 2 one card per spec with the judge's issues first, acceptance criteria as given / when / then, interfaces, allowed paths, and test plan folded away, an "Approve this spec" checkbox, and a note field.
4. *Your decision*, in a panel that stays on screen: what each option causes, a comment field, and the buttons. Every decision is two steps (the second button appears after a beat), so a stray click never decides. Notes left under an item travel with Request changes prefixed by the item id, which is how the orchestrator routes feedback. "Why this is safe to approve" explains the fingerprint binding ([ADR-0005](adr/0005-gates-bind-to-artifact-hashes.md)) and lists the files.

**Settings.** Readiness for the three things a run needs (agents and platform, code host, where decisions are read from), with the missing variable named; a live check that the agents in `claude-lock.json` exist on the platform and a list of recent sessions in the workspace with links to the Anthropic Console; the runs directory; and the logs of orchestrators started from the Console.

## The demo run

`--demo`, or "Run as demo", replays the Ledgerline example through the real store, state machine, checks, and file queue: a blocking question from the decomposer, Gate 1 with revision 2 on request-changes, four schema-valid specs and a judge verdict at Gate 2, approve-subset, pull requests, completion. Every event it appends is one the orchestrator would append, so a demo run looks exactly like a real one. It exists so the human loop can be exercised, and tested, without the platform.

## What it does not do yet

- No streaming of agent progress: the timeline is the event log, and session links go to the Anthropic Console for the trace.
- No Gate 3: code review is the code host's.
- No authentication: it binds to localhost and records decisions as `console`. Put it behind something before sharing it.
- The standalone page at `/gates/<id>/page` (the renderer in `surfaces/artifact_pages.py`) is the same content without the decision panel, for pasting into a tracker.
