"""Offline/isolated-Postgres production safety checks; providers are mocked."""
import datetime as dt
from dataclasses import replace
import pytest
from rentalsourcing.billing import billing_window,cycle_key
from rentalsourcing.collector import Client,RequestDeferred,LIVE
from rentalsourcing.storage import BudgetExceeded,Store
from rentalsourcing.digest import Digest
from rentalsourcing.engine import Engine
from rentalsourcing.audit import analyze
from conftest import Provider,listing,taxes


def subscription(settings,**kwargs):
    return replace(settings,approval_mode='billing_cycle',billing_anchor='2026-10-01T00:00:00Z',
        billing_baseline=0,approved_requests=200,per_month=200,**kwargs)


def test_subscription_requires_actual_anchor_and_usage_baseline(settings):
    with pytest.raises(ValueError,match='ANCHOR'):replace(settings,approval_mode='billing_cycle')
    with pytest.raises(ValueError,match='BASELINE'):
        replace(settings,approval_mode='billing_cycle',billing_anchor='2026-10-01T00:00:00Z')
    with pytest.raises(ValueError,match='timezone'):
        replace(settings,approval_mode='billing_cycle',billing_anchor='2026-10-01',billing_baseline=0)


def test_anniversary_does_not_drift_after_short_month(settings):
    s=replace(subscription(settings),billing_anchor='2028-01-31T15:45:00Z')
    before=dt.datetime(2028,2,29,15,44,tzinfo=dt.timezone.utc)
    start,end,_=billing_window(s,before)
    assert start.isoformat()=='2028-01-31T15:45:00+00:00'
    assert end.isoformat()=='2028-02-29T15:45:00+00:00'
    start,end,_=billing_window(s,before+dt.timedelta(minutes=1))
    assert start.day==29 and end.day==31 and end.month==3


def test_cycle_approval_renews_only_at_exact_timestamp(store,settings,clock):
    s=replace(subscription(settings),billing_anchor='2026-10-16T12:00:00Z',approved_requests=1)
    store.reserve_api(s,clock())
    clock.advance(days=31);clock.value=clock.value.replace(hour=11,minute=59)
    with pytest.raises(BudgetExceeded):store.reserve_api(s,clock())
    clock.advance(minutes=1)
    store.reserve_api(s,clock())


def test_baseline_and_approval_rename_cannot_reset_cycle(store,settings,clock):
    s=replace(subscription(settings),billing_baseline=198)
    store.reserve_api(s,clock());store.reserve_api(s,clock())
    with pytest.raises(BudgetExceeded):store.reserve_api(replace(s,approval_id='renamed',billing_baseline=0),clock())
    assert store.count_usage(cycle_key(s,clock()))==200
    assert store.count_usage('day:2026-10-16')==2
    assert store.budget_health(s,clock())['remaining']==0


def test_tax_work_cannot_spend_future_listing_reserve(store,settings,clock):
    s=replace(subscription(settings),approved_requests=17,per_month=17)
    # Oct 16..31: spend today's listing plus one tax, keep Oct 17..31.
    store.reserve_api(s,clock())
    store.reserve_api(s,clock(),tax_request=True)
    with pytest.raises(BudgetExceeded,match='reserved'):store.reserve_api(s,clock(),tax_request=True)
    assert store.count_usage(cycle_key(s,clock()))==2


def test_failed_request_is_not_repeated_immediately_after_restart(store,settings,clock):
    provider=Provider();provider.failures=[TimeoutError('unknown delivery')]
    with pytest.raises(TimeoutError):Client(store,settings,clock,provider,lambda _:None).get('/properties/fixture-property')
    with pytest.raises(RequestDeferred):Client(store,settings,clock,provider,lambda _:None).get('/properties/fixture-property')
    assert len(provider.calls)==1
    assert store.count_usage('approval:'+settings.approval_id)==1
    clock.advance(minutes=16)
    Client(store,settings,clock,provider,lambda _:None).get('/properties/fixture-property')
    assert len(provider.calls)==2


def test_response_cache_recovers_tax_without_another_billed_request(store,boundary,settings,clock):
    provider=Provider()
    Client(store,settings,clock,provider,lambda _:None).get('/properties/fixture-property')
    # Simulate a crash after durable transport caching, before saving tax_history.
    assert store.get_tax('fixture-property',clock().isoformat()) is None
    original=clock().isoformat();clock.advance(hours=1)
    stats=Engine(store,boundary,Client(store,settings,clock,provider,lambda _:None),settings,clock).run('recover-tax',clock().date().isoformat())
    assert provider.tax_calls==1
    assert store.get_property('fixture-property')['state']=='Qualified'
    assert store.get_tax('fixture-property',clock().isoformat())[2]==original


def test_incomplete_tax_cache_can_be_configured_for_weekly_recheck(store,boundary,settings,clock):
    provider=Provider();provider.records['fixture-property']={'id':'fixture-property'}
    s=replace(settings,negative_tax_cache_hours=168)
    for day in range(3):
        Engine(store,boundary,Client(store,s,clock,provider,lambda _:None),s,clock).run('day:'+str(day),clock().date().isoformat())
        clock.advance(days=1)
    assert provider.tax_calls==1


@pytest.mark.parametrize('missing',['approval','secret'])
def test_email_stays_blocked_without_both_approval_and_resend(store,boundary,settings,clock,monkeypatch,missing):
    s=replace(settings,email_approval_id='' if missing=='approval' else 'approved')
    if missing=='secret':monkeypatch.delenv('RESEND_API_KEY',raising=False)
    provider=Provider();digest=Digest(store,boundary,s,clock,provider)
    assert digest.prepare('job',clock().date().isoformat(),{}) is None
    with pytest.raises((PermissionError,ValueError)):digest.send('not-queued')
    assert provider.calls==[]
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0


