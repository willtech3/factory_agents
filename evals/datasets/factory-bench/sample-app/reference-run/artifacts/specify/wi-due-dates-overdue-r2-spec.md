# spec_wi-due-dates-overdue (revision 2): Optional due date on todos, overdue flag, and GET /todos/overdue
Work item `wi-due-dates-overdue` | outcome `out_4e93d2a0d0` | decomposition `dec_4e93d2a0d0r1` | size S
## Goal
After this merges, a client can send an optional calendar due_date (YYYY-MM-DD) when creating a todo; every todo the
API returns carries due_date (null when unset) and a boolean overdue computed at response time; and GET /todos/overdue
returns exactly the overdue todos in ascending id order. A todo is overdue if and only if it is open and its due_date
is strictly before the current UTC calendar date (confirmed by the owner, human_comments.md (3)). This serves the
metrics 'Overdue visibility' and 'Backwards compatibility' (title-only clients and the three existing tests are
unchanged). The goal is missed if any flag disagrees with the rule, the overdue list differs from the flagged todos,
or a malformed due_date is stored.
## Non-goals
- Changing or clearing a due date after creation (no PATCH/PUT); un-completing a todo; reminders; filtering, sorting
  or paginating by due date.
- Datetime due dates, per-user timezones, or any timezone other than UTC.
- Persistence, a database, response-model clean-up, any change to service_manifest.json, pyproject.toml, .github/ or
  CI, or a GET /todos/{todo_id} route.
## Context
Base commit 6dec694.
- app/storage.py:10-17: Todo is a stdlib dataclass (id, title, done, created_at via default_factory
  datetime.now(timezone.utc).isoformat()); to_dict() is asdict(self). It is the only clock and is not injectable.
  app/storage.py:21-53: TodoStore (dict plus lock): list() sorts by id; create(title); complete(id) returns the Todo
  or None.
- app/main.py:16-17: TodoIn has only title (min 1, max 200) and no extra='forbid', so unknown fields are ignored.
  app/main.py:25-46: routes return plain dicts. The file uses `from __future__ import annotations`, so TodoIn's types
  must be imported at module level. No GET /todos/{todo_id} exists, so /todos/overdue cannot be shadowed today.
- tests/test_todos.py: three tests with fixtures (autouse store.clear(), TestClient) defined in that file, so
  tests/test_overdue.py needs its own. No test compares a whole todo dict.
- Checked in a scratch venv (fastapi 0.143, pydantic 2.14): `due_date: str | None = Field(default=None,
  pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")` plus a field_validator calling date.fromisoformat returns 422 for every
  AC-9 value and 201 for "2026-11-30". A plain `date | None` field is not enough (it accepts datetimes and integer
  timestamps).
- Platform (inferred from service_manifest.json; the platform-contract skill was unavailable): no env var, port or
  migration is added, so no `config` interface is declared and the manifest is untouched. preview.yml's Deploy step is
  a placeholder that only echoes a URL; per the owner (human_comments.md (1)) the orchestrator starts the service from
  the manifest and exports $BASE_URL.
## Acceptance criteria
- AC-1 [auto, user-visible] Given An empty store and a client that sends only a title, as every existing client does.
  When It sends POST /todos {"title": "Write the outcome"}, then POST /todos {"title": "t", "priority": "high"} (an
  unknown field). Then Both responses are 201. Each body's keys, in order, are exactly id, title, done, created_at,
  due_date, overdue. The first has id 1, title echoed, done false, created_at a non-empty ISO-8601 string (all as
  today), due_date null, overdue false. The second is created normally and has no "priority" key (unknown request
  fields are still ignored).
- AC-2 [auto, user-visible] Given A client sends a future date or an explicit null. When It sends POST /todos with
  due_date "2999-12-31", and again with due_date null. Then Both 201; due_date echoed ("2999-12-31" / null) and
  overdue false in both.
- AC-3 [auto, user-visible] Given A client that sends a past due date. When It sends POST /todos {"title": "late",
  "due_date": "2000-01-01"}. Then 201 (past dates are accepted), "due_date": "2000-01-01", "overdue": true. GET /todos
  returns the same todo with "overdue": true.
- AC-4 [auto, user-visible] Given The current UTC calendar date is D (pytest fixes it by patching
  app.storage.today_utc; a live instance uses the system clock). When Todos are created with due_date D-1, D and D+1,
  then GET /todos is called. Then Overdue is true for D-1, false for D (strictly before today is the rule, so due
  today is not overdue) and false for D+1. Note: Pytest adds month (today 2026-03-01, due 2026-02-28) and year (today
  2027-01-01, due 2026-12-31) boundaries. The live check retries if the UTC date changed mid-check.
