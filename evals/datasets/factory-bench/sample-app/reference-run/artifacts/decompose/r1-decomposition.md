# Decomposition: Todos with due dates and an overdue view

Outcome `out_4e93d2a0d0` · decomposition `dec_4e93d2a0d0r1` · revision 1

## Approach

This is one coherent change to a small FastAPI service (about 100 lines of app code), so it is **one work item and one pull request**.

The change has three parts:
- **Optional due date.** A todo can take an optional due date when it is created.
- **Overdue flag.** Every todo the API returns carries a computed `overdue` flag.
- **Overdue view.** A new `GET /todos/overdue` returns the open todos whose due date has passed.

Clients that send only a title keep getting exactly the fields and values they get today. The only difference is two added keys: `due_date: null` and `overdue: false`.

**Why not split it?** I considered two items: field and flag first, endpoint second. I decided against it because:
- Both halves depend on one rule for what "overdue" means.
- A reviewer should approve that rule once, together with the tests that pin it down.
- Each half would be XS, and the split would only add a merge dependency.

The tests and the README note go inside the item, not into separate items.

**Owner decision.** The item is flagged `human_judgement_required` because what counts as "past due" is a product decision:
- whether the due date is a date or a date-and-time,
- which timezone applies,
- whether a task is overdue on its due day or only after it.

Proposed defaults are below. If you accept them, the item can go straight to specification.

**Platform.** The change needs none of the following:
- a database,
- a new dependency,
- a new environment variable,
- any change to `service_manifest.json`.

The existing `.github/workflows/preview.yml` deploys it as it is.

## Work items

| id | title | size | depends on | metrics |
|---|---|---|---|---|
| `wi-due-dates-overdue` | Optional due date on todos, overdue flag on every todo, and `GET /todos/overdue` | S | none | Overdue visibility; Backwards compatibility |

**What `wi-due-dates-overdue` delivers:**
1. **Optional due date.** It is set when the todo is created. A title-only request works exactly as today.
2. **Every returned todo carries `due_date` and `overdue`.** `due_date` is null when unset. `overdue` is true only when the todo is not done and its due date has passed. A todo with no due date is never overdue. This applies to:
   - `GET /todos`
   - `GET /todos/overdue`
   - the `POST /todos` response
   - the `POST /todos/{id}/complete` response
3. **`GET /todos/overdue`** returns exactly the overdue todos, ordered by ascending id like `GET /todos`. It returns `[]` when nothing is overdue.
4. **Validation.** A malformed due date returns 422, the same way an empty title does today.
5. **Tests and docs:**
   - The three existing tests pass **unedited**.
   - New API tests in `tests/` cover each rule. They include the due-date boundary, a completed todo with a past due date, and a todo without a due date.
   - The README and the endpoint list in the `app/main.py` docstring mention the new field and endpoint.

**Shared names, for spec writers:**
- request field `due_date`, optional
- response keys `due_date` (ISO-8601 string or null) and `overdue` (bool)
- route `GET /todos/overdue`

**Likely paths:** `app/storage.py`, `app/main.py`, `tests/test_todos.py`, `README.md`.

## Coverage

| Metric | Items | Why |
|---|---|---|
| Overdue visibility | `wi-due-dates-overdue` | Adds the due date, the per-todo `overdue` flag in every listing, and `GET /todos/overdue`. The new API tests in `tests/` are the measurement. |
| Backwards compatibility | `wi-due-dates-overdue` | The due date is optional and existing keys are unchanged. The existing tests must pass unedited, and new tests prove a todo without a due date is never overdue. |

Every metric is covered, and code can move both.

## Out of scope

- **Changing or clearing a due date after creation.** There is no update endpoint today, and adding one is a new capability. It can be a follow-up item if wanted.
- **Un-completing a todo.** No such operation exists, and none is needed here.
- **Reminders or notifications.** The outcome asks for visibility in API responses only.
- **Filtering or sorting `GET /todos` by due date, and pagination.**
- **Per-user timezones.** The service has no users. One server-side reference time (UTC) is used.
- **Persistence.** Storage stays in memory, as the constraints require. Due dates are lost on restart, like todos today.
- **A CI workflow that runs the tests.** None exists today, and `.github/` is outside the paths in scope.
- **Refactoring routes to use pydantic response models.** Today's routes return plain dicts, and that stays.

## Open questions

None block the work. Each has a default you can override at this gate.

1. **Date or date-and-time?** Default: a calendar date, `YYYY-MM-DD`. A datetime gets 422.
2. **When is a todo overdue, and in which timezone?** Default: when its due date is strictly before today's date in UTC. A todo due today is not overdue yet. UTC matches `created_at`.
3. **Is a completed todo with a past due date overdue?** Default: no. The flag then agrees with `/todos/overdue`, which lists open todos only.
4. **Can a todo be created with a due date already in the past?** Default: yes. It shows as overdue immediately.
5. **Are the key names `due_date` and `overdue` acceptable, and is `due_date: null` always present rather than omitted?** Default: yes to both, so every todo has one stable shape.
6. **Does the platform need anything beyond the current manifest and preview workflow?** Default: no. The change adds no env vars, ports, migrations or infrastructure. I could not confirm this against the platform-contract skill, which was not available in this session.

## Assumptions

- Adding keys is not "changing the shape of existing fields". Existing keys keep their names, types and values. Research found no test that compares a whole todo dict or its exact key set.
- `TodoIn` does not forbid unknown fields today, and this change keeps it that way, so clients sending extra keys are unaffected.
- "Any listing" means `GET /todos` and `GET /todos/overdue`. For consistency, the single todo returned by create and complete gets the same keys.
- `/todos/overdue` returns 200 with `[]` when nothing is overdue, ordered by id.
- The work stays within the paths in scope: `app/**`, `tests/**` and `README.md`.
- **Platform check.** The platform-contract skill was not found in this session. I judged deployability from the repo:
  - The manifest runs `uvicorn app.main:app`, probes `/health` and uses migrations `true`.
  - `preview.yml` deploys any `factory/**` push.
- **Tests not run.** The test suite could not be run during research because no dependencies are installed in the sandbox. That the existing tests pass today was inferred by reading them, not observed.

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Date-dependent tests are flaky around midnight UTC | medium | medium | The spec requires fixed far-past and far-future dates, or a clock the tests control. No sleeps. |
| `overdue` is stored at creation and goes stale | low | high | The flag is derived when each response is built. A test moves the clock and checks that the flag flips. |
| A future `GET /todos/{id}` route registered before `/todos/overdue` would shadow it | low | medium | No such route exists today. A test that calls `/todos/overdue` guards against regression. |
| The owner prefers datetime or local-time semantics | medium | low | The item is flagged for owner judgement, so the rule is confirmed before the spec is written. |

## Research basis

Two read-only codebase researchers reported on the current API and storage, and on build, tests and deployment.

Key facts:
- A todo is a dataclass serialised by `asdict` (`app/storage.py:10-17`), with keys `id`, `title`, `done` and `created_at`.
- There are no response models (`app/main.py:25-46`).
- There is no `GET /todos/{id}` route.
- The only clock call is `datetime.now(timezone.utc)` in a default factory (`app/storage.py:15`).
- Tests reset the in-memory store before and after each test.
