"""Read persisted execution evidence, without running collectors or synchronizers."""
import datetime as dt
import hashlib
import json
from collections import Counter
from urllib.parse import urlencode

from .collector import LIVE
from .schedule import ISRAEL


def _setting(store, name):
    row = store.execute('SELECT payload FROM settings WHERE name=?', (name,)).fetchone()
    return json.loads(row[0]) if row else {}


def _summary(value):
    # No property payloads, credentials, email content, or raw upstream errors.
    fields = {'state', 'job_id', 'started_at', 'completed_at', 'last_attempt_at',
              'requests', 'requests_this_invocation', 'listing_observations',
              'outgoing_emails', 'paid_tax_or_rent_requests', 'error_type',
              'received', 'received_this_run', 'total', 'discovery_state',
              'listing_observed_at', 'discovery_complete', 'discovery_partial',
              'partial', 'tax_checked', 'tax_cache_hits', 'new_source_listings',
              'new_non_tax_pass', 'new_two_bedroom_candidates',
              'new_three_four_bedroom_candidates', 'new_parcel_matches',
              'new_verified_tax', 'new_estimated_tax', 'new_unknown_tax',
              'new_qualified', 'new_pending', 'new_rejected', 'synced', 'sent',
              'pending', 'held', 'acknowledged', 'untracked', 'paid_tax_calls'}
    return {k: v for k, v in value.items() if k in fields and
            (v is None or isinstance(v, (str, int, float, bool)))}


def snapshot(store, now, boundary=None, page_size=500):
    """Expired cache is audit evidence only; this function never reuses it live."""
    day = now.astimezone(ISRAEL).date().isoformat()
    key = 'catchup-discovery:' + day
    receipt = _setting(store, key)
    result = {'day': day, 'receipt_key': key, 'receipt': _summary(receipt),
              'request_reservations': store.count_usage('request_scope:' + key),
              'provider_calls': 0, 'qualification_changed': False}
    if not receipt:
        result['state'] = 'no_receipt'
        return result
    result['stats'] = _summary(receipt.get('stats', {}))
    result['delivery'] = _summary(receipt.get('delivery', {}))
    result['price_sync'] = _summary(receipt.get('price_sync', {}))
    result['new_source_ids'] = receipt.get('new_source_ids', [])[:500]
    result['new_source_count'] = len(receipt.get('new_source_ids', []))
    job_id = receipt.get('job_id')
    if not job_id:
        return result
    row = store.execute('SELECT day,state,offset_value,collection_done,stats,updated_at FROM scan_jobs WHERE id=?', (job_id,)).fetchone()
    if not row:
        result['state'] = 'receipt_without_job'
        return result
    stats = json.loads(row[4])
    result['job'] = dict(job_id=job_id, day=row[0], state=row[1], offset=row[2],
                         collection_done=bool(row[3]), updated_at=row[5], stats=_summary(stats))
    result['future_discovery'] = _summary(_setting(store, 'future_discovery_report:' + job_id))
    disposed = _setting(store, 'future_discovery_disposed:' + job_id)
    result['disposed_source_count'] = len(disposed.get('source_ids', []))
    received = receipt.get('listing_observations', 0)
    offset = max(0, row[2] - received)
    query = dict(receipt.get('query', {}), limit=page_size, offset=offset)
    result['query'] = {k: v for k, v in query.items() if k in
                       {'city', 'state', 'zipCode', 'latitude', 'longitude', 'radius',
                        'bedrooms', 'price', 'propertyType', 'status', 'limit', 'offset'}}
    url = 'https://api.rentcast.io/v1/listings/sale?' + urlencode(sorted(query.items()))
    request_key = hashlib.sha256(url.encode()).hexdigest()
    result['request_key'] = request_key
    attempt = store.execute('SELECT state,attempted_at,retry_after FROM api_attempts WHERE request_key=?', (request_key,)).fetchone()
    result['unresolved_attempt'] = dict(zip(('state', 'attempted_at', 'retry_after'), attempt)) if attempt else None
    cached = store.execute('SELECT payload,source,expires_at FROM response_cache WHERE key=?', (request_key,)).fetchone()
    if not cached:
        result['response'] = {'state': 'no_saved_response'}
        return result
    body = json.loads(cached[0])
    fetched = body.get('fetched_at')
    try:
        at = dt.datetime.fromisoformat(fetched.replace('Z', '+00:00'))
        start = dt.datetime.fromisoformat(receipt['started_at'].replace('Z', '+00:00'))
        end = dt.datetime.fromisoformat(receipt.get('completed_at', now.isoformat()).replace('Z', '+00:00'))
        matches_run = start <= at <= end and cached[1] == LIVE
    except (KeyError, TypeError, ValueError):
        matches_run = False
    rows = body.get('data', [])
    result['response'] = {'state': 'saved_http_200' if cached[1] == LIVE else 'untrusted_source',
                          'fetched_at': fetched, 'matches_run': matches_run,
                          'records': len(rows) if isinstance(rows, list) else None,
                          'provider_total': body.get('total'), 'expires_at': cached[2]}
    if not matches_run or not isinstance(rows, list):
        return result
    ids = {p['id'] for p in rows if isinstance(p, dict) and isinstance(p.get('id'), str)}
    states, reasons, non_tax = Counter(), Counter(), Counter()
    if ids:
        from .core import verification
        for identity, raw, state, why in store.execute(
                'SELECT id,payload,state,reasons FROM properties WHERE id IN (' + ','.join('?' for _ in ids) + ')', tuple(sorted(ids))).fetchall():
            states[state] += 1
            reasons.update(json.loads(why))
            if boundary is not None:
                non_tax[verification(json.loads(raw), boundary)[0]] += 1
    result['returned_dispositions'] = {'unique_source_ids': len(ids), 'current_states': dict(states),
                                        'reason_counts': dict(reasons), 'non_tax_states': dict(non_tax)}
    sync = Counter()
    for raw, in store.execute('SELECT payload FROM settings WHERE name LIKE ?', ('price-observation:%',)).fetchall():
        entry = json.loads(raw)
        obs = entry.get('observation', {})
        if obs.get('source_id') in ids and obs.get('observed_at') == fetched:
            sync['observations'] += 1
            sync['acknowledged' if entry.get('synced_at') else 'held' if entry.get('held_at') else 'pending'] += 1
            if entry.get('receiver_disposition'):
                sync['receiver_' + entry['receiver_disposition']] += 1
    result['returned_price_sync'] = dict(sync)
    return result
