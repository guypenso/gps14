import datetime as dt
import io
import json
import urllib.error
from dataclasses import replace
import pytest
from rentalsourcing.schedule import scheduled_day
from rentalsourcing.storage import BudgetExceeded
from rentalsourcing.collector import Client
from rentalsourcing.digest import Digest
from rentalsourcing.config import Settings
from rentalsourcing.request import diagnose
from rentalsourcing.costs import api_costs,monthly_requests
from conftest import Provider,listing


def test_every_day_has_exactly_one_israel_eight_am_slot():
    for year in (2026,2027,2028):
        day=dt.date(year,1,1)
        while day.year==year:
            slots=[dt.datetime.combine(day,dt.time(h),tzinfo=dt.timezone.utc) for h in (5,6)]
            assert sum(scheduled_day(t) is not None for t in slots)==1
            day+=dt.timedelta(days=1)


def test_api_requires_explicit_approval_before_network(store,settings,clock,provider):
    c=Client(store,replace(settings,approved_requests=0),clock,provider)
    with pytest.raises(BudgetExceeded):c.get('/listings/sale',{'limit':2,'offset':0})
    assert provider.calls==[]


def test_persistent_approval_cap_does_not_reset_at_billing_boundary(store,settings,clock):
    limited=replace(settings,approved_requests=1)
    store.reserve_api(limited,clock());clock.advance(days=32)
    with pytest.raises(BudgetExceeded):store.reserve_api(limited,clock())


def test_daily_limit_rollback_does_not_consume_other_quotas(store,settings,clock):
    limited=replace(settings,per_day=1)
    store.reserve_api(limited,clock())
    with pytest.raises(BudgetExceeded):store.reserve_api(limited,clock())
    assert store.count_usage('approval:'+settings.approval_id)==1


def test_budget_shortfall_is_persisted_before_exhaustion(store,settings,clock):
    health=store.budget_health(replace(settings,per_month=10),clock())
    assert health['warning'] and store.operations()


def test_listing_cache_avoids_second_network_request(store,settings,clock,provider):
    c=Client(store,settings,clock,provider,lambda _:None)
    params={'limit':2,'offset':0}
    c.get('/listings/sale',params);c.get('/listings/sale',params)
    assert len(provider.calls)==1 and c.cache_hits==1
    clock.advance(minutes=16);c.get('/listings/sale',params)
    assert len(provider.calls)==2


def test_bounded_network_retries_each_consume_approval(store,settings,clock,provider):
    provider.failures=[urllib.error.URLError(TimeoutError('fixture'))]
    c=Client(store,replace(settings,retries=1),clock,provider,lambda _:None)
    c.get('/listings/sale',{'limit':2,'offset':0})
    assert c.count==2 and store.count_usage('approval:'+settings.approval_id)==2


def test_proxy_error_retains_reason_without_secret(monkeypatch):
    monkeypatch.setenv('RENTCAST_API_KEY','fixture-secret')
    d=diagnose(urllib.error.URLError(OSError('Tunnel connection failed: 403 fixture-secret')))
    assert d['stage']=='proxy_connect' and '[REDACTED]' in d['reason'] and 'fixture-secret' not in str(d)


def test_atomic_migrations_are_repeatable(store):
    store.migrate();store.migrate()
    assert [r[0] for r in store.execute('SELECT version FROM schema_versions ORDER BY version').fetchall()]==[2,3]


def test_offline_cost_arithmetic():
    assert monthly_requests(501,10)==360
    assert api_costs(930)['Foundation']==74


def test_email_requires_flag_and_secret(store,boundary,settings,clock):
    d=Digest(store,boundary,replace(settings,email_enabled=False),clock)
    assert d.prepare('x','2026-10-16',{}) is None
    with pytest.raises(PermissionError):d.send('missing')


def test_settings_reject_invalid_limits():
    with pytest.raises(ValueError):Settings(per_run=-1)
    with pytest.raises(ValueError):Settings(billing_day=31)


def test_postgres_lock_and_persistence_across_connections(store):
    if not store.pg:pytest.skip('PostgreSQL advisory locks require PostgreSQL')
    import psycopg
    from rentalsourcing.storage import Store
    # The test DSN contains an isolated search_path so reconnects see the same schema.
    other=Store(store.test_url)
    try:
        assert store.lock() is True
        assert other.lock() is False
        store.save_setting('persisted',{'scan_hour':8})
        assert json.loads(other.execute('SELECT payload FROM settings WHERE name=?',('persisted',)).fetchone()[0])=={'scan_hour':8}
    finally:other.close()


def test_dns_and_tls_diagnostics_are_distinguished():
    import socket,ssl
    assert diagnose(urllib.error.URLError(socket.gaierror(-2,'fixture DNS error')))['stage']=='dns'
    assert diagnose(urllib.error.URLError(ssl.SSLCertVerificationError(1,'fixture cert error')))['stage']=='tls'


def test_rate_limit_retry_cannot_exceed_approved_allowance(store,settings,clock,provider):
    provider.failures=[urllib.error.HTTPError('https://api.rentcast.io/v1/listings/sale',429,'limit',{},None)]
    c=Client(store,replace(settings,retries=1,approved_requests=1),clock,provider,lambda _:None)
    with pytest.raises(BudgetExceeded):c.get('/listings/sale',{'limit':2,'offset':0})
    assert len(provider.calls)==1


def test_http_error_status_and_safe_category_are_recorded(store,settings,clock,provider):
    import io
    provider.failures=[urllib.error.HTTPError('https://api.rentcast.io/v1/listings/sale',403,'Forbidden',{},io.BytesIO(b'{"error":"billing/subscription-inactive","secret":"fixture-key-not-real"}'))]
    c=Client(store,settings,clock,provider,lambda _:None)
    with pytest.raises(urllib.error.HTTPError):c.get('/listings/sale',{'limit':2,'offset':0})
    assert c.last_error['http_status']==403 and c.last_error['body_category']=='billing'
    assert 'fixture-key-not-real' not in str(c.last_error)
    assert store.operations()


def test_recurring_approval_never_resets_early_at_billing_boundary(store,settings,clock):
    s=replace(settings,approval_mode='rolling_32_days',approved_requests=1)
    store.reserve_api(s,clock());clock.advance(days=20)
    with pytest.raises(BudgetExceeded):store.reserve_api(s,clock())
    clock.advance(days=12)
    store.reserve_api(s,clock())
    assert store.rolling_usage(clock())==1


def test_one_time_approval_still_never_renews(store,settings,clock):
    s=replace(settings,approval_mode='one_time',approved_requests=1)
    store.reserve_api(s,clock());clock.advance(days=40)
    with pytest.raises(BudgetExceeded):store.reserve_api(s,clock())


def test_recurring_schedule_reserves_daily_listings_across_months(store,settings,clock):
    s=replace(settings,approval_mode='rolling_32_days',approved_requests=40,per_month=40,per_day=4,per_run=4)
    for _ in range(95):
        allowance=store.scheduled_allowance(s,clock())
        assert allowance>=1,clock().isoformat()
        for attempt in range(allowance):store.reserve_api(s,clock())
        assert store.rolling_usage(clock())<=40
        clock.advance(days=1)


def test_one_time_schedule_leaves_room_for_tax_when_allowance_exceeds_dates(store,settings,clock):
    remaining_dates=store.budget_health(settings,clock())['days_remaining']
    s=replace(settings,approved_requests=remaining_dates+5,per_month=remaining_dates+5,per_run=4)
    assert store.scheduled_allowance(s,clock())==2
