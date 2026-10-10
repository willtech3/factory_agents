#!/usr/bin/env bash
# F-3 re-check: is the old d33b244 still reachable from the branch, and does every commit in main..HEAD reject the
# AC-9 values that d33b244 accepted? Also runs the whole test plan at each commit (slice greenness).
# usage: f3_every_commit.sh <repo_with_main_and_head> <venv_bin>
set -u
REPO=$1
BIN=$2
PROBES=$(cd "$(dirname "$0")" && pwd)
cd "$REPO"
for old in d33b244 23128cd 1ad4332; do
  if git merge-base --is-ancestor "$old" HEAD; then echo "$old IS an ancestor of HEAD"; else echo "$old is NOT an ancestor of HEAD"; fi
done
echo "tree 23128cd=$(git rev-parse 23128cd^{tree}) 5fd5533=$(git rev-parse 5fd5533^{tree})"
echo "tree 1ad4332=$(git rev-parse 1ad4332^{tree}) 7a5bfd4=$(git rev-parse 7a5bfd4^{tree})"
for c in d33b244 $(git rev-list --reverse main..HEAD); do
  wt=/tmp/f3_wt_$(git rev-parse --short "$c")
  rm -rf "$wt"; git worktree prune; git worktree add -q --detach "$wt" "$c"
  echo "##### $(git log --oneline -1 "$c")"
  (cd "$wt" && PYTHONPATH=. "$BIN/python" - <<'EOF'
from fastapi.testclient import TestClient
from app.main import app, store
c = TestClient(app)
for v in ["2026-11-30T00:00:00", 0, 1700000000, "2026-11-30T17:00:00Z", "2027-02-29", True]:
    r = c.post("/todos", json={"title": "x", "due_date": v})
    print(f"    {v!r} -> {r.status_code} {r.json().get('due_date') if r.status_code == 201 else ''}")
print(f"    stored todos: {len(store.list())}")
EOF
  ) 2>&1 | grep -v Deprecation | grep -v "from starlette"
  if [ "$c" != d33b244 ]; then
    (cd "$wt" && git branch -f main 6dec694 >/dev/null 2>&1; bash "$PROBES/run_test_plan.sh" "$wt" "$BIN" 2>&1 | grep -E "^=== \[[0-9]+\] exit|ALL_EXIT|passed|failed" | tr '\n' ' '; echo)
  fi
done
