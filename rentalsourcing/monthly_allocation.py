"""Non-renewing, account-wide free-plan allocation for a separately approved cycle.

The provider reset and baseline must be confirmed before configuring a new
grant. This module never infers permission from a calendar rollover.
"""
import datetime as dt
import math

from .billing import billing_window, cycle_key
from .schedule import ISRAEL, scheduled_slot
from .storage import BudgetExceeded


def grant_window(settings):
    if settings.approval_mode != 'monthly_budget' or settings.supplemental_requests:
        return None
    if not settings.approval_id or not settings.approval_expires_at or not settings.provider_cycle_verified_at:
        return None
    try:
        anchor = dt.datetime.fromisoformat(settings.billing_anchor.replace('Z', '+00:00'))
        expires = dt.datetime.fromisoformat(settings.approval_expires_at.replace('Z', '+00:00'))
        verified = dt.datetime.fromisoformat(settings.provider_cycle_verified_at.replace('Z', '+00:00'))
        if anchor.tzinfo is None or expires.tzinfo is None or verified.tzinfo is None:
            return None
        anchor = anchor.astimezone(dt.timezone.utc)
        expires = expires.astimezone(dt.timezone.utc)
        verified = verified.astimezone(dt.timezone.utc)
        _, end, _ = billing_window(settings, anchor)
        return (anchor, expires) if anchor <= verified < expires <= end else None
    except (TypeError, ValueError):
        return None


def active(settings, now):
    window = grant_window(settings)
    return bool(window and window[0] <= now < window[1]
                and billing_window(settings, now)[0] == window[0])


