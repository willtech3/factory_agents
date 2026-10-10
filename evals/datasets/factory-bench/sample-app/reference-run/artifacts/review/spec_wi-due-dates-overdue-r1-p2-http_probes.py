"""Reviewer probes against a running instance (manifest entrypoint). Usage: python http_probes.py http://127.0.0.1:8000

Prints one line per probe: PROBE <name> -> <status> <short body>. Exits 0; the reviewer reads the lines.
"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/")


def call(method, path, body=None, raw=None, headers=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    h = {"Content-Type": "application/json"} if data is not None else {}
    h.update(headers or {})
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            status, text = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read()
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text.decode(errors="replace")


def count():
    return len(call("GET", "/todos")[1])


def show(name, status, body):
    s = json.dumps(body) if not isinstance(body, str) else body
    print(f"PROBE {name} -> {status} {s[:220]}")


# 1. Extra malformed due_date values not in the AC-9 list. Every one should be 422 and store nothing.
extra_bad = {
    "cr": "2026-11-30\r", "lead-newline": "\n2026-11-30", "lead-space": " 2026-11-30", "short-month": "2026-1-30",
    "short-day": "2026-11-3", "plus-year": "+2026-11-30", "iso-week": "2026-W48-1", "ordinal": "2026-334",
    "z-suffix": "2026-11-30Z", "offset": "2026-11-30+00:00", "fullwidth-digits": "\uff12\uff10\uff12\uff16-11-30",
    "arabic-indic": "\u0662\u0660\u0662\u0666-\u0661\u0661-\u0663\u0660", "month-00": "2026-00-10", "day-00": "2026-11-00",
    "nov-31": "2026-11-31", "1900-02-29": "1900-02-29", "2100-02-29": "2100-02-29", "nul": "2026-11-30\u0000",
    "slashes": "2026/11/30", "5-digit-year": "12026-11-30", "obj": {"year": 2026}, "neg-int": -1, "big-int": 10**30,
    "nan-str": "NaN", "bytes-like": "MjAyNi0xMS0zMA==", "huge": "2026-11-30" * 100000,
}
before = count()
for name, value in extra_bad.items():
    st, body = call("POST", "/todos", {"title": f"probe-bad-{name}", "due_date": value})
    show(f"bad:{name}", st, body if st != 422 else "(422)")
after = count()
print(f"PROBE bad-values-stored-nothing -> before={before} after={after}")

# 2. Edge-valid dates.
for v in ["2000-02-29", "0001-01-01", "9999-12-31", "2028-02-29"]:
    st, body = call("POST", "/todos", {"title": f"probe-ok-{v}", "due_date": v})
    show(f"ok:{v}", st, body)

# 3. Client cannot set server-owned fields; unknown fields still ignored.
st, body = call("POST", "/todos", {"title": "probe-spoof", "overdue": True, "done": True, "id": 999999,
                                   "created_at": "x", "due_date": None})
show("spoof-server-fields", st, body)
st, body = call("POST", "/todos", {"title": "probe-spoof2", "overdue": False, "due_date": "2000-01-01"})
show("spoof-overdue-false-on-past", st, body)

# 4. Title validation unchanged, and combined errors (bad title + bad date) still 422.
show("empty-title+good-date", *call("POST", "/todos", {"title": "", "due_date": "2026-11-30"}))
show("empty-title+bad-date", *call("POST", "/todos", {"title": "", "due_date": "2026-02-30"}))
show("missing-title+date", *call("POST", "/todos", {"due_date": "2026-11-30"}))
show("title-201+date", *call("POST", "/todos", {"title": "x" * 201, "due_date": "2026-11-30"}))
show("body-array", *call("POST", "/todos", [{"title": "a"}]))
show("body-not-json", *call("POST", "/todos", raw=b"due_date=2026-11-30&title=a"))

# 5. Other methods / shapes on /todos/overdue: no shadowing, no new writes.
for m in ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]:
    st, body = call(m, "/todos/overdue")
    show(f"{m} /todos/overdue", st, body)
show("GET /todos/overdue?done=true", *call("GET", "/todos/overdue?done=true&x=1"))
st, body = call("GET", "/todos/overdue/")
show("GET /todos/overdue/ (trailing slash)", st, type(body).__name__)
show("GET /todos/overdue/complete", *call("GET", "/todos/overdue/complete"))
show("POST /todos/overdue/complete", *call("POST", "/todos/overdue/complete"))

# 6. Consistency: overdue list == [t for t in GET /todos if t.overdue], and complete/404 behaviour.
lst = call("GET", "/todos")[1]
ov = call("GET", "/todos/overdue")[1]
print(f"PROBE overdue-list-equals-filtered-list -> {ov == [t for t in lst if t['overdue']]} (n_list={len(lst)}, n_overdue={len(ov)})")
print(f"PROBE every-key-order -> {all(list(t) == ['id','title','done','created_at','due_date','overdue'] for t in lst)}")
# replayed complete is idempotent
t = call("POST", "/todos", {"title": "probe-replay", "due_date": "2000-01-01"})[1]
r1 = call("POST", f"/todos/{t['id']}/complete")
r2 = call("POST", f"/todos/{t['id']}/complete")
show("complete-replay-1", *r1)
show("complete-replay-2", *r2)
show("complete-negative-id", *call("POST", "/todos/-1/complete"))
show("complete-non-int-id", *call("POST", "/todos/abc/complete"))
st, _ = call("DELETE", f"/todos/{t['id']}")
show("delete-overdue-then-overdue-list-has-it", 200, any(x["id"] == t["id"] for x in call("GET", "/todos/overdue")[1]))

# 7. OpenAPI still builds and documents due_date.
st, spec = call("GET", "/openapi.json")
schema = spec["components"]["schemas"]["TodoIn"] if st == 200 else None
show("openapi TodoIn", st, schema)
print("PROBE openapi paths ->", sorted(spec["paths"]) if st == 200 else st)
