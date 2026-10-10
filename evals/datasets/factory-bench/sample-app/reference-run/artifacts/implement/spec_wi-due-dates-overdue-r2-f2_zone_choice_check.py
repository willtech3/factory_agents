# local-all picks TZ "<-12>12" (UTC-12) before 12:00 UTC and "<+14>-14" (UTC+14) from 12:00; check, for every
# minute of a UTC day, that the chosen zone's calendar date differs from the UTC date.
from datetime import datetime, timedelta, timezone

start = datetime(2026, 10, 10, tzinfo=timezone.utc)
bad = []
for m in range(24 * 60):
    now = start + timedelta(minutes=m)
    offset = timedelta(hours=-12) if now.hour < 12 else timedelta(hours=14)
    if now.astimezone(timezone(offset)).date() == now.date():
        bad.append(now.time())
print(f"minutes of the UTC day where the chosen zone shares the UTC date: {len(bad)} of 1440")
