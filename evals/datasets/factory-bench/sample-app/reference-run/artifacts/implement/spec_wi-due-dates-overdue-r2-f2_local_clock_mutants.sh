#!/usr/bin/env bash
# Review finding F-2: does any test_plan command fail when app.storage.today_utc returns the host-local date?
# For HEAD unmodified and for each mutant, clone the branch head to /tmp, apply the mutant, and run the plan's
# pytest, local-all and rollover-local with TZ=UTC (the orchestrator's environment).
# Usage: f2_local_clock_mutants.sh <venv-bin-dir> [ref]
export PATH="$1:$PATH"
REF="${2:-HEAD}"
export TZ=UTC
REPO=/workspace/repo
SHA=$(git -C "$REPO" rev-parse --short "$REF")
echo "UTC now: $(date -u +%FT%T)  ref: $REF ($SHA)  python: $(python --version)"
for name in head 'date.today()' 'datetime.now().date()'; do
  dir=/tmp/f2_mutant_$RANDOM
  git clone -q "$REPO" "$dir" && git -C "$dir" checkout -q "$SHA"
  if [ "$name" != head ]; then
    python - "$dir/app/storage.py" "$name" <<'EOF'
import sys
path, expr = sys.argv[1], sys.argv[2]
src = open(path).read()
old = "    return datetime.now(timezone.utc).date()\n"
assert src.count(old) == 1
open(path, "w").write(src.replace(old, f"    return {expr}\n"))
EOF
  fi
  echo "--- today_utc() body: $(grep -A1 '^def today_utc' "$dir/app/storage.py" | tail -1 | sed 's/^ *//')"
  (cd "$dir" && python -m pytest -q 2>&1 | grep -E "^FAILED|passed|failed" | tail -4; echo "pytest exit ${PIPESTATUS[0]}")
  (cd "$dir" && python tests/e2e_overdue.py local-all 2>&1 | grep -E "^starting|^FAIL|passed" | cut -c1-220; echo "local-all exit ${PIPESTATUS[0]}")
  (cd "$dir" && PYTHONPATH=. python tests/e2e_overdue.py rollover-local 2>&1 | tail -1; echo "rollover-local exit ${PIPESTATUS[0]}")
  rm -rf "$dir"
done
