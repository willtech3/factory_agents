{{! The change request description. The controller renders it (orchestrator/factory/pr_description.py) for an
engineer who knows the app and nothing about the factory, and lints what the integrator sends: at most 60 lines, these
section headings, no factory ids or vocabulary, no agent or session names, no costs, no first-person text, no links to
the owner's machine except the one run-record link, at most three screenshots and one demo. Everything else lives in
the run record (post-mortem 2026-10-11 §4). }}
## What changed and why

{{what_changed}}

## Needs your decision

{{#decisions}}
- {{text}}{{#location}} (`{{location}}`){{/location}}.
{{/decisions}}
{{^decisions}}
None.
{{/decisions}}
{{#see_it}}

## See it

{{see_it}}
{{/see_it}}

## How it was tested

{{tested}}

{{#has_commands}}
To run the tests locally:

{{/has_commands}}
{{#commands}}
- `{{command}}`
{{/commands}}

## Size

{{size_line}}

## Start reading at

{{#read_first}}
- `{{path}}`{{#why}}: {{why}}{{/why}}
{{/read_first}}

---

[Full run record]({{run_record_url}}), in the owner's Factory Console: acceptance criteria with evidence, review findings and how they were settled, verification detail, every screenshot and recording, and cost.