def limits(settings):
    # Already consumed provider-wide requests are outside this new grant.
    total = max(0, settings.request_ceiling-settings.billing_baseline)
    if total < 10:
        return {'listing': total, 'rent': 0, 'targeted': 0, 'recovery': 0}
    weights = {'listing': 36, 'rent': 8, 'targeted': 4, 'recovery': 2}
    caps = {kind: total*weight//50 for kind,weight in weights.items()}
    order = sorted(weights, key=lambda kind: -(total*weights[kind] % 50))
    for kind in order[:total-sum(caps.values())]:caps[kind] += 1
    return caps


def block_reason(settings, now):
    if settings.approval_mode != 'monthly_budget' or settings.supplemental_requests:
        return None
    if not settings.provider_cycle_verified_at:
        return 'provider_cycle_verification_required'
    window = grant_window(settings)
    if not window:return 'current_cycle_approval_invalid'
    if now >= window[1]:return 'cycle_approval_expired'
    if not active(settings, now):return 'current_cycle_approval_inactive'
    return None


def _slots(anchor, expires):
    day = anchor.astimezone(ISRAEL).date()-dt.timedelta(days=1)
    last = expires.astimezone(ISRAEL).date()+dt.timedelta(days=1)
    while day <= last:
        for hour in (8, 20):
            slot = dt.datetime.combine(day, dt.time(hour), tzinfo=ISRAEL).astimezone(dt.timezone.utc)
            if anchor <= slot < expires:
                yield slot
        day += dt.timedelta(days=1)


def listing_limit_now(settings, now):
    window = grant_window(settings)
    if not window or not active(settings, now):
        return 0
    slots = tuple(_slots(*window))
    # Morning-first weighted release: most days retain a morning page, while
    # evening pages and any unused allowance remain available within the cap.
    weight=lambda slot: 5 if slot.astimezone(ISRAEL).hour==8 else 1
    elapsed = sum(weight(slot) for slot in slots if slot <= now)
    return math.ceil(limits(settings)['listing'] * elapsed / sum(map(weight,slots))) if slots else 0


def rent_limit_now(settings, now):
    """Release the small rent allocation across the cycle, not on day one."""
    window = grant_window(settings)
    if not window or not active(settings, now):
        return 0
    start, end = window
    first = start.astimezone(ISRAEL).date()
    last = end.astimezone(ISRAEL).date()
    days = max(1, (last-first).days)
    elapsed = max(1, min(days, (now.astimezone(ISRAEL).date()-first).days+1))
    return math.ceil(limits(settings)['rent'] * elapsed / days)


def period(settings, now, purpose):
    return 'cycle-purpose:' + cycle_key(settings, now) + ':' + purpose


def available(store, settings, now, purpose):
    if not active(settings, now) or purpose not in limits(settings):
        return 0
    cap = limits(settings)[purpose]
    if purpose == 'listing':
        cap = min(cap, listing_limit_now(settings, now))
        if not scheduled_slot(now):
            return 0
    elif purpose == 'rent':
        cap = min(cap, rent_limit_now(settings, now))
    used = store.count_usage(period(settings, now, purpose))
    total = store.budget_health(settings, now, record_warning=False)['remaining']
    return max(0, min(cap-used, total))


def reservation_periods(store, settings, now, purpose, scope):
    if settings.approval_mode != 'monthly_budget' or settings.supplemental_requests:
        return []
    if not active(settings, now):
        raise BudgetExceeded('Current billing cycle is not independently approved or has expired')
    if purpose not in limits(settings):
        raise BudgetExceeded('Provider request purpose is not approved in this cycle')
    if purpose == 'listing':
        slot = scheduled_slot(now)
        expected = 'multi-discovery:' + slot[0] + ':' + slot[1] if slot else None
        if not scope or scope[0] != expected:
            raise BudgetExceeded('Listing allowance is reserved for the scheduled slot')
        limit = listing_limit_now(settings, now)
    else:
        if scope and str(scope[0]).startswith('multi-discovery:'):
            raise BudgetExceeded('Enrichment cannot spend the listing discovery scope')
        limit = rent_limit_now(settings, now) if purpose == 'rent' else limits(settings)[purpose]
    if limit < 1:
        raise BudgetExceeded('Approved monthly allocation is unavailable for this purpose')
    return [(period(settings, now, purpose), limit)]


def purpose_readiness(store,settings,now,purpose):
    """Forecast the next scheduled release; this never reserves or renews calls."""
    reason=block_reason(settings,now)
    if not active(settings,now):
        return {'state':'blocked','reason':reason or 'current_cycle_approval_inactive','available_now':0,'next_at':None}
    used=store.count_usage(period(settings,now,purpose));cap=limits(settings).get(purpose,0)
    remaining=store.budget_health(settings,now,record_warning=False)['remaining']
    ready=available(store,settings,now,purpose)
    if ready:return {'state':'available','reason':None,'available_now':ready,'purpose_remaining':cap-used,'next_at':None}
    if remaining<=0:return {'state':'blocked','reason':'account_wide_allowance_exhausted','available_now':0,'next_at':None}
    if used>=cap:return {'state':'blocked','reason':purpose+'_purpose_allocation_exhausted','available_now':0,'next_at':None}
    window=grant_window(settings)
    for slot in _slots(*window):
        if slot<=now:continue
        released=listing_limit_now(settings,slot) if purpose=='listing' else rent_limit_now(settings,slot) if purpose=='rent' else cap
        if released>used and store.count_usage(slot.strftime('day:%Y-%m-%d'))<settings.per_day:
            return {'state':'scheduled','reason':'monthly_'+purpose+'_pacing','available_now':0,
                    'purpose_remaining':cap-used,'next_at':slot.isoformat(),
                    'forecast_note':'Revalidated atomically at execution; no future capacity is spent or renewed.'}
    return {'state':'blocked','reason':'no_release_before_approval_cutoff','available_now':0,'purpose_remaining':cap-used,'next_at':None}


def snapshot(store, settings, now):
    caps = limits(settings)
    return {'active': active(settings, now), 'approval_id': settings.approval_id,
            'expires_at': settings.approval_expires_at or None,
            'limits': caps,
            'used': {kind: store.count_usage(period(settings, now, kind)) for kind in caps},
            'available': {kind: available(store, settings, now, kind) for kind in caps},
            'listing_cumulative_pace_limit': listing_limit_now(settings, now),
            'rent_cumulative_pace_limit': rent_limit_now(settings, now),
            'readiness':{kind:purpose_readiness(store,settings,now,kind) for kind in ('listing','rent')},
            'provider_calls': 0}
