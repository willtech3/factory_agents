#!/usr/bin/env bash
# Runs every spec test_plan.commands entry, in order, from a clean clone with `main` = base sha.
# Usage: run_test_plan.sh <repo_dir>   (PATH must put the review venv's python first)
cd "$1" || exit 2
i=0
while IFS= read -r cmd; do
  i=$((i+1))
  echo "=== [$i] $cmd"
  out=$(bash -c "$cmd" 2>&1); rc=$?
  echo "$out" | tail -15
  echo "=== [$i] exit=$rc"
done < <(python -c "import json; [print(c) for c in json.load(open('/mnt/session/uploads/spec.json'))['test_plan']['commands']]")
