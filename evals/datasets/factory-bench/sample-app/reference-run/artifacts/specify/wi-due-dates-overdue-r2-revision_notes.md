# Revision 2: how each judge issue was handled
- I-1: spec.md is now rendered mechanically from spec.json (one source, both revision 2). Content follows the MD's fixes.
- I-2: per owner decision (human_comments.md (2)) the preview rollover pair is gone. AC-10 stays user_visible; evidence is the
  clock-patched pytest test plus the local `rollover-local` entry, stated in AC-10's verification_note and an open question.
  The preview `listing` entry covers AC-10's per-request invariant only, and says so.
- I-3: 0 and 1700000000 (and 1.5, "2026-11-30\n", "0000-01-01") are in AC-9; the wrong `date | None` claim is corrected in
  context. The preview.yml placeholder deploy is declared in dependencies.external, with the owner's preview decision (1).
- I-4: `local-all` runs `empty-overdue --strict` first on the fresh server, then `all` (shape-only); stated in the interface.
- I-5: AC-13 lists the exact README strings the command checks; prose quality is left to review.
- I-6: AC-1 sends an unknown field; forbidden_changes and AC-12 (grep for extra=/model_config/ConfigDict) pin it.
- I-7: import scan is an AST scan against sys.stdlib_module_names plus exact allowed roots (probed: `import requests` fails,
  allowed-only code passes); diff scope uses git diff vs main plus untracked files (probed with a stray file).
- I-8: AC-7's note says the preview entry checks shape only; strict [] is local.
- I-9: owner decisions are quoted by number from human_comments.md; nothing else is claimed as an owner decision.
- I-10: spec.md is 199 lines at 118 columns with no gate or process commentary.
- I-11: PYTHONPATH=. stated for rollover-local; malformed check uses probe titles; overdue-list fixture has a-f incl. due-today;
  test -f and a named pytest file guard against a missing test file.
