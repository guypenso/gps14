"""Audit real persistence paths; never issue a provider request while auditing."""
import datetime as dt
import hashlib
import json
from urllib.parse import urlencode

from conftest import listing
from rentalsourcing.collector import LIVE
from rentalsourcing.discovery_audit import snapshot


def seed(store, clock, *, received=2, cached=True, old=False):
    day = clock().date().isoformat()
    job = 'coverage:fixture:pittsburgh-bellevue:2026-10-15:morning'
    store.job(job, '2026-10-15')
    stats = {'received': 500 + received, 'received_this_run': received, 'discovery_state': 'fresh'}
    store.checkpoint(job, 500 + received, False, stats, 'partial')
    receipt = {'state': 'complete', 'job_id': job, 'query': {'city': 'Pittsburgh', 'state': 'PA'},
               'started_at': clock().isoformat(), 'completed_at': (clock()+dt.timedelta(seconds=10)).isoformat(),
               'requests': 1, 'listing_observations': received, 'new_source_ids': [], 'stats': stats,
               'delivery': {'state': 'no_new_reviewable_records', 'new_source_listings': 0, 'synced': 0},
               'secret': 'must never leave storage'}
    store.save_setting('catchup-discovery:' + day, receipt)
    store.execute('INSERT INTO usage VALUES (?,?)', ('request_scope:catchup-discovery:' + day, 1))
    params = dict(receipt['query'], limit=500, offset=500)
    key = hashlib.sha256(('https://api.rentcast.io/v1/listings/sale?' + urlencode(sorted(params.items()))).encode()).hexdigest()
    if cached:
        at = clock()-dt.timedelta(hours=1) if old else clock()+dt.timedelta(seconds=5)
        rows = [listing('known-a'), listing('known-b', bedrooms=5)][:received]
        store.cache_put(key, {'data': rows, 'total': '900', 'fetched_at': at.isoformat()}, LIVE, (clock()+dt.timedelta(minutes=15)).isoformat())
        for p in rows:
            store.save_property(p['id'], p, 'Rejected' if p['bedrooms']==5 else 'Pending Verification',
                                ['bedrooms'] if p['bedrooms']==5 else ['tax_not_verified'], LIVE, 1, at.isoformat())
    return key


def test_audit_proves_saved_response_and_resumed_cursor_without_writes(store, clock, boundary, monkeypatch):
    seed(store, clock)
    original = store.execute
    def read_only(sql, args=()):
        assert sql.lstrip().upper().startswith('SELECT ')
        return original(sql, args)
    monkeypatch.setattr(store, 'execute', read_only)
    result = snapshot(store, clock()+dt.timedelta(hours=1), boundary)
    assert result['response']['state'] == 'saved_http_200'
    assert result['response']['matches_run'] is True
    assert result['response']['records'] == 2
    assert result['query']['offset'] == 500
    assert result['job']['offset'] == 502
    assert result['new_source_count'] == 0
    assert result['returned_dispositions']['current_states'] == {'Pending Verification': 1, 'Rejected': 1}
    assert result['request_reservations'] == 1 and result['provider_calls'] == 0
    assert 'must never leave storage' not in json.dumps(result)


def test_reservation_without_response_is_not_success(store, clock):
    key = seed(store, clock, received=0, cached=False)
    store.execute('INSERT INTO api_attempts VALUES (?,?,?,?)', (key, 'in_flight', clock().isoformat(), (clock()+dt.timedelta(minutes=15)).isoformat()))
    result = snapshot(store, clock())
    assert result['request_reservations'] == 1
    assert result['response']['state'] == 'no_saved_response'
    assert result['unresolved_attempt']['state'] == 'in_flight'


def test_old_response_never_proves_current_run(store, clock):
    seed(store, clock, old=True)
    result = snapshot(store, clock())
    assert result['response']['matches_run'] is False
    assert 'returned_dispositions' not in result


def test_no_receipt_does_not_run_or_create_a_job(store, clock):
    result = snapshot(store, clock())
    assert result['state'] == 'no_receipt' and result['provider_calls'] == 0
    assert store.execute('SELECT COUNT(*) FROM scan_jobs').fetchone()[0] == 0
