"""Live adversarial probe against a running instance. Usage: python probe_live.py BASE_URL [seconds]

1. Concurrency: writer threads create (past / UTC-yesterday / UTC-today / future / null / omitted), race to complete
   the id about to be handed out, complete their own todos and delete some; reader threads hammer GET /todos and
   GET /todos/overdue. EVERY response (POST, complete, both lists) is checked for: exact key order, the overdue rule
   against the UTC date (a date change mid-run is tolerated), and for lists ascending unique ids and, for the
   overdue list, only open overdue todos.
2. At rest: GET /todos/overdue == [t for t in GET /todos if t.overdue].
3. Edge inputs: malformed due_date values beyond AC-9 (422 and nothing stored), spoofed server fields, duplicate
   JSON keys, replayed complete, 404 / 422 paths.
"""
import json
import random
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone

URL = sys.argv[1].rstrip("/")
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 8
KEYS = ["id", "title", "done", "created_at", "due_date", "overdue"]


def call(method, path, body=None, raw=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            s, b = r.status, r.read()
    except urllib.error.HTTPError as e:
        s, b = e.code, e.read()
    try:
        return s, json.loads(b) if b else None
    except ValueError:
        return s, b


def utc_today():
    return datetime.now(timezone.utc).date()


def rule(t, today):
    return t["done"] is False and t["due_date"] is not None and date.fromisoformat(t["due_date"]) < today


problems, counts, lock = [], {"writes": 0, "reads": 0, "post_done_true": 0}, threading.Lock()


def bad(msg):
    with lock:
        if len(problems) < 30:
            problems.append(msg)


def check_todo(t, ctx, before, after):
    if not isinstance(t, dict) or list(t) != KEYS:
        return bad(f"{ctx}: keys {t!r}")
    if t["done"] and t["overdue"]:
        bad(f"{ctx}: done and overdue {t}")
    if before == after and t["overdue"] is not rule(t, before):
        bad(f"{ctx}: rule broken for UTC {before}: {t}")


def check_list(path, todos, before, after):
    ids = [t["id"] for t in todos]
    if ids != sorted(set(ids)):
        bad(f"GET {path}: ids not ascending/unique")
    for t in todos:
        check_todo(t, f"GET {path}", before, after)
        if path.endswith("overdue") and not (t["overdue"] is True and t["done"] is False):
            bad(f"GET {path}: non-overdue element {t}")


def writer(stop, seed):
    rnd = random.Random(seed)
    while not stop.is_set():
        today = utc_today()
        due = rnd.choice(["2000-01-01", (today - timedelta(days=1)).isoformat(), today.isoformat(),
                          "2999-12-31", None, "omit"])
        body = {"title": f"probe-{seed}"} if due == "omit" else {"title": f"probe-{seed}", "due_date": due}
        _, listing = call("GET", "/todos") if rnd.random() < 0.05 else (None, None)
        if listing:
            nxt = max(t["id"] for t in listing) + 1
            threading.Thread(target=call, args=("POST", f"/todos/{nxt}/complete")).start()
        before = utc_today()
        s, t = call("POST", "/todos", body)
        after = utc_today()
        if s != 201:
            bad(f"POST {body}: {s} {t}")
            continue
        check_todo(t, "POST /todos", before, after)
        with lock:
            counts["writes"] += 1
            counts["post_done_true"] += t["done"]
        r = rnd.random()
        if r < 0.5:
            before = utc_today()
            s, c = call("POST", f"/todos/{t['id']}/complete")
            after = utc_today()
            if s != 200:
                bad(f"complete {t['id']}: {s}")
            else:
                check_todo(c, "POST complete", before, after)
                if c["done"] is not True or c["overdue"] is not False:
                    bad(f"complete response {c}")
        elif r < 0.6:
            s, _ = call("DELETE", f"/todos/{t['id']}")
            if s != 204:
                bad(f"delete {t['id']}: {s}")


def reader(stop, path):
    while not stop.is_set():
        before = utc_today()
        s, todos = call("GET", path)
        after = utc_today()
        if s != 200 or not isinstance(todos, list):
            bad(f"GET {path}: {s}")
            continue
        check_list(path, todos, before, after)
        with lock:
            counts["reads"] += 1


stop = threading.Event()
threads = [threading.Thread(target=writer, args=(stop, i)) for i in range(6)]
threads += [threading.Thread(target=reader, args=(stop, p)) for p in ("/todos", "/todos/overdue") * 3]
for t in threads:
    t.start()
time.sleep(SECONDS)
stop.set()
for t in threads:
    t.join()
time.sleep(0.5)
print(f"[1] concurrency {SECONDS}s: {counts}, problems={len(problems)}")
for p in problems:
    print("   ", p)

# 2. at rest
for _ in range(3):
    today = utc_today()
    _, all_todos = call("GET", "/todos")
    _, overdue = call("GET", "/todos/overdue")
    if utc_today() == today:
        break
flagged = [t for t in all_todos if t["overdue"]]
print(f"[2] at rest: GET /todos/overdue == flagged subset of GET /todos: {overdue == flagged} "
      f"({len(overdue)} of {len(all_todos)})")

# 3. edge inputs
extra_bad = ["2026-11-30\r", " 2026-11-30", "\t2026-11-30", "2026-1-30", "2026-11-3", "+2026-11-30", "2026-11-30Z",
             "２０２６-１１-３０", "٢٠٢٦-١١-٣٠", "2026-11-30\x00", "\ufeff2026-11-30", "2026-11-31", "2100-02-29",
             "2026-00-10", "2026-10-00", "null", "None", "x" * 100000, {}, {"y": 2026}, -1, 1e20, False, [],
             "2026/11/30", "30-11-2026", "2026-W48-1", "2026-334", "10000-01-01", "2026-11-30T00:00"]
_, before = call("GET", "/todos")
tags, fails = [], []
for v in extra_bad:
    tag = f"edge-{uuid.uuid4()}"
    s, b = call("POST", "/todos", {"title": tag, "due_date": v})
    tags.append(tag)
    if s != 422:
        fails.append((repr(v)[:40], s, b))
_, after = call("GET", "/todos")
leaked = [t for t in after if t["title"] in tags]
print(f"[3a] {len(extra_bad)} extra malformed values: non-422={fails} leaked={leaked} "
      f"count before/after={len(before)}/{len(after)}")

s, t = call("POST", "/todos", {"title": "spoof", "due_date": "2000-01-01", "overdue": False, "done": True,
                               "id": 1, "created_at": "x"})
print(f"[3b] spoofed server fields: {s} done={t['done']} overdue={t['overdue']} id_is_server={t['id'] != 1 or len(after) == 0} "
      f"created_at_is_server={t['created_at'] != 'x'}")
s, t2 = call("POST", "/todos", raw=b'{"title":"dup","due_date":"2000-01-01","due_date":"2026-02-30"}')
s3, t3 = call("POST", "/todos", raw=b'{"title":"dup","due_date":"2026-02-30","due_date":"2000-01-01"}')
print(f"[3c] duplicate due_date keys (last invalid) -> {s}; (last valid) -> {s3} {t3 and t3.get('due_date')}")
s1, c1 = call("POST", f"/todos/{t['id']}/complete")
s2, c2 = call("POST", f"/todos/{t['id']}/complete")
print(f"[3d] replayed complete: {s1}/{s2} identical={c1 == c2} overdue={c2['overdue']} done={c2['done']}")
print(f"[3e] unknown id complete -> {call('POST', '/todos/999999999/complete')[0]}; non-int -> "
      f"{call('POST', '/todos/abc/complete')[0]}; GET /todos/overdue?x=1 -> {call('GET', '/todos/overdue?x=1')[0]}; "
      f"POST /todos/overdue -> {call('POST', '/todos/overdue')[0]}; GET /todos/1 -> {call('GET', '/todos/1')[0]}")
s, b = call("POST", "/todos", {"title": "", "due_date": "2026-02-30"})
print(f"[3f] empty title + bad date -> {s}, error locs {[e['loc'] for e in b['detail']]}")
s, b = call("POST", "/todos", raw=b"not json")
print(f"[3g] non-JSON body -> {s}")
print("PROBLEMS" if problems or fails or leaked or overdue != flagged else "OK: no violations")
