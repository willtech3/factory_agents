"""Probe: can a single todo response carry done=false with overdue=false for a past-due todo?

Todo.to_dict() (app/storage.py:27-28) reads `done` twice without the store lock: once in asdict(self), once in
is_overdue(). The POST /todos route (app/main.py:50) calls it outside the lock, so a concurrent
POST /todos/{id}/complete can flip `done` between the two reads.

Part A drives the exact objects the routes use (store.create(...).to_dict() vs store.complete(id)) in two threads.
Part B does the same over HTTP against a live server (python probe_race_post_response.py <base_url>).
Run from the repo root with PYTHONPATH=.
"""
from __future__ import annotations

import json
import sys
import threading
import urllib.request
from datetime import date

PAST = date(2000, 1, 1)


def torn(d: dict) -> bool:
    return d["due_date"] == "2000-01-01" and d["done"] is False and d["overdue"] is False


def part_a(rounds: int = 20000) -> int:
    from app.storage import TodoStore

    sys.setswitchinterval(1e-6)
    store = TodoStore()
    hits = 0
    for _ in range(rounds):
        next_id = store._next  # the id the creator is about to get (ids are sequential and guessable)
        result: dict = {}
        go = threading.Event()

        def creator():
            go.wait()
            result["d"] = store.create("race", PAST).to_dict()

        def completer():
            go.wait()
            while store.complete(next_id) is None:
                pass

        ts = [threading.Thread(target=creator), threading.Thread(target=completer)]
        [t.start() for t in ts]
        go.set()
        [t.join() for t in ts]
        if torn(result["d"]):
            hits += 1
            if hits == 1:
                print("torn response example:", result["d"])
    print(f"part A (in-process, switchinterval 1us): {hits} torn responses in {rounds} rounds")
    return hits


def part_b(base: str, rounds: int = 3000) -> int:
    def call(method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    hits = 0
    for _ in range(rounds):
        ids = [t["id"] for t in call("GET", "/todos")[1]]
        guess = (max(ids) if ids else 0) + 1
        out = {}
        go = threading.Event()

        def creator():
            go.wait()
            out["d"] = call("POST", "/todos", {"title": "race-http", "due_date": "2000-01-01"})[1]

        def completer():
            go.wait()
            for _ in range(50):
                if call("POST", f"/todos/{guess}/complete")[0] == 200:
                    return

        ts = [threading.Thread(target=creator), threading.Thread(target=completer)]
        [t.start() for t in ts]
        go.set()
        [t.join() for t in ts]
        if out.get("d") and torn(out["d"]):
            hits += 1
            if hits == 1:
                print("torn HTTP response example:", out["d"])
    print(f"part B (HTTP {base}): {hits} torn responses in {rounds} rounds")
    return hits


if __name__ == "__main__":
    import urllib.error  # noqa: F401

    a = part_a()
    b = part_b(sys.argv[1].rstrip("/")) if len(sys.argv) > 1 else 0
    sys.exit(0 if (a == 0 and b == 0) else 1)