- AC-5 [auto, user-visible] Given Open and completed todos exist, with and without due dates. When GET /todos is
  called. Then 200 and a list in ascending id order. Every element has exactly the keys id, title, done, created_at,
  due_date, overdue; due_date is a YYYY-MM-DD string or null; overdue is a bool equal to (done is false AND due_date
  is not null AND due_date < current UTC date). A todo with due_date null is never overdue.
- AC-6 [auto, user-visible] Given The store holds: open past-due todo a; past-due todo b that was then completed; open
  future-due c; open no-due-date d; open todo e due today (UTC); a second open past-due todo f (f > a). When GET
  /todos/overdue is called. Then 200 (not 404 or 422, so the route is not shadowed) and a list of exactly a then f,
  each with overdue true, done false and the full shape of AC-5. b, c, d and e are absent.
- AC-7 [auto, user-visible] Given No todo is both open and past due (empty store, or every past-due todo is
  completed). When GET /todos/overdue is called. Then 200 with body [] (not null, not an error). Note: Strict [] is
  proven by pytest and `local-all` (fresh server). The preview entry may share an instance with other todos, so it
  checks shape only.
- AC-8 [auto, user-visible] Given An open todo with due_date 2000-01-01 is in GET /todos/overdue. When POST
  /todos/{id}/complete is called, then GET /todos/overdue and GET /todos. Then The complete response is 200 with done
  true, due_date "2000-01-01" and overdue false. The todo is gone from GET /todos/overdue and still in GET /todos with
  overdue false. An unknown id still returns 404.
- AC-9 [auto, user-visible] Given A running service holding N todos. When POST /todos is called with title and a
  malformed due_date, one request per value: "2026-13-01", "2026-02-30", "0000-01-01", "not-a-date", "", "20261130",
  "2026-11-30T17:00:00Z", "2026-11-30T00:00:00", "2026-11-30 ", "2026-11-30\n", 20261130, 0, 1700000000, 1.5, true,
  ["2026-11-30"]. Then Every response is 422 (as an empty title is today) and no todo is created. Only null or a
  string of exactly 10 characters matching [0-9]{4}-[0-9]{2}-[0-9]{2} that is a real calendar date is accepted;
  datetimes are rejected even at midnight, and 2028-02-29 is accepted while 2027-02-29 is rejected. Note: A plain
  pydantic `date` field accepts datetimes and integers like 0 (timestamps); see context.
- AC-10 [auto, user-visible] Given A todo is open with due_date D and the UTC date is controlled by patching
  app.storage.today_utc. When With the clock at D, GET /todos and GET /todos/overdue are read; the clock is advanced
  to D+1 with no write in between; both are read again. Then At D the todo has overdue false and is absent from the
  overdue list. At D+1 the same todo has overdue true in GET /todos and is present in GET /todos/overdue, so the flag
  is derived when the response is built and never stored. Note: Owner decision (human_comments.md (2)): a live
  instance cannot show a date rollover in one run, so there is no preview rollover entry. The clock-patched pytest
  test and the local `rollover-local` entry (clock driven in-process) are the authoritative evidence. The preview
  `listing` entry covers only the per-request invariant.
- AC-11 [auto, internal] Given The repository at the base commit. When The whole test suite is run. Then The three
  tests in tests/test_todos.py pass unedited, and tests/test_overdue.py passes.
- AC-12 [static, internal] Given The finished change. When The static checks in test_plan.commands are run. Then Only
  allowed_paths changed (untracked files included); pyproject.toml, service_manifest.json, .github/ and
  tests/test_todos.py equal main. Imports under app/ and tests/ are stdlib, fastapi, starlette, pydantic, uvicorn,
  pytest, httpx or app only. app/main.py contains no extra=, model_config or ConfigDict (TodoIn must not forbid
  unknown fields).
- AC-13 [static, internal] Given The finished change. When README.md and the app/main.py module docstring are read.
  Then The docstring's endpoint list contains the strings 'GET /todos/overdue' and 'due_date'. README.md contains each
  of: 'GET /todos/overdue', 'due_date', 'overdue', 'YYYY-MM-DD', 'strictly before', 'UTC'. Note: The command enforces
  these exact strings only; whether the prose states the rule correctly is left to review.
