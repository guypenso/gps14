"""Evidence-based ordering inside existing atomic allocation; never spends calls."""
import datetime as dt
VERSION='freshness-fairness-yield-v2'


def age_seconds(state, now):
    last=state.get('last_success_at')
    try:
        return max(0,(now-dt.datetime.fromisoformat(last.replace('Z','+00:00'))).total_seconds())
    except (ValueError,TypeError,AttributeError):
        return float('inf')


def advance_reason(state, now):
    age = age_seconds(state, now)
    if age >= 72 * 3600:
        return 'partition_overdue'
    if state.get('offset', 0) > 0 and not state.get('query_complete') and age >= 24 * 3600:
        return 'unfinished_pagination_overdue'
    return None


def rank(slug,state,now,order):
    age = age_seconds(state, now)
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
    from .municipal_coverage import AREAS, overlap_size
    return {'version':VERSION,'state':'forecast','approval_expires_at':window[1].isoformat(),
            'purpose_caps':caps,'days':days,'automatic_renewal':False,'provider_calls':0,
            'capacity_model':{'processing_windows':sum(len(v) for v in days.values()),'listing_pages':caps['listing'],
             'target_areas':list(AREAS),'page_size':settings.page_size,
             'continuation_overlap':overlap_size(settings.page_size),
             'maximum_one_page_windows':caps['listing'],
             'example_complete_traversals_at_two_pages':caps['listing']//2,
             'legacy_traversals_at_four_pages':caps['listing']//4,
             'market_completeness':'not_verified'}}
