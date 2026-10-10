#!/usr/bin/env bash
# Mutation probe for the round-2 hunks (F-1 and F-2 fixes).
# Each mutant is a fresh copy of the head checkout under /tmp/mut_<name> with one fault applied by a python
# one-liner; then the plan's pytest, local-all and rollover-local run with TZ=UTC (as on the orchestrator), and for
# to_dict mutants the in-process race probe runs too.
# usage: mutants_round2.sh <head_checkout> <venv_bin>
set -u
HEAD_DIR=$1
BIN=$2
PROBES=$(cd "$(dirname "$0")" && pwd)

declare -A MUT
MUT[today_date_today]='s.replace("return datetime.now(timezone.utc).date()", "return date.today()")'
MUT[today_naive_now]='s.replace("return datetime.now(timezone.utc).date()", "return datetime.now().date()")'
MUT[today_astimezone_local]='s.replace("return datetime.now(timezone.utc).date()", "return datetime.now(timezone.utc).astimezone().date()")'
MUT[today_fromtimestamp_local]='s.replace("return datetime.now(timezone.utc).date()", "return datetime.fromtimestamp(datetime.now(timezone.utc).timestamp()).date()")'
MUT[todict_no_copy]='s.replace("snapshot = replace(self)", "snapshot = self")'
MUT[todict_overdue_first]='s.replace("        snapshot = replace(self)  # done is read once, so a concurrent complete() cannot split done from overdue\n        return {**asdict(snapshot), \"overdue\": snapshot.is_overdue(today)}", "        overdue = self.is_overdue(today)\n        return {**asdict(self), \"overdue\": overdue}")'

for name in today_date_today today_naive_now today_astimezone_local today_fromtimestamp_local todict_no_copy todict_overdue_first; do
  dir=/tmp/mut_$name
  rm -rf "$dir"; cp -r "$HEAD_DIR" "$dir"; rm -rf "$dir/.git"
  "$BIN/python" - "$dir/app/storage.py" "${MUT[$name]}" <<'EOF'
import sys
p, expr = sys.argv[1], sys.argv[2]
s = open(p).read()
n = eval(expr)
assert n != s, "mutation did not apply"
open(p, "w").write(n)
EOF
  echo "##### mutant $name"
  (cd "$dir" && diff <(cd "$HEAD_DIR" && cat app/storage.py) app/storage.py | sed 's/^/    /')
  (cd "$dir" && TZ=UTC "$BIN/python" -m pytest -q 2>&1 | grep -E "passed|failed|FAILED" | sed 's/^/    pytest: /')
  (cd "$dir" && TZ=UTC "$BIN/python" tests/e2e_overdue.py local-all 2>&1 | grep -vE "^PASS" | sed 's/^/    local-all: /')
  (cd "$dir" && TZ=UTC PYTHONPATH=. "$BIN/python" tests/e2e_overdue.py rollover-local 2>&1 | grep -E "passed|FAIL" | sed 's/^/    rollover-local: /')
  case $name in todict_*)
    (cd "$dir" && PYTHONPATH=. timeout 200 "$BIN/python" "$PROBES/probe_race_post_response.py" 20000 2>&1 | sed 's/^/    race probe: /');;
  esac
done
