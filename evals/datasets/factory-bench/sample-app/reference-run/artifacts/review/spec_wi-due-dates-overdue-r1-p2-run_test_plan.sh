#!/usr/bin/env bash
# Runs spec_wi-due-dates-overdue test_plan.commands verbatim, one by one, in a scratch clone at head.
cd "${1:-/tmp/rv}" || exit 2
source /tmp/venv312/bin/activate
python - <<'PY' > /tmp/cmds.txt
import json
for c in json.load(open('/mnt/session/uploads/spec.json'))['test_plan']['commands']:
    print(c.replace('\n','\\n'))
PY
i=0
while IFS= read -r cmd; do
  i=$((i+1))
  out=$(bash -c "$cmd" 2>&1); rc=$?
  echo "=== [$i] rc=$rc :: $cmd"
  echo "$out" | tail -6
done < /tmp/cmds.txt
