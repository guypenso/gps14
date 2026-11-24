"""One owner-approved advance of the November 24 evening listing release."""
import datetime as dt
import json
from dataclasses import replace

from .engine import Engine
from .municipal_coverage import UNIFIED, page_evidence
from .page_recovery import cached_page, reconcile_page
from .partition_discovery import ListingClient, PREFIX, _fingerprints, _record_page
from .schedule import ISRAEL
from .storage import BudgetExceeded
from .sync_runtime import get

DAY = '2026-11-24'
HEAD = 'multi-discovery:2026-11-23:evening'
JOB = 'coverage:investment-20261111-two-bedroom:all-approved-municipalities:2026-11-23:evening:3d0b144b'
QUERY_HASH = '3d0b144b95aa6641a9e046a5506b54e94c56655377be08d193abef750d535775'
KEY = 'approved_page_continuation:' + DAY
SCOPE = KEY + ':offset-475'
OFFSET = 475


def in_window(now):
    local = now.astimezone(ISRAEL)
    return local.date().isoformat() == DAY and dt.time(8, 20) <= local.time() < dt.time(19)


def reservation_limit(store, settings, now, purpose, scope):
    from .monthly_allocation import active, limits, listing_limit_now, period
    from .billing import cycle_key
    cursor = get(store, PREFIX + UNIFIED, {})
    intent = get(store, KEY, {})
    caps = limits(settings)
    usage = {kind: store.count_usage(period(settings, now, kind)) for kind in caps}
    account = store.count_usage(cycle_key(settings, now))
    evening = dt.datetime.combine(dt.date.fromisoformat(DAY), dt.time(20), tzinfo=ISRAEL)
    if (purpose != 'listing' or scope != (SCOPE, 1) or not in_window(now) or not active(settings, now)
            or settings.approval_id != 'rentcast-free-20261115-cycle-only' or settings.request_ceiling != 50
            or caps != {'listing': 36, 'rent': 8, 'targeted': 4, 'recovery': 2} or usage['listing'] != 12
            or account != settings.billing_baseline + sum(usage.values()) or account >= 50
            or listing_limit_now(settings, evening) < 13 or not settings.enabled
            or cursor.get('job_id') != JOB or cursor.get('query_hash') != QUERY_HASH
            or cursor.get('offset') != 500 or cursor.get('query_complete') or cursor.get('blocked')
            or intent.get('state') != 'prepared' or intent.get('offset_requested') != OFFSET
            or not get(store, 'discovery_run:' + DAY + ':morning', {}).get('finished_at')):
        raise BudgetExceeded('Approved listing continuation preconditions do not match')
    return 13


class ContinuationClient(ListingClient):
    def get(self, path, params=None, cache=True):
        if path != '/listings/sale' or params != self.allowed_params or not cache:
            raise PermissionError('Only the approved listing page can be requested')
        if self.saved_page:
            self.cache_hits += 1
            self.last_fetched_at = self.saved_page['fetched_at']
            return self.wrap(self.saved_page['data']), self.saved_page.get('total')
        if self.store.count_usage('request_scope:' + SCOPE):
            raise BudgetExceeded('Previous reservation has no matching response; operator reconciliation required')
        rows, total = super().get(path, params)
        if not self.head_observed_at <= self.last_fetched_at <= self.clock().isoformat():
            raise ValueError('Response does not belong to the active traversal')
        return rows, total


