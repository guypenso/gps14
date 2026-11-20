"""Evidence-based ordering inside existing atomic allocation; never spends calls."""
import datetime as dt
VERSION='freshness-fairness-yield-v1'


def rank(slug,state,now,order):
    last=state.get('last_success_at')
    try:age=max(0,(now-dt.datetime.fromisoformat(last.replace('Z','+00:00'))).total_seconds())
    except (ValueError,TypeError,AttributeError):age=1e12
    overdue=age>=72*3600
    pages=state.get('selection_pages',0)/(2 if slug=='pittsburgh-bellevue' else 1)
    performance=state.get('performance',{});requests=max(1,performance.get('requests',0))
    yield_=performance.get('new_reviewable',0)/requests
    return (0 if overdue else 1,-age if overdue else pages,
            0 if state.get('job_id') and not state.get('query_complete') else 1,
            -min(yield_,10),order.index(slug),-age)


def model(settings,now):
    from .monthly_allocation import _slots,grant_window,listing_limit_now,limits
    window=grant_window(settings)
    if not window:return {'state':'approval_unavailable','automatic_renewal':False,'provider_calls':0}
    prior=0;days={}
    from .schedule import ISRAEL
    for slot in _slots(*window):
        released=listing_limit_now(settings,slot);delta=max(0,released-prior);prior=released
        day=slot.astimezone(ISRAEL).date().isoformat()
        days.setdefault(day,[]).append({'at_israel':slot.astimezone(ISRAEL).isoformat(),'newly_released_listing_requests':delta})
    caps=limits(settings)
    return {'version':VERSION,'state':'forecast','approval_expires_at':window[1].isoformat(),
            'purpose_caps':caps,'days':days,'automatic_renewal':False,'provider_calls':0,
            'capacity_model':{'processing_windows':sum(len(v) for v in days.values()),'listing_pages':caps['listing'],
             'assumed_pages_per_traversal':4,'provider_query_traversals_at_that_size':caps['listing']/4,
             'assumptions':'Pittsburgh/Bellevue two pages; Bethel Park and Pleasant Hills one each at limit 500. Inventory can change; official municipal completeness is unverified.',
             'main_page_share':0.5,'other_partition_page_share_each':0.25}}
