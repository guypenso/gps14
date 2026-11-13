"""Prepare a verified free-plan grant. Pure calculation: no network, ledger writes or activation."""
import calendar
import datetime as dt
from .schedule import ISRAEL

def plan(provider_used,reset_date,ledger,*,now=None):
    now=now or dt.datetime.now(dt.timezone.utc)
    if type(provider_used) is not int or not 0<=provider_used<=50:raise ValueError('Verified provider usage must be between 0 and 50')
    reset=dt.date.fromisoformat(reset_date)
    # A date-only provider reset could occur as early as midnight in UTC+14.
    # Stop before every possible timezone's reset; never assume UTC midnight is authoritative.
    cutoff=dt.datetime.combine(reset,dt.time(),dt.timezone(dt.timedelta(hours=14))).astimezone(dt.timezone.utc)
    if now>=cutoff:raise ValueError('Date-only verification is no longer safe; obtain the exact reset timestamp')
    year,month=(reset.year-1,12) if reset.month==1 else (reset.year,reset.month-1)
    start=reset.replace(year=year,month=month,day=min(reset.day,calendar.monthrange(year,month)[1]))
    grant=min(11,50-provider_used)
    if not grant:raise ValueError('No included provider allowance remains')
    app=sum(r['used'] for r in ledger if 'day:'+start.isoformat()<=r['period']<'day:'+reset.isoformat())
    return {'state':'ready_for_activation','provider_used':provider_used,'provider_included':50,'additional_requests':grant,'app_reserved_attempts_in_date_window':app,'unreconciled_account_delta':provider_used-app,'delta_note':'Day counters count attempts, including errors. The account delta is covered by the provider baseline; its origin cannot be attributed from these counters alone. Approval/cycle counters are overlapping views, never additive.','reset_date':reset_date,'cutoff':cutoff.isoformat(),'anchor_note':'UTC date anchor is an internal accounting boundary inferred from the monthly reset date, not a verified provider reset timestamp. The separate conservative expiry prevents renewal.','environment':{'API_APPROVAL_MODE':'monthly_budget','API_APPROVAL_ID':'rentcast-free-supplement-20261113','BILLING_CYCLE_ANCHOR':start.isoformat()+'T00:00:00+00:00','BILLING_USAGE_BASELINE':str(provider_used),'PROVIDER_INCLUDED_REQUESTS':'50','MONTHLY_API_REQUEST_BUDGET':'50','MAX_API_REQUESTS_PER_MONTH':'50','SUPPLEMENTAL_API_REQUESTS':str(grant),'API_APPROVAL_EXPIRES_AT':cutoff.isoformat(),'MAX_API_REQUESTS_PER_RUN':'1','MAX_API_REQUESTS_PER_DAY':'2','ENABLE_RENT_ESTIMATES':'false','ENABLE_PAID_TAX_VERIFICATION':'false','CATCH_UP_DISCOVERY_DAY':now.astimezone(ISRAEL).date().isoformat()}}
