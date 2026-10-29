"""Frozen digest snapshots and optional, best-effort backend synchronization.

No provider requests, email reads, or email-body changes. Only accepted daily
outbox messages can be exported; ambiguous/failed deliveries stay unpublished.
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import urllib.parse
import urllib.request

from .core import EXCLUDED_ZIPS
from .request import NoRedirect


def configuration():
    if os.getenv('ENABLE_COMMAND_CENTER_SYNC', 'false').lower() != 'true':
        return None
    url = os.getenv('COMMAND_CENTER_SYNC_URL', '')
    secret = os.getenv('COMMAND_CENTER_SYNC_SECRET', '')
    service_token = os.getenv('COMMAND_CENTER_SERVICE_TOKEN', '')
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment or
            parsed.path != '/api/rentalsourcing/sync' or
            len(secret) < 32 or not service_token):
        raise ValueError('Command Center requires its HTTPS sync endpoint, a 32+ character signing secret, and Sites service access')
    return url, secret, service_token


def snapshot(store, identity, day, items, stats, now):
    """Called inside the SAME transaction that freezes the email selection."""
    from .digest import listing_link
    opportunities = []
    for entry, section in [(x, x['kind']) for x in items] + [(x, 'pending_verification') for x in stats.get('pending_properties', [])]:
        p = entry['listing']
        if str(p.get('zipCode', '')).split('-')[0] in EXCLUDED_ZIPS:
            continue
        tax = entry.get('tax')
        first = store.execute('SELECT MIN(observed_at) FROM price_history WHERE property_id=?', (p.get('id'),)).fetchone()[0]
        observed = entry.get('listing_observed_at') or entry.get('observed_at') or entry.get('verified_at')
        opportunities.append(dict(
            source_id=p.get('id'), source='RentCast', source_label='RentCast Sale Listings',
            address=p.get('formattedAddress'), street_address=p.get('addressLine1'),
            unit=p.get('addressLine2'), city=p.get('city'), state=p.get('state'),
            zip_code=p.get('zipCode'), price=p.get('price'), bedrooms=p.get('bedrooms'),
            bathrooms=p.get('bathrooms'), square_feet=p.get('squareFootage'),
            annual_tax=tax['annual'] if tax else None, tax_year=tax['year'] if tax else None,
            tax_status='provider_verified' if tax else 'pending_verification',
            tax_source='RentCast Property Records' if tax else None,
            qualification='pending_verification' if section == 'pending_verification' else 'verified',
            section=section, previous_price=entry.get('previous_price'),
            reasons=entry.get('reasons', []), listing_url=listing_link(p),
            discovered_at=first or observed or now.isoformat(), updated_at=observed or now.isoformat(),
            listing_observed_at=observed, verified_at=entry.get('verified_at'),
            tax_fetched_at=entry.get('tax_fetched_at')))
    body = json.dumps(dict(schema_version=1, report_id=identity, report_date=day,
                          prepared_at=now.isoformat(), discovery_state=stats.get('discovery_state', 'stored' if stats.get('saved_data_only') else 'unknown'),
                          opportunities=opportunities), ensure_ascii=False, separators=(',', ':'))
    store.execute('INSERT INTO command_center_reports (outbox_id,payload,attempts) VALUES (?,?,0) ON CONFLICT(outbox_id) DO UPDATE SET payload=excluded.payload WHERE command_center_reports.synced_at IS NULL', (identity, body))


def flush(store, now=None, opener=None):
    """At most three pending deliveries; retry on a later existing daily run.

    Called AFTER email handling, outside its acceptance/error transaction. HTTP
    errors cannot change sent email state. No redirects or immediate retries.
    """
    config = configuration()
    if not config:
        return 0
    url, secret, service_token = config
    now = now or dt.datetime.now(dt.timezone.utc)
    opener = opener or urllib.request.build_opener(NoRedirect)
    rows = store.execute("SELECT r.outbox_id,r.payload FROM command_center_reports r JOIN outbox o ON o.id=r.outbox_id WHERE o.state='sent' AND o.job_id LIKE ? AND r.synced_at IS NULL ORDER BY o.job_id LIMIT 3", ('daily:%',)).fetchall()
    accepted = 0
    for identity, payload in rows:
        body = payload.encode('utf-8')
        timestamp = str(int(now.timestamp()))
        signature = hmac.new(secret.encode(), timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
        store.execute('UPDATE command_center_reports SET attempts=attempts+1,last_attempt_at=? WHERE outbox_id=?', (now.isoformat(), identity))
        req = urllib.request.Request(url, data=body, method='POST', headers={
            'Content-Type': 'application/json', 'X-RentalSourcing-Timestamp': timestamp,
            'X-RentalSourcing-Signature': 'v1=' + signature,
            'OAI-Sites-Authorization': 'Bearer ' + service_token})
        try:
            with opener.open(req, timeout=20) as response:
                result = json.loads(response.read(4096))
                if response.status != 200 or result.get('report_id') != identity or result.get('accepted') is not True:
                    raise ValueError('Invalid synchronization receipt')
            store.execute('UPDATE command_center_reports SET synced_at=?,error_type=NULL WHERE outbox_id=?', (now.isoformat(), identity))
            accepted += 1
        except Exception as exc:
            # Never persist raw HTTP errors, response bodies, URLs or headers.
            store.execute('UPDATE command_center_reports SET error_type=? WHERE outbox_id=?', (type(exc).__name__, identity))
    return accepted
