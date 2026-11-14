"""Owner-approved, non-renewing Nov 14 listing recovery. No digest or enrichment APIs."""
import datetime as dt
import hashlib
import json
import os

from .schedule import ISRAEL
from .storage import BudgetExceeded

DAY='2026-11-14'
SLOT='approved-catchup'
APPROVAL='listing-catchup-20261114-before-noon'
KEY='approved_listing_catchup:'+DAY
RUN='multi-discovery:'+DAY+':'+SLOT
LIMITS={'pittsburgh-bellevue':2,'bethel-park':1,'pleasant-hills':1}
EXPIRES=dt.datetime(2026,11,14,10,tzinfo=dt.timezone.utc)


def enabled():
    return os.getenv('CATCH_UP_DISCOVERY_APPROVAL_ID')==APPROVAL and os.getenv('CATCH_UP_DISCOVERY_DAY')==DAY


def get(store,key):
    row=store.execute('SELECT payload FROM settings WHERE name=?',(key,)).fetchone()
    return json.loads(row[0]) if row else None


def frozen_morning(store):
    return hashlib.sha256(json.dumps(get(store,'multi-discovery:'+DAY+':morning'),sort_keys=True).encode()).hexdigest()


def activate(store,settings,now):
    if not enabled():return {'state':'not_requested'}
    if now>=EXPIRES or now.astimezone(ISRAEL).date().isoformat()!=DAY:return {'state':'expired','provider_calls':0}
    from .discovery_allocation import saved
    allocation=saved(store)
    if not allocation or allocation['approval_id']!=settings.approval_id or settings.request_ceiling>50 or not settings.enabled:
        raise BudgetExceeded('Verified existing free-plan discovery allocation required')
    with store.transaction():
        if store.pg:store.execute('SELECT pg_advisory_xact_lock(73541210)')
        old=get(store,KEY)
        if old:return {**old,'state':'already_active'}
        used=store.count_usage(allocation['cycle_key'])
        # Owner-confirmed account baseline 40 plus one prior catch-up and four
        # evening requests. Do not silently accept an unexplained ledger change.
        if used!=45 or store.count_usage('allocation:'+allocation['id'])!=4:
            raise BudgetExceeded('Account usage differs from revalidated 45/50 baseline')
        remaining=min(4,50-used,store.budget_health(settings,now,record_warning=False)['remaining'])
        if remaining<=0:raise BudgetExceeded('No safely verified capacity')
        body={'id':APPROVAL,'state':'active','activated_at':now.isoformat(),'expires_at':EXPIRES.isoformat(),
              'account_baseline':used,'free_plan_ceiling':50,'limit':remaining,'partition_limits':LIMITS,
              'cycle_key':allocation['cycle_key'],'listing_only':True,'automatic_renewal':False,
              'closed_morning_sha256':frozen_morning(store),'provider_calls':0}
        store.save_setting(KEY,body)
        return body


def remaining(store,settings,now):
    grant=get(store,KEY)
    if not enabled() or not grant or now>=EXPIRES or now.astimezone(ISRAEL).date().isoformat()!=DAY:return 0
    used=store.count_usage('request_scope:'+RUN)
    accounted=store.count_usage(grant['cycle_key'])
    if accounted!=grant['account_baseline']+used:return 0
    return max(0,min(grant['limit']-used,50-accounted,store.budget_health(settings,now,record_warning=False)['remaining']))


def reservation_periods(store,settings,now,purpose,scope):
    if purpose!='listing' or not scope or not scope[0].startswith(RUN+':'):return None
    slug=scope[0][len(RUN)+1:]
    if slug not in LIMITS or scope[1]!=LIMITS[slug] or remaining(store,settings,now)<=0:
        raise BudgetExceeded('Catch-up scope, capacity or approval invalid')
    return [('request_scope:'+RUN,get(store,KEY)['limit'])]


def snapshot(store,now):
    grant=get(store,KEY)
    if not grant:return {'state':'not_active'}
    return {**grant,'state':'expired' if now>=EXPIRES else grant['state'],
            'used':store.count_usage('request_scope:'+RUN),
            'partition_used':{slug:store.count_usage('request_scope:'+RUN+':'+slug) for slug in LIMITS},
            'receipt':get(store,'approved_listing_catchup_receipt:'+DAY),'provider_calls':0}


def run(store,boundary,settings,day,*,clock=None,opener=None,deliver=True):
    clock=clock or (lambda:dt.datetime.now(dt.timezone.utc))
    if day!=DAY:raise BudgetExceeded('Only the approved November 14 catch-up is permitted')
    prior=get(store,'approved_listing_catchup_receipt:'+DAY)
    if prior and prior.get('state') in {'complete','partial'}:return {**prior,'already_executed':True,'requests_this_invocation':0}
    grant=get(store,KEY)
    if not grant or grant['closed_morning_sha256']!=frozen_morning(store):raise BudgetExceeded('Closed morning receipt changed or catch-up not activated')
    if not get(store,RUN) and remaining(store,settings,clock())<=0:raise BudgetExceeded('Catch-up capacity cannot be safely verified')
    from .partition_discovery import run as discover
    stats,client=discover(store,boundary,settings,DAY,SLOT,clock=clock,opener=opener)
    reports=[]
    previous={r['job_id']:r for r in (prior or {}).get('delivery_reports',[])}
    if deliver:
        from .command_center import review_configuration
        from .future_discovery import process
        config=review_configuration()
        if config:
            url,secret,token=config
            config=(url.rsplit('/',1)[0]+'/review-reports',secret,token)
        for job in stats['job_ids']:
            done=previous.get(job)
            reports.append(done if done and done['state'] in {'accepted','no_new_reviewable_records'} else process(store,boundary,job,clock(),config=config,opener=opener,new_since=stats['started_at']))
    receipt={'state':'pending_delivery' if any(r['state'] in {'pending','pending_recovery','not_configured'} for r in reports) else 'partial' if stats.get('discovery_partial') else 'complete',
             'run_key':RUN,'completed_at':clock().isoformat(),'requests':store.count_usage('request_scope:'+RUN),
             'requests_this_invocation':client.count,'outgoing_emails':0,'paid_tax_or_rent_requests':0,
             'delivery_totals':{k:sum(r.get(k,0) for r in reports) for k in ('new_source_listings','recovered_source_listings','new_non_tax_pass','new_qualified','new_pending','new_rejected','synced')},
             'penten_receipts':{k:sum(r.get('receipt_counts',{}).get(k,0) for r in reports) for k in ('inserted','updated','rejected','excluded','duplicate_cards')},
             'delivery_reports':reports,'stats':stats}
    stats.update(delivery_totals=receipt['delivery_totals'],penten_receipts=receipt['penten_receipts'])
    store.save_setting(RUN,stats);store.save_setting('multi-discovery:last',stats)
    store.save_setting('approved_listing_catchup_receipt:'+DAY,receipt)
    store.save_setting('health_budget',store.budget_health(settings,clock(),record_warning=False))
    return receipt
