"""Server started with a non-UTC TZ must still judge overdue against the UTC date.
Usage: python probe_tz.py <base_url>   (server started with TZ=Etc/GMT-14 or TZ=Etc/GMT+12)"""
import json, sys, urllib.request
from datetime import datetime, timedelta, timezone
base = sys.argv[1]
def post(body):
    r = urllib.request.urlopen(urllib.request.Request(base + "/todos", json.dumps(body).encode(), {"Content-Type": "application/json"}))
    return json.loads(r.read())
utc = datetime.now(timezone.utc).date()
print("client UTC date", utc, "client local", datetime.now().date())
bad = 0
for due, want in ((utc - timedelta(days=1), True), (utc, False), (utc + timedelta(days=1), False)):
    t = post({"title": f"tz-{due}", "due_date": due.isoformat()})
    ok = t["overdue"] is want
    bad += not ok
    print("OK " if ok else "BAD", due, "overdue", t["overdue"], "want", want)
listed = json.loads(urllib.request.urlopen(base + "/todos").read())
print("GET /todos flags:", [(t["due_date"], t["overdue"]) for t in listed])
sys.exit(bad)