## Interfaces
- Todo response object (http_api, modified): Returned by every todo endpoint; existing keys unchanged, two appended.
  {"id": 1, "title": "Pay rent", "done": false, "created_at": "2026-10-10T08:10:54.068302+00:00", "due_date":
  "2026-11-30", "overdue": false}
  due_date: "YYYY-MM-DD" string or null. overdue: bool, true iff done is false AND due_date is not null AND
  date.fromisoformat(due_date) < app.storage.today_utc(). Both new keys always present.
- POST /todos (http_api, modified): Body: {"title": str (1..200), "due_date": str|null (optional, default null)}.
  due_date must match ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ and be a real date, else 422. Declare it as `str | None` with
  Field(pattern=...) and a validator calling date.fromisoformat (a plain date type fails AC-9). Unknown fields are
  ignored as today. Response: 201 with the Todo response object.
- GET /todos (http_api, modified): 200, list of Todo response objects in id order; [] when empty.
- GET /todos/overdue (http_api, new): No parameters. 200, list of Todo response objects whose overdue is true,
  ascending id; [] when none. One today_utc() reading per request. A future GET /todos/{todo_id} must be registered
  after this route.
- POST /todos/{todo_id}/complete (http_api, modified): Unchanged except the 200 body is the Todo response object
  (overdue false since done).
- app.storage.today_utc (function, new): def today_utc() -> datetime.date: return datetime.now(timezone.utc).date()
  Module-level in app/storage.py and only ever called through that module global (never `from .storage import
  today_utc`), so tests can monkeypatch.setattr('app.storage.today_utc', lambda: date(2026, 11, 30)).
- app.storage.Todo / TodoStore (function, modified): Todo gains `due_date: str | None = None` after created_at.
  Todo.is_overdue(self, today: date | None = None) -> bool (today defaults to today_utc()). Todo.to_dict(self, today:
  date | None = None) -> dict is asdict(self) plus "overdue", keys ordered id, title, done, created_at, due_date,
  overdue. TodoStore.create(self, title: str, due_date: date | None = None) -> Todo stores due_date.isoformat() or
  None. New TodoStore.list_dicts(self, overdue_only: bool = False) -> list[dict] reads today_utc() once and returns
  to_dict(today) per todo in id order, only overdue ones when overdue_only. Other TodoStore methods unchanged. GET
  /todos and GET /todos/overdue call list_dicts; the POST routes call to_dict().
- tests/e2e_overdue.py (cli, new): python tests/e2e_overdue.py <check> [base_url]; base_url defaults to $BASE_URL.
  Exit 0 on pass, else 1 naming the failed assertion. Checks, in order: compat, create-future, create-past, boundary,
  listing, overdue-list, empty-overdue, complete, malformed (AC-1..AC-9). `all` runs them in that order (empty-overdue
  in shape-only mode). `empty-overdue --strict` also asserts the body is exactly [] and is valid only on a server
  holding no todos. `local-all` starts `python -m uvicorn app.main:app` from the repo root on a free 127.0.0.1 port,
  polls /health until 200, runs `empty-overdue --strict` first (before any check creates a todo), then `all`, and
  always stops the server. `rollover-local` runs in-process (imports app.main and fastapi.testclient; needs
  PYTHONPATH=. as in the test command) and patches app.storage.today_utc. All other checks use only the standard
  library. Each check creates its own todos, asserts only on those ids plus single-response invariants, and never
  touches others' todos, so it passes on an instance already holding todos. Not collected by pytest (no test_ prefix).
## Allowed paths
`app/storage.py`, `app/main.py`, `README.md`, `tests/test_overdue.py`, `tests/e2e_overdue.py`
## Forbidden changes
- Do not edit tests/test_todos.py, pyproject.toml, service_manifest.json or .github/; add no database, dependency, env
  var or date library.
- Do not change id, title, done, created_at; do not store overdue on the Todo; do not add extra='forbid' to TodoIn.
- No time.sleep, datetime.now() or date.today() in tests/test_overdue.py; control 'today' via app.storage.today_utc or
  far-past/future dates.
## Non-functional
- backwards_compatibility: Additive only: existing keys keep names, types, values and order; a title-only POST behaves
  as before; unknown request fields are still ignored.
## Test plan
Commands that must pass (from the repo root):
- `python -m pytest -q`
- `python -m pytest -q tests/test_todos.py tests/test_overdue.py`
- `test -f tests/test_overdue.py && test -f tests/e2e_overdue.py`
- `git diff --exit-code main -- tests/test_todos.py pyproject.toml service_manifest.json .github`
- `test -z "$( (git diff --name-only main; git ls-files -o --exclude-standard) | grep -vxE
  'app/storage\.py|app/main\.py|README\.md|tests/test_overdue\.py|tests/e2e_overdue\.py' )"`
