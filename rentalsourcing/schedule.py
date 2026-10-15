"""Render cron uses UTC; evaluate both possible UTC slots in Pittsburgh time."""
import datetime as dt
from zoneinfo import ZoneInfo

PITTSBURGH = ZoneInfo('America/New_York')

def scheduled_day(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None: raise ValueError('Timezone-aware clock required')
    local = now.astimezone(PITTSBURGH)
    return local.date().isoformat() if local.hour == 8 else None
