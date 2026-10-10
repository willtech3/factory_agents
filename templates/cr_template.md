# {{spec_id}}: {{spec_title}}

> Factory run `{{run_id}}` · outcome: {{outcome_title}} · design record: `.factory/runs/{{run_id}}/` on this branch{{#design_cr_url}} · design change request: {{design_cr_url}}{{/design_cr_url}}{{#tracker_url}} · tracker: {{tracker_url}}{{/tracker_url}}

## Summary

{{merge_judge_summary_for_human}}

## Decisions needed from you

{{#accepted_risks}}
- **{{finding_id}}** ({{severity}}, {{category}}): {{claim}}. Implementer's position: {{explanation}}
{{/accepted_risks}}
{{^accepted_risks}}
None. The judge found no accepted-risk items.
{{/accepted_risks}}

## Acceptance criteria

Status and evidence are the merge judge's (`merge_readiness.json` `criteria`); the e2e column lists the orchestrator's own `test_results.json` rows that cover the criterion; the reviewer's `spec_conformance` status is for information.

| AC | Given / When / Then | Judge | Evidence | Orchestrator e2e | Reviewer |
|---|---|---|---|---|---|
{{#acceptance_criteria}}
| {{ac_id}} | {{given}} / {{when}} / {{then}} | {{judge_status}} | {{judge_evidence}} | {{e2e_rows}} | {{reviewer_status}} |
{{/acceptance_criteria}}

## What was contested

{{#contested_findings}}
- **{{finding_id}}** {{severity}}: {{claim}} → implementer `{{disposition}}` → reviewer `{{final_status}}` → judge: {{judge_assessment}}
{{/contested_findings}}
{{^contested_findings}}
No findings were disputed.
{{/contested_findings}}

## Verification

{{#preview_platform}}
Run by the orchestrator through the platform's CI on `{{judged_head_sha}}`:
{{/preview_platform}}
{{#preview_local}}
Run by the orchestrator on its own host on `{{judged_head_sha}}`, in a per-run virtual environment built from this repository ({{environment_installer}}, Python {{environment_python}}, {{environment_install}}); not a clean sandbox and not a deployment:
{{/preview_local}}

{{#test_commands}}
- `{{command}}` → {{result}}
{{/test_commands}}

{{#preview_platform}}
End-to-end on the preview environment ({{preview_url}}, pipeline {{preview_pipeline_url}}):
{{/preview_platform}}
{{#preview_local}}
End-to-end against the service the orchestrator started from the manifest entrypoint (`{{entrypoint}}`) on its host; reviewers ran the same entries against an instance they started the same way:
{{/preview_local}}

{{#e2e_results}}
- `{{command}}` → exit {{exit_code}} (covers {{covers_ac}})
{{/e2e_results}}

Deterministic checks: {{deterministic_checks_summary}}

{{#preview_platform}}
After merge the orchestrator watches the platform's dev deploy and re-runs the end-to-end checks against dev; the result appears on the run board (and the tracker issue), not in this change request.
{{/preview_platform}}
{{#preview_local}}
There is no dev deployment in local mode; nothing runs after merge.
{{/preview_local}}

## Where to start reading

{{#files_to_read_first}}
- `{{path}}` — {{why}}
{{/files_to_read_first}}

## Provenance

| Stage | Agent (version) | Session | List cost |
|---|---|---|---|
{{#sessions}}
| {{stage}} | {{agent_name}} v{{agent_version}} | [{{session_id}}]({{console_url}}) | ${{list_cost_usd}} |
{{/sessions}}

Spec: `.factory/runs/{{run_id}}/specs/{{spec_id}}.md` and `.json`; spec judge verdict: `.factory/runs/{{run_id}}/verdict.json`; merge judge verdict: `.factory/runs/{{run_id}}/evidence/{{spec_id}}/merge_readiness.json` (all committed on this branch by the orchestrator).

---
*Opened by the factory integrator. A human must review and approve this change request; the factory never merges.*