def run(store, boundary, settings, clock, config, *, provider=None, destination=None):
    """Caller holds the production discovery lock. Historical slot receipts are immutable."""
    from .review_feedback import refresh
    now = clock()
    receipt = get(store, KEY, {})
    if receipt.get('state') == 'complete':
        return receipt
    if not in_window(now) or not config:
        raise ValueError('Continuation window or destination unavailable')
    if not get(store, 'discovery_run:' + DAY + ':morning', {}).get('finished_at'):
        raise ValueError('Scheduled morning processing must finish first')
    source_run = get(store, HEAD, {})
    head = next((p for p in source_run.get('pages', []) if p.get('job_id') == JOB and p.get('offset_requested') == 0), {})
    state = get(store, PREFIX + UNIFIED, {})
    from .municipal_coverage import unified_query
    if (not source_run.get('finished_at') or head.get('offset_after') != 500 or head.get('records') != 500
            or state.get('job_id') != JOB or state.get('query_hash') != QUERY_HASH
            or state.get('query') != unified_query(boundary)[0] or (not receipt and (state.get('offset') != 500
            or state.get('total') != 741 or state.get('query_complete') or state.get('blocked')))):
        raise ValueError('Persisted source traversal differs from the approved page')
    # Refresh owner decisions and independently settle the whole preceding page before further discovery.
    feedback = refresh(store, now, destination, config)
    if feedback.get('state') != 'fresh':
        raise ValueError('Current administrator decisions unavailable')
    original = reconcile_page(store, boundary, now, source_run, head, config, destination)
    if original.get('state') != 'complete':
        raise ValueError('Previous cached page has unresolved reconciliation')
    query = state['query']
    saved = cached_page(store, query, OFFSET)
    if saved and (not isinstance(saved.get('data'), list) or not head['fetched_at'] <= saved.get('fetched_at','') <= now.isoformat()):
        if saved.get('fetched_at', '') >= head['fetched_at']:
            raise ValueError('Invalid cached continuation response')
        saved = {}
    if not receipt:
        receipt = {'state': 'prepared', 'job_id': JOB, 'started_at': now.isoformat(), 'offset_requested': OFFSET,
                   'matching_cache_at_start': bool(saved), 'head_fetched_at': head['fetched_at'], 'outgoing_emails': 0}
        store.save_setting(KEY, receipt)
    effective = replace(settings, per_run=1, max_pages=1, page_size=500, retries=0, email_enabled=False)
    client = ContinuationClient(store, effective, clock, provider, request_scope=(SCOPE, 1))
    client.allowed_params, client.saved_page = dict(query, offset=OFFSET), saved
    client.head_observed_at = head['fetched_at']
    checkpoint = store.job(JOB, DAY)
    if checkpoint['stats'].get('last_listing_page', {}).get('offset_requested') != OFFSET:
        if checkpoint['offset'] != 500 or checkpoint['collection_done'] or state.get('offset') != 500:
            raise ValueError('Listing checkpoint changed; continuation stopped')
        if not saved:
            reservation_limit(store, effective, now, 'listing', (SCOPE, 1))
        try:
            Engine(store, boundary, client, effective, clock, allow_paid_tax=False, query_parameters=query,
                   offset_overlap=25, describe_page=lambda rows: {'fingerprints': _fingerprints(rows),
                   'area_observations': page_evidence(boundary, rows)}).run(JOB, DAY, candidate_limit=0)
        except Exception as exc:
            from .request import diagnose
            failure = client.last_error or diagnose(exc)
            receipt.update(state='provider_attempt_requires_reconciliation' if client.count else 'blocked',
                           requests_reserved=store.count_usage('request_scope:' + SCOPE),
                           failure={k: failure.get(k) for k in ('stage','http_status','body_category','errorType')})
            if failure.get('stage') == 'proxy_connect' and '403' in (failure.get('reason') or ''):
                receipt['failure']['proxy_status'] = 403
            store.save_setting(KEY, receipt)
            raise
        checkpoint = store.job(JOB, DAY)
    stats = checkpoint['stats']
    evidence = stats.get('last_listing_page', {})
    if evidence.get('offset_requested') != OFFSET:
        raise ValueError('No confirmed continuation response')
    page = {'partition': UNIFIED, 'job_id': JOB, 'offset_before': 500, 'offset_after': checkpoint['offset'],
            'offset_requested': OFFSET, 'records': evidence['records'], 'requests': store.count_usage('request_scope:' + SCOPE),
            'fetched_at': stats['listing_observed_at'], 'query_complete': checkpoint['collection_done'],
            'area_observations': evidence['area_observations']}
    with store.transaction():
        if not receipt.get('page'):
            if state.get('offset') != 500:
                raise ValueError('Partition changed after ingestion')
            _record_page(state, page, evidence['fingerprints'], stats.get('total'))
            store.save_setting(PREFIX + UNIFIED, state)
            receipt.update(state='page_received', requests_reserved=page['requests'], page=page, total=stats.get('total'))
            store.save_setting(KEY, receipt)
    page = receipt['page']
    from .price_sync import field
    firsts = store.execute('SELECT payload FROM settings WHERE name LIKE ? AND '+field(store,'job_id')+'=? AND '+
                           field(store,'at')+'>=?', ('first_discovery:%', JOB, receipt['started_at'])).fetchall()
    identities = [json.loads(row[0])['source_id'] for row in firsts]
    audit_run = {'run_key': SCOPE, 'partitions': {UNIFIED: {'query': query}}, 'new_source_ids': identities}
    audit = reconcile_page(store, boundary, clock(), audit_run, page, config, destination)
    receipt.update(state=audit['state'], checked_at=clock().isoformat(), new_identities=len(identities),
                   dispositions=audit.get('dispositions'), acknowledgments=audit.get('delivery_acknowledgments'),
                   new_dispositions={name: sum(row['disposition']==name for row in audit.get('new_sources',[]))
                                     for name in sorted({row['disposition'] for row in audit.get('new_sources',[])})},
                   post_delivery_missing=audit.get('missing_after_delivery'))
    if audit['state'] == 'complete':
        # A future traversal must not rediscover or redeliver these same source identities as new.
        key = 'future_discovery_disposed:' + JOB
        disposed = set(get(store, key, {}).get('source_ids', []))
        with store.transaction():
            store.save_setting(key, {'source_ids': sorted(disposed.union(identities))})
            store.save_setting(KEY, receipt)
    else:
        store.save_setting(KEY, receipt)
    return receipt