def test_database_audit_distinguishes_tax_work_from_missing_listing_fields(store,boundary,settings,clock):
    now=clock().isoformat();expiry=(clock()+dt.timedelta(days=2)).isoformat()
    for identity,payload in [('needs-tax',listing('needs-tax')),('cached',listing('cached')),('missing',listing('missing',bedrooms=None))]:
        store.save_property(identity,payload,'Pending Verification',['tax_not_verified'],LIVE,1,now)
    store.save_tax('cached',taxes('cached'),LIVE,now,expiry)
    before=store.execute('SELECT COUNT(*) FROM operations').fetchone()[0]
    audit=analyze(store,boundary,settings,clock())
    assert audit['total']==3
    assert audit['pending']=={'tax_requests_needed_first_pass':1,'valid_tax_cache_no_request':1,'listing_or_source_requires_refresh':1}
    assert store.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==before


def test_concurrent_identical_get_uses_one_request_and_one_reservation(store,settings,clock):
    if not store.pg:pytest.skip('Concurrent sessions need PostgreSQL')
    from test_postgres import simultaneous
    provider=Provider()
    def fetch(other):
        return Client(other,settings,clock,provider,lambda _:None).get('/properties/fixture-property')[0]['id']
    results=simultaneous(store,fetch)
    assert results==['fixture-property']*8
    assert provider.tax_calls==1
    assert store.count_usage('approval:'+settings.approval_id)==1


def test_reserve_accounts_for_renewal_day_scan_before_afternoon_reset(settings):
    from rentalsourcing.billing import future_listing_days
    now=dt.datetime(2026,10,16,12,tzinfo=dt.timezone.utc)
    s=replace(subscription(settings),billing_anchor='2026-10-01T15:00:00Z')
    _,end,_=billing_window(s,now)
    # November 1 08:00 EST (13 UTC) precedes the 15 UTC renewal.
    assert future_listing_days(now,end)==16


def test_one_unavailable_tax_record_does_not_stop_other_properties(store,boundary,settings,clock):
    import urllib.error
    provider=Provider([listing('a-missing'),listing('b-valid')])
    original_open=provider.open
    def missing(request,timeout=None):
        if request.full_url.endswith('/properties/a-missing'):
            provider.calls.append(request)
            raise urllib.error.HTTPError(request.full_url,404,'not found',{},None)
        return original_open(request,timeout)
    provider.open=missing
    s=replace(settings,negative_tax_cache_hours=168)
    client=Client(store,s,clock,provider,lambda _:None)
    result=Engine(store,boundary,client,s,clock).run('404',clock().date().isoformat())
    assert result['partial']
    assert store.get_property('b-valid')['state']=='Qualified'
    assert store.get_property('a-missing')['state']=='Pending Verification'
    count=len(provider.calls);clock.advance(days=1)
    with pytest.raises(RequestDeferred):Client(store,s,clock,provider,lambda _:None).get('/properties/a-missing')
    assert len(provider.calls)==count


def test_read_only_audit_command_does_not_modify_database(store,boundary,settings,clock):
    if not store.pg:pytest.skip('PostgreSQL read-only enforcement')
    other=Store(store.test_url,initialize=False)
    try:
        other.execute('SET default_transaction_read_only = on')
        assert analyze(other,boundary,settings,clock())['total']==0
    finally:other.close()


def test_178_backlog_with_two_new_candidates_daily_fits_two_cycles(tmp_path,boundary,settings,clock):
    # Planning scenario only: every record is tax-verifiable, one listing page,
    # two new candidates/day, and one failed attempt every fourth day.
    db=Store('sqlite:///'+str(tmp_path/'scenario.sqlite'));db.migrate()
    try:
        clock.value=dt.datetime(2026,11,1,13,tzinfo=dt.timezone.utc)
        s=replace(subscription(settings),billing_anchor='2026-11-01T00:00:00Z',per_run=8,per_day=8,page_size=500,retries=1,tax_cache_hours=720)
        provider=Provider([listing('old-'+str(i)) for i in range(178)])
        seed=Engine(db,boundary,Client(db,s,clock,provider,lambda _:None),s,clock)
        db.job('seed','2026-10-31')
        for row in provider.rows:seed.ingest('seed',row)
        for day in range(62):
            for i in range(2):
                identity=f'new-{day}-{i}'
                provider.rows.append(listing(identity));provider.records[identity]=taxes(identity)
            if day%4==0:provider.failures=[TimeoutError('planning retry')]
            allowance=db.scheduled_allowance(s,clock())
            if allowance:
                run_settings=replace(s,per_run=allowance)
                Engine(db,boundary,Client(db,run_settings,clock,provider,lambda _:None),run_settings,clock).run('daily:'+str(day),clock().date().isoformat())
            assert db.count_usage(cycle_key(s,clock()))<=200
            assert db.count_usage(clock().strftime('day:%Y-%m-%d'))<=8
            left=db.execute("SELECT COUNT(*) FROM properties p WHERE p.id LIKE 'old-%' AND NOT EXISTS (SELECT 1 FROM tax_history t WHERE t.property_id=p.id)").fetchone()[0]
            if left==0:break
            clock.advance(days=1)
        assert left==0
        assert day+1<=62
        print(f'Synthetic backlog first pass completed in {day+1} days')
    finally:db.close()
