# Usage: bash tz_probe.sh <repo_dir> <port> <TZ>
# Starts the service under the given TZ and runs the clock-sensitive e2e checks (client side computes UTC today).
source /tmp/venv312/bin/activate
cd "$1"
TZ="$3" python -m uvicorn app.main:app --host 127.0.0.1 --port "$2" --log-level warning > /tmp/tz_$2.log 2>&1 &
pid=$!
for i in $(seq 1 50); do curl -s -m 1 -o /dev/null http://127.0.0.1:$2/health && break; sleep 0.1; done
echo "server TZ=$3 at $1 (pid $pid)"
for c in boundary listing overdue-list; do python tests/e2e_overdue.py $c http://127.0.0.1:$2 | head -1 | cut -c1-200; done
# direct: create due = UTC today; it must not be overdue
today=$(date -u +%F)
curl -s -X POST -H 'Content-Type: application/json' -d "{\"title\":\"tz-due-utc-today\",\"due_date\":\"$today\"}" http://127.0.0.1:$2/todos; echo
kill $pid; wait $pid 2>/dev/null
