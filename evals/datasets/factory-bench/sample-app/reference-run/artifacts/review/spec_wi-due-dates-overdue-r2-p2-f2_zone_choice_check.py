"""F-2 re-check of local-all's zone choice: for every minute of a UTC day, does the chosen POSIX zone give a local
calendar date different from the UTC date? Uses the same expression as tests/e2e_overdue.py:342 and the fixed
offsets those POSIX strings denote (<-12>12 = UTC-12, <+14>-14 = UTC+14; confirmed with `TZ=... date`).
Also reports how close to a flip point the choice is (a run that crosses 00:00 or 12:00 UTC loses the property)."""
from datetime import datetime, timedelta, timezone

OFFSET = {"<-12>12": -12, "<+14>-14": 14}
same, margin = 0, []
base = datetime(2026, 10, 10, tzinfo=timezone.utc)
for m in range(1440):
    now = base + timedelta(minutes=m)
    zone = "<-12>12" if now.hour < 12 else "<+14>-14"
    local = now.astimezone(timezone(timedelta(hours=OFFSET[zone])))
    same += local.date() == now.date()
print(f"minutes of the UTC day whose chosen zone shares the UTC date: {same} of 1440")
print("the property is lost only if one local-all run spans 12:00 UTC (chosen UTC-12 before, local date becomes the"
      " UTC date at 12:00) or 00:00 UTC (chosen UTC+14 before, local date becomes the UTC date at 00:00)")
