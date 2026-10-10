"""Review finding F-1 reproduction (in-process, same calls the routes make).

Main thread: store.create("race", date(2000, 1, 1)).to_dict()  -- what POST /todos does (app/main.py create_todo).
Spinner thread: store.complete(<id just handed out>)            -- what POST /todos/{id}/complete does.
sys.setswitchinterval(1e-6) widens the window. A response is torn when overdue != (not done): the due date is in
the past, so the rule says overdue iff open.

Usage: PYTHONPATH=<checkout> python probe_race_post_response.py [rounds]   (exit 1 if any torn response)
"""
import sys
import threading
from datetime import date

from app.storage import TodoStore

rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
sys.setswitchinterval(1e-6)
store = TodoStore()
stop = threading.Event()


def spinner() -> None:
    while not stop.is_set():
        store.complete(store._next - 1)


thread = threading.Thread(target=spinner, daemon=True)
thread.start()
torn = []
for _ in range(rounds):
    d = store.create("race", date(2000, 1, 1)).to_dict()
    if d["overdue"] is not (not d["done"]):
        torn.append(d)
stop.set()
thread.join()
print(f"{len(torn)} torn responses in {rounds} rounds" + (f"; example: {torn[0]}" if torn else ""))
sys.exit(1 if torn else 0)
