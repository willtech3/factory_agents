#!/usr/bin/env bash
# F-2 re-check: does the test plan catch a today_utc() that is not the UTC calendar date?
# Each mutant is a fresh clone of head under /tmp/f2m_<name> with app/storage.py:11 replaced; the plan's pytest,
# local-all and rollover-local run with TZ=UTC exported (the orchestrator's environment per the refutation condition).
# Usage: f2_local_clock_mutants.sh <venv-bin>
set -u
BIN="$1"
export TZ=UTC
echo "UTC now $(date -u +%FT%T); TZ=$TZ"
declare -A MUT=(
  [head]=''
  [date_today]='    return date.today()'
  [naive_now]='    return datetime.now().date()'
  [astimezone_local]='    return datetime.now(timezone.utc).astimezone().date()'
  [utcnow_correct]='    return datetime.utcnow().date()'
  [fromtimestamp_utc_correct]='    import time; return datetime.fromtimestamp(time.time(), timezone.utc).date()'
)
for name in head date_today naive_now astimezone_local utcnow_correct fromtimestamp_utc_correct; do
  d=/tmp/f2m_$name
  rm -rf "$d" && git clone -q /workspace/repo "$d" && (cd "$d" && git checkout -q 93ced45)
  if [ -n "${MUT[$name]}" ]; then
    python3 - "$d/app/storage.py" "${MUT[$name]}" <<'EOF'
import sys
p, repl = sys.argv[1], sys.argv[2]
s = open(p).read()
old = "    return datetime.now(timezone.utc).date()\n"
assert s.count(old) == 1
open(p, "w").write(s.replace(old, repl + "\n"))
EOF
  fi
  echo "=== mutant $name: storage.py:11 -> $(sed -n 11p "$d/app/storage.py")"
  cd "$d"
  "$BIN/python" -m pytest -q -p no:warnings 2>&1 | grep -E '^FAILED|passed|failed' | sed 's/^/  pytest: /'
  echo "  pytest exit ${PIPESTATUS[0]}"
  "$BIN/python" tests/e2e_overdue.py local-all 2>&1 | grep -E 'starting|FAIL|passed' | sed 's/^/  local-all: /'
  echo "  local-all exit ${PIPESTATUS[0]}"
  PYTHONPATH=. "$BIN/python" tests/e2e_overdue.py rollover-local 2>&1 | tail -1 | sed 's/^/  rollover-local: /'
  cd /
done
