# factory-bench item: sample-app

The first end-to-end run of the factory, kept as a reference. It is **not** a hidden acceptance suite (the implementer's own tests are never the oracle); it is the baseline a future run on the same outcome is compared against for shape, cost, interventions, and the judge's rubric scores.

- `outcome.json`: the outcome the run started from (identical to `examples/sample-app/outcome.json`).
- Target repository at its pre-change state: `examples/sample-app/` in this repository (the live repository `willtech3/factory-sample-app` was a throwaway copy of it).
- `reference-run/artifacts/`: every artefact the run persisted, by stage (decomposition, spec rounds, judge verdicts, implementation reports, deterministic checks, test results, reviewer findings per pass, merged findings, review responses, merge readiness, integrator output), with sha256 sidecars.
- `reference-run/events.jsonl`: the run's full event log (`run_57fde1adc2`, 2026-10-10). A run now keeps its events in `runs/<run_id>/run.db`; `factory export-events <run_id>` writes them in this form.
- `reference-run/pages/`: the Gate 1 and Gate 2 reading pages as the owner saw them (Gate 2 as approved, the second time it opened).
- `reference-run/change.patch`: the diff the factory produced (`main...factory/run_57fde1adc2/spec_wi-due-dates-overdue`, judged head `93ced45`, plus the run-record commit).
- `reference-run/pull_request.json`: the pull request the integrator opened, body included.

Run facts: one work item; spec passed the judge in round 2 after one Gate 2 request-changes round trip; implementation took two rounds (three reviewer findings fixed, one orchestrator check finding); merge judge `ready` with no accepted risks and one open nit; 20 agent sessions; $33.18 platform cost; one manual resume after an orchestrator crash (file-listing pagination, fixed the same day). Gate decisions were made by Claude on the owner's instruction, so this item carries no human gate labels.
