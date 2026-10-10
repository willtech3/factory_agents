# {{spec_id}}: {{spec_title}}

> Factory run `{{run_id}}` · design MR !{{design_mr_iid}} · outcome: {{outcome_title}}

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

| AC | Given / When / Then | Status | Evidence |
|---|---|---|---|
{{#acceptance_criteria}}
| {{ac_id}} | {{given}} / {{when}} / {{then}} | {{status}} | {{evidence}} |
{{/acceptance_criteria}}

## What was contested

{{#contested_findings}}
- **{{finding_id}}** {{severity}}: {{claim}} → implementer `{{disposition}}` → reviewer `{{final_status}}` → judge: {{judge_assessment}}
{{/contested_findings}}
{{^contested_findings}}
No findings were disputed.
{{/contested_findings}}

## Verification

Run by the orchestrator in a clean sandbox on `{{head_sha}}`:

{{#test_commands}}
- `{{command}}` → {{result}}
{{/test_commands}}

End-to-end on the preview environment ({{preview_url}}, pipeline {{preview_pipeline_url}}):

{{#e2e_results}}
- `{{command}}` → exit {{exit_code}} (covers {{covers_ac}})
{{/e2e_results}}

Deterministic checks: {{deterministic_checks_summary}}

After merge the orchestrator watches the platform's dev deploy and re-runs the end-to-end checks against dev; the result appears on the run board, not in this MR.

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

Spec: `.factory/runs/{{run_id}}/specs/{{spec_id}}.md` (design MR). Rubric scores and full findings: `.factory/runs/{{run_id}}/evidence/{{spec_id}}/`.

---
*Opened by the factory integrator. A human must review and approve this MR; the factory never merges.*
