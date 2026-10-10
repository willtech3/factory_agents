"""Live adversarial probes against a running instance (stdlib only).

usage: python probe_live.py <base_url>
Prints one line per probe and a final 'bad=N'. A probe is 'bad' when the service contradicts the spec.
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

BASE = sys.argv[1].rstrip("/")
KEYS = ["id", "title", "done", "created_at", "due_date", "overdue"]
bad = 0


def call(method, path, body=None, raw=None, headers=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    h = dict(headers or {})
    if data is not None and "Content-Type" not in h:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            status, text = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read()
    try:
        return status, json.loads(text) if text else None
    except ValueError:
        return status, text.decode(errors="replace")


def report(ok, label, detail=""):
    global bad
    if not ok:
        bad += 1
    print(("ok   " if ok else "BAD  ") + label + (f" :: {detail}" if detail else ""))


def utc_today():
    return datetime.now(timezone.utc).date()


def rule(t, today):
    return t["done"] is False and t["due_date"] is not None and date.fromisoformat(t["due_date"]) < today


def titles():
    return {t["title"] for t in call("GET", "/todos")[1]}


# 1. malformed values beyond the AC-9 list: must be 422 and must store nothing
extra_bad = ["2026-11-30\r", " 2026-11-30", "\t2026-11-30", "2026-1-30", "2026-11-3", "+2026-11-30", "2026-11-30Z",
             "2026-11-30\x00", "２０２６-１１-３０", "٢٠٢٦-١١-٣٠", "2026–11–30", "\ufeff2026-11-30", "2100-02-29",
             "2026-00-10", "2026-10-00", "2026-04-31", "null", "None", "2026-11-30\n\n", "2026/11/30", "30-11-2026",
             "2026-W48-1", "2026-334", "12026-11-30", "2026-11-30+00:00", "x" * 100_000, {}, {"y": 2026}, -1, 1e20,
             False, [], "1900-02-29", "0000-00-00"]
n_before = len(call("GET", "/todos")[1])
for i, v in enumerate(extra_bad):
    title = f"probe-bad-{i}"
    s, b = call("POST", "/todos", {"title": title, "due_date": v})
    report(s == 422, f"malformed {v!r:.40} -> {s}")
n_after = len(call("GET", "/todos")[1])
report(n_before == n_after, f"nothing stored by malformed probes (before {n_before}, after {n_after})")

# 2. accepted edge dates echo exactly; overdue matches the rule
for v, want in [("2000-02-29", True), ("2028-02-29", False), ("0001-01-01", True), ("9999-12-31", False),
                ((utc_today() - timedelta(days=1)).isoformat(), True), (utc_today().isoformat(), False)]:
    s, b = call("POST", "/todos", {"title": f"probe-ok-{v}", "due_date": v})
    report(s == 201 and b["due_date"] == v and b["overdue"] is want and list(b) == KEYS,
           f"accepted {v} -> {s} overdue={b.get('overdue') if isinstance(b, dict) else b}")

# 3. server-owned fields cannot be spoofed; unknown fields ignored
s, b = call("POST", "/todos", {"title": "probe-spoof", "due_date": "2000-01-01", "overdue": False, "done": True,
                               "id": 999999, "created_at": "x"})
report(s == 201 and b["overdue"] is True and b["done"] is False and b["id"] != 999999 and b["created_at"] != "x"
       and list(b) == KEYS, f"spoofed fields ignored -> {b}")

# 4. title rules unchanged, and combined errors
for body, want in [({"title": ""}, 422), ({"title": "a" * 201}, 422), ({"title": "a" * 200}, 201), ({}, 422),
                   ({"title": "", "due_date": "bad"}, 422), ({"due_date": "2026-11-30"}, 422)]:
    s, _ = call("POST", "/todos", body)
    report(s == want, f"title probe {str(body)[:50]} -> {s}, want {want}")
s, _ = call("POST", "/todos", raw=b"not json")
report(s == 422, f"non-JSON body -> {s}")
s, _ = call("POST", "/todos", raw=b'["2026-11-30"]')
report(s == 422, f"array body -> {s}")
s, b = call("POST", "/todos", raw=b'{"title":"probe-dupkey","due_date":"2026-11-30","due_date":"2026-13-01"}')
report(s == 422, f"duplicate due_date key, last invalid -> {s}")

# 5. routing around /todos/overdue
for m, p, want in [("GET", "/todos/overdue?x=1", 200), ("POST", "/todos/overdue", 405), ("GET", "/todos/1", 405),
                   ("GET", "/openapi.json", 200), ("DELETE", "/todos/overdue", 422),
                   ("POST", "/todos/overdue/complete", 422)]:
    s, _ = call(m, p)
    report(s == want, f"{m} {p} -> {s}, want {want} (base behaviour for the last two)")

# 6. complete: replay idempotent, 404 for unknown and negative id
s, t = call("POST", "/todos", {"title": "probe-complete", "due_date": "2000-01-01"})
s1, c1 = call("POST", f"/todos/{t['id']}/complete")
s2, c2 = call("POST", f"/todos/{t['id']}/complete")
report(s1 == s2 == 200 and c1 == c2 and c1["overdue"] is False and c1["done"] is True, "replayed complete idempotent")
report(call("POST", "/todos/123456789/complete")[0] == 404 and call("POST", "/todos/-1/complete")[0] == 404,
       "unknown / negative id complete -> 404")
s, t = call("POST", "/todos", {"title": "probe-delete", "due_date": "2000-01-01"})
call("DELETE", f"/todos/{t['id']}")
report(t["id"] not in [x["id"] for x in call("GET", "/todos/overdue")[1]], "deleted overdue todo leaves the list")

# 7. HTTP race: POST past-due todo while another thread completes the id about to be handed out
torn = []
stop = threading.Event()
next_id = [max([x["id"] for x in call("GET", "/todos")[1]], default=0) + 1]


def completer():
    while not stop.is_set():
        i = next_id[0]
        for j in (i, i + 1):
            s, b = call("POST", f"/todos/{j}/complete")
            if s == 200 and b["overdue"] is not (not b["done"] and rule(b, utc_today())):
                torn.append(("complete", b))


threads = [threading.Thread(target=completer) for _ in range(3)]
for th in threads:
    th.start()
for _ in range(600):
    s, b = call("POST", "/todos", {"title": "probe-race", "due_date": "2000-01-01"})
    next_id[0] = b["id"] + 1
    if b["overdue"] is not (not b["done"]):
        torn.append(("post", b))
stop.set()
for th in threads:
    th.join()
report(not torn, f"HTTP POST/complete race: {len(torn)} torn responses in 600 POSTs", str(torn[:1]))

# 8. readers vs writers: every list response obeys order, shape and the rule; overdue list holds only open overdue
errors = []
stop = threading.Event()
counts = {"reads": 0, "writes": 0}


def writer(k):
    i = 0
    dues = ["2000-01-01", None, "2999-12-31"]
    while not stop.is_set():
        due = dues[i % 3] if i % 4 else (utc_today() - timedelta(days=1)).isoformat()
        s, b = call("POST", "/todos", {"title": f"probe-w{k}", "due_date": due})
        if i % 2:
            call("POST", f"/todos/{b['id']}/complete")
        if i % 10 == 0:
            call("DELETE", f"/todos/{b['id']}")
        counts["writes"] += 1
        i += 1


def reader(path):
    while not stop.is_set():
        today = utc_today()
        s, lst = call("GET", path)
        if utc_today() != today:
            continue
        ids = [t["id"] for t in lst]
        if s != 200 or ids != sorted(set(ids)):
            errors.append((path, "order/status"))
        for t in lst:
            if list(t) != KEYS or t["overdue"] is not rule(t, today):
                errors.append((path, t))
            if path == "/todos/overdue" and not (t["overdue"] is True and t["done"] is False):
                errors.append((path, t))
        counts["reads"] += 1


ths = [threading.Thread(target=writer, args=(k,)) for k in range(5)]
ths += [threading.Thread(target=reader, args=(p,)) for p in ("/todos", "/todos/overdue") for _ in range(3)]
for th in ths:
    th.start()
threading.Event().wait(6)
stop.set()
for th in ths:
    th.join()
report(not errors, f"concurrent readers/writers: reads={counts['reads']} writes={counts['writes']} errors={len(errors)}",
       str(errors[:2]))

# 9. at rest: overdue list == overdue-flagged subset of GET /todos
today = utc_today()
all_t = call("GET", "/todos")[1]
od = call("GET", "/todos/overdue")[1]
report(utc_today() != today or od == [t for t in all_t if t["overdue"]],
       f"at rest: GET /todos/overdue equals flagged subset of GET /todos ({len(od)} of {len(all_t)})")

print(f"bad={bad}")
