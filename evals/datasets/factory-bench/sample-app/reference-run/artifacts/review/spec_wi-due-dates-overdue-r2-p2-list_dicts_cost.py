"""Cost of the F-1 fix on the list path: list_dicts() builds every dict while holding TodoStore._lock, and since
e563e95 each to_dict() also does dataclasses.replace(). Times list_dicts() at N todos. PYTHONPATH=<checkout>."""
import subprocess
import time
from datetime import date

from app.storage import TodoStore

sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
for n in (1000, 10000, 50000):
    s = TodoStore()
    for i in range(n):
        s.create("t", date(2000, 1, 1) if i % 3 == 0 else None)
    best = min((lambda t0: (s.list_dicts(), time.perf_counter() - t0)[1])(time.perf_counter()) for _ in range(5))
    print(f"{sha} list_dicts at {n}: {best * 1000:.1f} ms (best of 5)")
