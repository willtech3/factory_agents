"""F-1 re-check: torn done/overdue in single-object responses under a concurrent complete().

Run from a checkout root with PYTHONPATH=. :
    PYTHONPATH=. python probe_race_post_response.py [rounds] [mode]
mode "create" (default): main thread does store.create('race', date(2000,1,1)).to_dict(), which is exactly what
POST /todos does (app/main.py create_todo); a second thread spins store.complete(<id about to be handed out>),
which is what POST /todos/{id}/complete does. A response is torn when overdue != (not done) for a past-due todo.
sys.setswitchinterval(1e-6) widens the window so the interleaving actually happens in-process.
"""
from __future__ import annotations

import sys
import threading
from datetime import date

from app.main import store

rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
sys.setswitchinterval(1e-6)

store.clear()
stop = False


def completer() -> None:
    while not stop:
        store.complete(store._next)  # the id the next create() will hand out
        store.complete(store._next - 1)  # or the one just handed out


t = threading.Thread(target=completer, daemon=True)
t.start()
torn = []
for _ in range(rounds):
    d = store.create("race", date(2000, 1, 1)).to_dict()
    if d["overdue"] is not (not d["done"]):
        torn.append(d)
stop = True
t.join()
print(f"{len(torn)} torn responses in {rounds} rounds" + (f"; example: {torn[0]}" if torn else ""))
