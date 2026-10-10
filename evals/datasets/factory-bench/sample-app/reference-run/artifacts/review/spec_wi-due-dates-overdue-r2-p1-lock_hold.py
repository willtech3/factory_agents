"""Lock-hold cost of list_dicts (run from a checkout root with PYTHONPATH=.): builds N todos and times list_dicts()."""
import time
from datetime import date

from app.storage import TodoStore

for n in (1_000, 10_000, 50_000):
    s = TodoStore()
    for i in range(n):
        s.create(f"t{i}", date(2000, 1, 1) if i % 2 else None)
    best = min((lambda t0: (s.list_dicts(), time.perf_counter() - t0)[1])(time.perf_counter()) for _ in range(3))
    print(f"n={n}: list_dicts {best * 1000:.1f} ms")
