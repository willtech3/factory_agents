"""F-1 re-check (part A, in-process): the calls the routes make, raced.

Main thread: store.create('race', date(2000, 1, 1)).to_dict()   -- what POST /todos does (app/main.py:50)
Racer thread: spins store.complete(<id about to be handed out>)  -- what POST /todos/{id}/complete does
A response is torn when overdue != (not done) for this past-due todo (the rule with due 2000-01-01 < today).
sys.setswitchinterval(1e-6) widens the window. Run with PYTHONPATH=<checkout>. Arg: rounds (default 20000).
Part B (--http URL): the same race over HTTP against a live server.
"""
import subprocess
import sys
import threading
from datetime import date

ROUNDS = int(next((a for a in sys.argv[1:] if a.isdigit()), 20000))


def part_a() -> None:
    from app.storage import TodoStore

    sys.setswitchinterval(1e-6)
    store = TodoStore()
    stop = threading.Event()

    def racer() -> None:
        while not stop.is_set():
            store.complete(store._next)
            store.complete(store._next - 1)

    t = threading.Thread(target=racer, daemon=True)
    t.start()
    torn, completed_seen, example = 0, 0, None
    for _ in range(ROUNDS):
        d = store.create("race", date(2000, 1, 1)).to_dict()
        completed_seen += d["done"]
        if d["overdue"] is (not d["done"]):
            continue
        torn += 1
        example = example or d
    stop.set()
    t.join()
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    print(f"{sha} part A: {torn} torn responses in {ROUNDS} rounds "
          f"(responses already done=true: {completed_seen}, so the racer did hit the window region); example: {example}")


def part_b(url: str) -> None:
    import json
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    def call(method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None

    torn = 0
    rounds = min(ROUNDS, 1500)
    with ThreadPoolExecutor(4) as ex:
        for _ in range(rounds):
            nxt = max((t["id"] for t in call("GET", "/todos")[1]), default=0) + 1
            fut = [ex.submit(call, "POST", f"/todos/{nxt}/complete") for _ in range(3)]
            s, d = call("POST", "/todos", {"title": "race", "due_date": "2000-01-01"})
            [f.result() for f in fut]
            if d["overdue"] is not (not d["done"]):
                torn += 1
    print(f"part B over HTTP {url}: {torn} torn in {rounds} rounds")


if "--http" in sys.argv:
    part_b(sys.argv[sys.argv.index("--http") + 1])
else:
    part_a()