- `python -c "import ast,glob,sys;
  ok=set(sys.stdlib_module_names)|{'app','fastapi','pydantic','uvicorn','pytest','httpx','starlette'};
  fs=glob.glob('app/**/*.py',recursive=True)+glob.glob('tests/**/*.py',recursive=True); bad=[(f,m) for f in fs for n
  in ast.walk(ast.parse(open(f).read())) for m in ([a.name.split('.')[0] for a in n.names] if isinstance(n,ast.Import)
  else [n.module.split('.')[0]] if isinstance(n,ast.ImportFrom) and n.level==0 else []) if m not in ok];
  print('disallowed imports:',bad); sys.exit(bool(bad))"`
- `python -c "import ast; d=ast.get_docstring(ast.parse(open('app/main.py').read())); r=open('README.md').read();
  assert 'GET /todos/overdue' in d and 'due_date' in d, 'docstring'; assert all(s in r for s in ['GET
  /todos/overdue','due_date','overdue','YYYY-MM-DD','strictly before','UTC']), 'README'"`
- `! grep -nE "sleep|datetime\.now|date\.today" tests/test_overdue.py`
- `! grep -nE "extra *=|model_config|ConfigDict" app/main.py`
- `python tests/e2e_overdue.py local-all`
- `PYTHONPATH=. python tests/e2e_overdue.py rollover-local`
E2E: `python tests/e2e_overdue.py <check>`, preview ones with $BASE_URL appended; AC numbers in parentheses:
- [preview] `compat` (1): title-only and unknown-field POSTs: shape and values.
- [preview] `create-future` (2): due_date 2999-12-31 and null: echoed, overdue false.
- [preview] `create-past` (3): due_date 2000-01-01: 201, overdue true here and in GET /todos.
- [preview] `boundary` (4): Due yesterday/today/tomorrow (UTC system clock, retried on date change): true/false/false.
- [preview] `listing` (5,10): Mixed todos; one GET /todos response obeys id order, key set and the rule (for AC-10:
  per-request invariant only).
- [preview] `overdue-list` (6): Create a-f of AC-6; among them the overdue list is exactly a, f; all elements overdue
  true.
- [preview] `empty-overdue` (7): Shape only: own non-overdue todos absent; every element overdue true. Does not prove
  the empty case.
- [preview] `complete` (8): Complete a past-due todo; flag cleared, off the overdue list; unknown id 404.
- [preview] `malformed` (9): Each AC-9 value with a unique probe title (e2e-malformed-<uuid>): 422, and no todo with
  that title in GET /todos.
- [local] `local-all` (1,2,3,4,5,6,7,8,9): Starts the service locally, runs `empty-overdue --strict` first, then
  `all`.
- [local] `PYTHONPATH=. rollover-local` (10): In-process: clock D then D+1 with no write; overdue flips false to true
  in both endpoints.
New tests:
- tests/test_overdue.py (1,2,3,4,5,11): Create variants (title-only, unknown field, 2999-12-31, null, 2000-01-01);
  patched-today boundaries D-1/D/D+1 plus month and year edges; mixed-store GET /todos obeys the rule per element.
- tests/test_overdue.py (6,7,8,9,10): Six-todo store gives exactly [a, f]; empty and non-overdue stores give [];
  completing clears the flag; parametrized malformed values incl. 0 and 1700000000 give 422 with no todo created;
  2028-02-29 accepted, 2027-02-29 rejected; D to D+1 with no write flips overdue.
- tests/e2e_overdue.py (1,2,3,4,5,6,7,8,9,10): tests/e2e_overdue.py per the interfaces.
## Dependencies, rollout, open questions
- External: Preview environment: per the owner (human_comments.md (1)) the orchestrator starts the service from
  service_manifest.json and exports $BASE_URL. The preview.yml Deploy step is a placeholder that only echoes a URL and
  deploys nothing.
- Rollout: no feature flag, no migration. Rollback: Revert the pull request. No data migration: storage is in memory.
- Q: Is a UTC-midnight cutoff acceptable outside UTC? Default: Yes; confirmed by the owner (human_comments.md (3));
  the service has no users or timezones.
- Q: Does the preview instance need to be fresh for each e2e run? Default: No. Preview entries pass on an instance
  already holding todos; only the local strict check needs a fresh server, which local-all starts.
- Q: Are pytest and local rollover-local the only evidence for AC-10 (no preview rollover entry)? Default: Yes; the
  owner's decision (human_comments.md (2)). AC-10 stays user_visible.
