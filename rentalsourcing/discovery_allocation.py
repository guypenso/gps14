"""Explicit, non-renewing listing-only allocation for the owner's two scans."""
import datetime as dt
import json
import os

from .schedule import scheduled_slot
from .storage import BudgetExceeded

ID = 'listing-recovery-20261113-14'
KEY = 'discovery_allocation:' + ID
SLOTS = {'2026-11-13:evening': 5, '2026-11-14:morning': 4}
EXPIRES = dt.datetime(2026, 11, 14, 10, tzinfo=dt.timezone.utc)


def saved(store):
    row = store.execute('SELECT payload FROM settings WHERE name=?', (KEY,)).fetchone()
    return json.loads(row[0]) if row else None


def activate(store, settings, now):
    """Build preflight only; preserve counters and never call a provider."""
    if os.getenv('DISCOVERY_ALLOCATION_ID') != ID:
        return {'state': 'not_requested'}
    from .billing import cycle_key
    if now >= EXPIRES:
        return {**(saved(store) or {'id': ID}), 'state': 'expired', 'automatic_renewal': False}
    if (settings.approval_mode != 'monthly_budget' or settings.approval_id != 'rentcast-free-supplement-20261113'
            or settings.billing_anchor != '2026-10-15T00:00:00+00:00'
            or dt.datetime.fromisoformat(settings.approval_expires_at.replace('Z', '+00:00')) != EXPIRES
            or settings.supplemental_requests != 10 or settings.request_ceiling > 50):
        raise BudgetExceeded('Listing allocation does not match the verified, non-renewing approval')
    with store.transaction():
        if store.pg: store.execute('SELECT pg_advisory_xact_lock(73541210)')
        old = saved(store)
        if old:
            return dict(old, state='already_active')
        health = store.budget_health(settings, now, record_warning=False)
        if health['used'] < 41:
            raise BudgetExceeded('Catch-up request must be reconciled before allocating the remaining requests')
        plan = {'id': ID, 'state': 'active', 'activated_at': now.isoformat(),
                'approval_id': settings.approval_id, 'cycle_key': cycle_key(settings, now),
                'used_at_activation': health['used'], 'additional_limit': min(9, health['remaining']),
                'slots': SLOTS, 'expires_at': EXPIRES.isoformat(), 'provider_ceiling': 50,
                'listing_only': True, 'automatic_renewal': False}
        store.save_setting(KEY, plan)
        return plan


def allocation_scope(day, slot):
    key = day + ':' + str(slot)
    return ('multi-discovery:' + key, SLOTS[key]) if key in SLOTS else None


def reservation_periods(store, settings, now, purpose, scope):
    """Called INSIDE the same advisory-locked transaction as every reservation."""
    plan = saved(store)
    if not plan:
        return []
    if now >= EXPIRES:
        from .billing import cycle_key
        if settings.approval_id == plan['approval_id'] or cycle_key(settings,now) == plan['cycle_key']:
            raise BudgetExceeded('Non-renewing listing allocation expired')
        return []
    slot = scheduled_slot(now)
    expected = allocation_scope(*slot) if slot else None
    if (purpose != 'listing' or not expected or scope != expected
            or settings.approval_id != plan['approval_id']):
        raise BudgetExceeded('Remaining requests are reserved for the two approved scheduled listing runs')
    limit=plan['additional_limit']
    if slot[1]=='evening':
        limit=max(0,limit-4)
        if store.count_usage(plan['cycle_key'])>=46:
            raise BudgetExceeded('Four account-wide requests remain protected for the morning')
    return [('allocation:' + ID, limit)]


def allowance(store, settings, now, day, slot):
    plan = saved(store)
    if not plan:
        return None
    if now >= EXPIRES:
        from .billing import cycle_key
        return None if settings.approval_id != plan['approval_id'] and cycle_key(settings,now) != plan['cycle_key'] else 0
    if scheduled_slot(now) != (day, slot):
        return 0
    scope = allocation_scope(day, slot)
    if not scope:
        return 0
    day_limit = 6 if day == '2026-11-13' else 4
    reserved = 4 if slot=='evening' else 0
    return max(0, min(scope[1] - store.count_usage('request_scope:' + scope[0]),
                      plan['additional_limit'] - store.count_usage('allocation:' + ID) - reserved,
                      day_limit - store.count_usage('day:' + day),
                      store.budget_health(settings, now, record_warning=False)['remaining'] - reserved))


def snapshot(store, now):
    plan = saved(store)
    if not plan:
        return {'state': 'not_active'}
    used = store.count_usage('allocation:' + ID)
    return {**plan, 'state': 'expired' if now >= EXPIRES else 'active', 'used': used,
            'remaining': 0 if now >= EXPIRES else min(plan['additional_limit'] - used,
                max(0, 50 - store.count_usage(plan['cycle_key']))),
            'run_allowances': {k: {'limit': n, 'used': store.count_usage('request_scope:multi-discovery:' + k),
                                  'remaining': max(0, n - store.count_usage('request_scope:multi-discovery:' + k))}
                               for k, n in SLOTS.items()},
            'other_provider_jobs_blocked': now < EXPIRES,
            'provider_calls': 0}
