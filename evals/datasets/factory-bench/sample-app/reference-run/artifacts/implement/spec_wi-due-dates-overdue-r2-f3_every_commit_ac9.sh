#!/usr/bin/env bash
# Review finding F-3: at every commit in main..<ref>, POST /todos must reject due_date "2026-11-30T00:00:00" and 0
# with 422 (the reviewer's reproduction), and the plan's pytest must pass. Pass extra refs (e.g. d33b244) to compare.
# Usage: f3_every_commit_ac9.sh <venv-bin-dir> [ref] [extra-ref...]
export PATH="$1:$PATH"
REF="${2:-HEAD}"
shift 2 2>/dev/null
REPO=/workspace/repo
for sha in $(git -C "$REPO" rev-list --reverse "main..$REF") "$@"; do
  dir=/tmp/f3_commit_$RANDOM
  git clone -q "$REPO" "$dir" && git -C "$dir" checkout -q "$sha"
  echo "--- $(git -C "$dir" log --format='%h %s' -1 | cut -c1-110)"
  (cd "$dir" && PYTHONPATH=. python -c "
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
for v in ['2026-11-30T00:00:00', 0]:
    r = c.post('/todos', json={'title': 'x', 'due_date': v})
    print('  POST due_date', repr(v), '->', r.status_code, r.json().get('due_date') if r.status_code == 201 else '')
" 2>&1 | grep -v Warning | grep -v 'from starlette')
  (cd "$dir" && python -m pytest -q 2>&1 | tail -1 | sed 's/^/  pytest: /')
  rm -rf "$dir"
done
