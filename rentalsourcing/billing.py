"""Subscription windows retain the original anniversary on short months."""
import calendar
import datetime as dt


def billing_window(settings, now=None):
    now=now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:raise ValueError('Timezone-aware billing clock required')
    if settings.billing_anchor:
        try:
            anchor=dt.datetime.fromisoformat(settings.billing_anchor.replace('Z','+00:00'))
            if anchor.tzinfo is None:raise ValueError
            anchor=anchor.astimezone(dt.timezone.utc)
        except (ValueError,TypeError):
            raise ValueError('BILLING_CYCLE_ANCHOR must be the provider cycle start with timezone') from None
    elif settings.approval_mode=='billing_cycle':
        raise ValueError('BILLING_CYCLE_ANCHOR is required for recurring subscription approval')
    else:
        anchor=dt.datetime(2000,1,settings.billing_day,tzinfo=dt.timezone.utc)
    def anniversary(month_index):
        year,month=divmod(month_index,12);month+=1
        return anchor.replace(year=year,month=month,day=min(anchor.day,calendar.monthrange(year,month)[1]))
    index=now.year*12+now.month-1
    start=anniversary(index)
    if now<start:index-=1;start=anniversary(index)
    return start,anniversary(index+1),anchor


def cycle_key(settings, now=None):
    start,_,_=billing_window(settings,now)
    return 'cycle:'+ (start.isoformat() if settings.approval_mode=='billing_cycle' else start.date().isoformat())


def future_listing_days(now,end):
    """Future 08:00 Pittsburgh slots strictly before provider renewal."""
    from .schedule import PITTSBURGH
    day=now.astimezone(PITTSBURGH).date()+dt.timedelta(days=1)
    count=0
    while day<=end.astimezone(PITTSBURGH).date():
        slot=dt.datetime.combine(day,dt.time(8),tzinfo=PITTSBURGH)
        if slot<end:count+=1
        day+=dt.timedelta(days=1)
    return count
