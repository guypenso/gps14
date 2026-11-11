"""Render cron uses UTC; evaluate both possible UTC slots in Israel time."""
import datetime as dt
from zoneinfo import ZoneInfo

PITTSBURGH = ZoneInfo('America/New_York')
ISRAEL = ZoneInfo('Asia/Jerusalem')

def scheduled_day(now=None):
    slot=scheduled_slot(now)
    return slot[0] if slot else None

def scheduled_slot(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None: raise ValueError('Timezone-aware clock required')
    local = now.astimezone(ISRAEL)
    return (local.date().isoformat(),'morning' if local.hour==8 else 'evening') if local.hour in (8,20) else None
