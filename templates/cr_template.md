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

After merge the orchestrator watches the platform's dev deploy and re-runs the end-to-end checks against dev; the result appears on the run board (and the tracker issue), not in this change request.

## Where to start reading

{{#files_to_read_first}}
- `{{path}}` — {{why}}
{{/files_to_read_first}}

## Provenance

| Stage | Agent (version) | Session | Duration | List cost |
|---|---|---|---|---|
{{#sessions}}
| {{stage}} | {{agent_name}} v{{agent_version}} | [{{session_id}}]({{console_url}}) | {{duration}} | ${{list_cost_usd}} |
{{/sessions}}

Sessions above: ${{sessions_cost_usd}} over {{sessions_duration}}; whole run so far: ${{list_cost_usd}}.

Spec: `.factory/runs/{{run_id}}/specs/{{spec_id}}.md` and `.json`; spec judge verdict: `.factory/runs/{{run_id}}/verdict.json`; merge judge verdict: `.factory/runs/{{run_id}}/evidence/{{spec_id}}/merge_readiness.json` (all committed on this branch by the orchestrator).

---
*Opened by the factory integrator. A human must review and approve this change request; the factory never merges.*
