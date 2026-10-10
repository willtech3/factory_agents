"""F-3 re-check: at the current checkout, POST every AC-9 malformed value (plus 2027-02-29) through TestClient
and report any that is not 422 or that stores a todo. Run with PYTHONPATH=<checkout>."""
import subprocess

from fastapi.testclient import TestClient

from app.main import app, store

VALUES = ["2026-13-01", "2026-02-30", "0000-01-01", "not-a-date", "", "20261130", "2026-11-30T17:00:00Z",
          "2026-11-30T00:00:00", "2026-11-30 ", "2026-11-30\n", 20261130, 0, 1700000000, 1.5, True, ["2026-11-30"],
          "2027-02-29"]
store.clear()
c = TestClient(app)
bad = []
for v in VALUES:
    r = c.post("/todos", json={"title": "x", "due_date": v})
    if r.status_code != 422:
        bad.append((v, r.status_code, r.json()))
stored = c.get("/todos").json()
ok = [c.post("/todos", json={"title": "ok", "due_date": v}).status_code for v in ("2028-02-29", None)]
sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
print(f"{sha}: non-422={bad} stored_after_malformed={len(stored)} accepted(2028-02-29,null)={ok}")
