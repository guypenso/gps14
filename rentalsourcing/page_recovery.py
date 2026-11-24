"""Reconcile committed listing pages with canonical records and signed PENTEN acknowledgments."""
import datetime as dt
import hashlib
import json
from collections import Counter

from .collector import LIVE, request_url, response_key
from .core import verification
from .digest import recent_timestamp
from .municipal_coverage import UNIFIED
from .review_inventory import select, run as deliver
from .schedule import ISRAEL
from .sync_runtime import get
from .tax_backlog import review_snapshot, review_deferred

LATEST = 'listing_page_recovery:last'


def cached_page(store, query, offset):
    key = response_key(request_url('/listings/sale', dict(query, offset=offset)))
    saved = store.execute('SELECT payload FROM response_cache WHERE key=? AND source=?', (key, LIVE)).fetchone()
    return json.loads(saved[0]) if saved else {}


def recovery_key(page):
    identity = f"{page['job_id']}|{page['offset_requested']}|{page['fetched_at']}"
    return 'listing_page_recovery:' + hashlib.sha256(identity.encode()).hexdigest()


def reconcile_page(store, boundary, now, run, page, config, opener=None):
    from .reliability import destination_inventory
    offset = page['offset_requested']
    observed = page['fetched_at']
    key = recovery_key(page)
    prior = get(store, key, {})
    if prior.get('state') == 'complete':
        return prior
    query = run['partitions'][UNIFIED]['query']
    cache = cached_page(store, query, offset)
    rows = cache.get('data')
    if cache.get('fetched_at') != observed or not isinstance(rows, list) or len(rows) != page['records']:
        return {'state': 'exact_cached_response_unavailable', 'run_key': run['run_key'], 'offset': offset}
    ids = {p['id'] for p in rows if isinstance(p, dict) and isinstance(p.get('id'), str) and p['id']}
    fresh_ids = ids.intersection(run.get('new_source_ids', []))
    canonical = {}
    if ids:
        args = tuple(sorted(ids))
        for source_id, payload, source, at, reasons in store.execute(
                'SELECT id,payload,source,observed_at,reasons FROM properties WHERE id IN (' +
                ','.join('?' for _ in args) + ')', args).fetchall():
            canonical[source_id] = (json.loads(payload), source, at, json.loads(reasons))
    pairs, _ = select(store, boundary, now, source_ids=ids, include_verified=True)
    cards = {card['source_id']: card for card, _ in pairs}
    reviews = review_snapshot(store)
    matches = destination_inventory(config, sorted(cards), opener)
    missing = {i for i,c in cards.items() if not matches[i]['matched'] and recent_timestamp(c['listing_observed_at'],now)
               and reviews.get(i,{}).get('status') in (None,'new') and not reviews.get(i,{}).get('contract_state')}
    delivery_key = 'stored-review-inventory:cached-page:' + key.rsplit(':',1)[1]
    ledger = get(store, delivery_key, {})
    if missing or ledger and not ledger.get('synced_at'):
        url, secret, token = config
        delivery_config = (url.rsplit('/', 1)[0] + '/review-reports', secret, token)
        ledger = deliver(store, boundary, now, execute=True, config=delivery_config, opener=opener,
                         job_key=delivery_key, source_ids=missing, include_verified=True, refresh_decisions=False)
        matches = destination_inventory(config, sorted(cards), opener)
    counts = Counter()
    new_records = {}
    represented = {}
    for row in rows:
        source_id = row.get('id') if isinstance(row,dict) else None
        cached_state, cached_reasons = verification(row,boundary) if isinstance(row,dict) else ('Pending Verification',['invalid_listing'])
        current, source, at, stored_reasons = canonical.get(source_id, ({}, None, None, []))
        state, reasons = verification(current,boundary) if current else (cached_state,cached_reasons)
        if not current or source != LIVE:
            outcome = 'canonical_evidence_missing'
        elif state == 'Rejected':
            outcome = 'hard_rejected'
        elif review_deferred(source_id,store,reviews=reviews):
            outcome = 'owner_decision_preserved'
        elif source_id in cards:
            match = matches[source_id]
            if match['matched']:
                outcome = 'represented_in_penten'
                represented[source_id] = {'review_status':match['review_status'],'tax_status':match.get('tax_status')}
            elif not recent_timestamp(at,now):
                outcome = 'stale_observation'
            elif reviews.get(source_id,{}).get('status') not in (None,'new') or reviews.get(source_id,{}).get('contract_state'):
                outcome = 'owner_decision_preserved'
            else:
                outcome = 'missing_at_destination'
        else:
            outcome = 'tax_rejected' if 'annual_tax_too_high' in stored_reasons else 'evidence_unresolved'
            reasons = list(dict.fromkeys(reasons + stored_reasons))
        counts[outcome] += 1
        if source_id in fresh_ids:
            new_records[source_id] = {'source_id':source_id,'address':row.get('formattedAddress'),
                                      'source_state':cached_state,'source_reasons':cached_reasons,
                                      'disposition':outcome,'reasons':reasons}
    result = {'state':'pending' if counts['missing_at_destination'] or counts['canonical_evidence_missing'] else 'complete',
              'run_key':run['run_key'],'job_id':page['job_id'],'offset':offset,'source_observed_at':observed,
              'checked_at':now.isoformat(),'records':len(rows),'unique_identities':len(ids),
              'dispositions':dict(counts),'new_sources':list(new_records.values()),'represented':represented,
              'delivery_acknowledgments':ledger.get('receipt_counts',{}),'missing_after_delivery':counts['missing_at_destination'],
              'provider_calls':0,'outgoing_emails':0}
    store.save_setting(key,result)
    store.save_setting(LATEST,result)
    return result


def reconcile_recent(store,boundary,now,config,opener=None):
    """Run under the watchdog's discovery lock, after its signed owner-feedback refresh."""
    if not config:
        return
    today = now.astimezone(ISRAEL).date()
    for day in (today-dt.timedelta(days=1), today):
        for slot in ('morning','evening'):
            run = get(store, 'multi-discovery:'+day.isoformat()+':'+slot, {})
            for page in run.get('pages',[]):
                if (page.get('partition')==UNIFIED and run.get('finished_at') and 'offset_requested' in page
                        and recent_timestamp(page.get('fetched_at'),now,hours=48)):
                    reconcile_page(store,boundary,now,run,page,config,opener)
