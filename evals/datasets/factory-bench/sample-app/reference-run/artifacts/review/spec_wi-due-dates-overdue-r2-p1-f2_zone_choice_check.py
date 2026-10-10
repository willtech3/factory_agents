"""F-2 re-check: for every minute of a UTC day, does the zone local-all picks give a local calendar date that differs
from the UTC date? Uses the real POSIX TZ parsing (time.tzset), the same strings as tests/e2e_overdue.py:342."""
import os
import time
from datetime import datetime, timezone

day0 = int(datetime(2026, 10, 10, tzinfo=timezone.utc).timestamp())
same = []
for zone in ("<-12>12", "<+14>-14"):
    os.environ["TZ"] = zone
    time.tzset()
    for minute in range(1440):
        ts = day0 + minute * 60
        hour = minute // 60
        chosen = "<-12>12" if hour < 12 else "<+14>-14"
        if chosen != zone:
            continue
        if time.localtime(ts)[:3] == time.gmtime(ts)[:3]:
            same.append((zone, minute))
print(f"minutes where the chosen zone shares the UTC date: {len(same)} of 1440")
