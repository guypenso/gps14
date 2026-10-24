"""Conservative RentCast ceilings layered atomically on the ORIGINAL allowance ledger."""
import uuid,math
from ..billing import cycle_key,billing_window
from ..storage import BudgetExceeded


def seed(store,settings,now):
    if not settings.billing_anchor or settings.billing_baseline<0:raise BudgetExceeded('Known provider cycle anchor and usage baseline required')
    key=cycle_key(settings,now);start,_,anchor=billing_window(settings,now)
    if now<anchor:raise BudgetExceeded('Provider cycle has not started')
    baseline=settings.billing_baseline if start==anchor else 0
    store.execute('INSERT INTO usage VALUES (?,?) ON CONFLICT(period) DO UPDATE SET used=CASE WHEN usage.used<excluded.used THEN excluded.used ELSE usage.used END',(key,baseline))


def reserve(store,settings,now,purpose,request_hash):
    """Caller owns existing quota transaction/lock. Failed checks roll back ALL counters."""
    if purpose not in {'discovery','enrichment','rent_estimate','verification'}:raise BudgetExceeded('Explicit request purpose required')
    cycle=cycle_key(settings,now);used=store.count_usage(cycle)
    limit=min(50,settings.per_month,settings.approved_requests if settings.approval_mode=='billing_cycle' else settings.per_month)
    if used>limit:raise BudgetExceeded('Internal/provider50 ceiling reached')
    counts=dict(store.execute('SELECT purpose,COUNT(*) FROM discovery_api_ledger WHERE cycle=? GROUP BY purpose',(cycle,)).fetchall())
    if purpose=='discovery' and counts.get(purpose,0)>=settings.discovery_cap:raise BudgetExceeded('Discovery allocation reached')
    if purpose in {'enrichment','rent_estimate'} and sum(counts.get(k,0) for k in ('enrichment','rent_estimate'))>=settings.enrichment_cap:raise BudgetExceeded('Enrichment allocation reached')
    if purpose=='verification' and counts.get(purpose,0)>=settings.verification_reserve:raise BudgetExceeded('Verification allocation reached')
    remaining_reserve=max(0,settings.verification_reserve-counts.get('verification',0))
    if purpose!='verification' and limit-used<remaining_reserve:raise BudgetExceeded('Capacity reserved for important verification')
    identity=uuid.uuid4().hex
    store.execute('INSERT INTO discovery_api_ledger VALUES (?,?,?,?,?,?)',(identity,cycle,purpose,request_hash,now.isoformat(),'reserved_or_uncertain'))
    return identity


def forecast(days=31,pages=1,candidates=9,requests_per_candidate=2,verification=6):
    if days not in range(1,32) or min(pages,candidates,requests_per_candidate,verification)<0:raise ValueError('Invalid forecast')
    schedules={'daily':days,'every_other_day':math.ceil(days/2),'weekly':math.ceil(days/7),'event_driven_only':0}
    return {k:dict(discovery_requests=n*pages,enrichment_requests=candidates*requests_per_candidate,verification_requests=verification,
        total=n*pages+candidates*requests_per_candidate+verification,within_internal40=n*pages+candidates*requests_per_candidate+verification<=40,
        within_included50=n*pages+candidates*requests_per_candidate+verification<=50) for k,n in schedules.items()}
