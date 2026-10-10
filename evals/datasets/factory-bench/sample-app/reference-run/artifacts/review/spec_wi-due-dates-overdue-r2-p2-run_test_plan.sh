#!/usr/bin/env bash
# Run every spec test_plan.commands entry verbatim in a checkout. Usage: run_test_plan.sh <checkout> <venv-bin>
set -u
cd "$1"
export PATH="$2:$PATH"
echo "== checkout $(git rev-parse --short HEAD) python $(python --version 2>&1) TZ=${TZ:-<unset>} UTC $(date -u +%FT%T)"
i=0
while IFS= read -r cmd; do
  i=$((i+1))
  echo "---- [$i] $cmd"
  bash -c "$cmd" 2>&1 | tail -15
  echo "---- [$i] exit ${PIPESTATUS[0]}"
done < <(python - <<'EOF'
import json
for c in json.load(open('/mnt/session/uploads/spec.json'))['test_plan']['commands']:
    print(c)
EOF
)
