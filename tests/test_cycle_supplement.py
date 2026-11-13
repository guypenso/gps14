import datetime as dt,json
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import pytest
from rentalsourcing.config import Settings
from rentalsourcing.storage import Store,BudgetExceeded
from rentalsourcing.billing import cycle_key
from rentalsourcing.budget_activation import plan
from rentalsourcing.catch_up import run,ListingOnlyClient
from conftest import Provider,listing

def policy(settings):
    return replace(settings,approval_mode='monthly_budget',billing_anchor='2026-10-15T00:00:00Z',billing_baseline=40,provider_included_requests=50,monthly_budget_requests=50,per_month=50,supplemental_requests=10,approval_expires_at='2026-11-14T10:00:00Z',per_day=100,page_size=2)
def november(clock):clock.value=dt.datetime(2026,11,13,8,tzinfo=dt.timezone.utc);return clock

def test_plan_uses_actual_40_and_conservative_nov15_reset():
    p=plan(40,'2026-11-15',[{'period':'day:2026-10-16','used':4},{'period':'day:2026-11-12','used':27},{'period':'approval:old','used':27}],now=dt.datetime(2026,11,13,tzinfo=dt.timezone.utc))
    assert p['additional_requests']==10 and p['app_reserved_attempts_in_date_window']==31 and p['unreconciled_account_delta']==9
    assert p['cutoff']=='2026-11-14T10:00:00+00:00'
    assert p['environment']['BILLING_CYCLE_ANCHOR']=='2026-10-15T00:00:00+00:00'
    assert 'ENABLE_EMAIL' not in p['environment'] and 'ENABLE_RENTCAST' not in p['environment']
    with pytest.raises(ValueError):plan(50,'2026-11-15',[])

def test_baseline_grant_and_old_approval_are_not_reset(store,settings,clock):
    november(clock);s=policy(settings)
    for key,n in [('approval:old',27),('cycle:2026-10-14',27)]:store.execute('INSERT INTO usage VALUES (?,?)',(key,n))
    for _ in range(10):store.reserve_api(s,clock())
    assert store.budget_health(s,clock())['used']==50
    with pytest.raises(BudgetExceeded):store.reserve_api(replace(s,approval_id='renamed',billing_baseline=0),clock())
    assert store.count_usage('approval:old')==27 and store.count_usage('cycle:2026-10-14')==27
    assert store.count_usage('supplemental:2026-10-15T00:00:00+00:00')==10

def test_reconciliation_is_durable_and_does_not_spend_or_erase_old_counters(store,settings,clock):
    november(clock);s=policy(settings)
    for key,n in [('approval:old',27),('cycle:2026-10-14',27),('day:2026-10-16',4),('day:2026-10-27',27)]:store.execute('INSERT INTO usage VALUES (?,?)',(key,n))
    report=store.reconcile_provider_baseline(s,clock())
    assert report['app_reserved_attempts_in_date_window']==31 and report['unreconciled_account_delta']==9
    assert store.count_usage(cycle_key(s,clock()))==40 and store.count_usage('approval:old')==27
    assert store.budget_health(s,clock())['remaining']==10
    store.reserve_api(s,clock());store.reconcile_provider_baseline(s,clock())
    assert store.count_usage(cycle_key(s,clock()))==41

def test_provider_reconciliation_cannot_expand_additional_grant(store,settings,clock):
    november(clock);s=replace(policy(settings),billing_baseline=20)
    for _ in range(10):store.reserve_api(s,clock())
    with pytest.raises(BudgetExceeded):store.reserve_api(replace(s,billing_baseline=31),clock())

def test_cutoff_and_rollover_fail_closed(store,settings,clock):
    november(clock);s=policy(settings);store.reserve_api(s,clock());clock.value=dt.datetime(2026,11,14,10,tzinfo=dt.timezone.utc)
    assert store.budget_health(s,clock())['remaining']==0
    with pytest.raises(BudgetExceeded):store.reserve_api(s,clock())
    clock.advance(days=2)
    with pytest.raises(BudgetExceeded):store.reserve_api(s,clock())
    assert store.budget_health(s,clock())['remaining']==0

def test_request_scope_is_atomic_and_rollback_preserves_usage(store,settings,clock):
    november(clock);s=policy(settings);store.reserve_api(s,clock(),scope=('catchup:today',1))
    with pytest.raises(BudgetExceeded):store.reserve_api(s,clock(),scope=('catchup:today',1))
    assert store.count_usage(cycle_key(s,clock()))==41 and store.count_usage('day:2026-11-13')==1

def test_concurrent_last_request_never_exceeds_50(store,settings,clock):
    if not store.pg:pytest.skip('PostgreSQL concurrent connections')
    november(clock);s=replace(policy(settings),billing_baseline=49)
    def reserve(_):
        db=Store(store.test_url,initialize=False)
        try:
            try:db.reserve_api(s,clock());return True
            except BudgetExceeded:return False
        finally:db.close()
    with ThreadPoolExecutor(max_workers=4) as pool:assert sum(pool.map(reserve,range(4)))==1
    assert store.count_usage(cycle_key(s,clock()))==50

def test_catchup_one_listing_page_preserves_cursor_and_never_sends(store,boundary,settings,clock,monkeypatch):
    november(clock);s=policy(settings);provider=Provider([listing('new-'+str(i)) for i in range(4)])
    monkeypatch.setenv('ENABLE_PAID_TAX_VERIFICATION','true');monkeypatch.setenv('ENABLE_RENT_ESTIMATES','true')
    first=run(store,boundary,s,'2026-11-13',clock=clock,opener=provider,deliver=False)
    assert first['requests']==1 and first['listing_observations']==2 and len(first['new_source_ids'])==2
    assert len(provider.calls)==1 and '/listings/sale?' in provider.calls[0].full_url
    assert store.job(first['job_id'],'2026-11-13')['offset']==2
    assert not store.job(first['job_id'],'2026-11-13')['collection_done']
    assert run(store,boundary,s,'2026-11-13',clock=clock,opener=provider,deliver=False)['already_executed']
    assert len(provider.calls)==1 and store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0

def test_catchup_cannot_bypass_unactivated_approval(store,boundary,settings,clock):
    november(clock);p=Provider()
    with pytest.raises(BudgetExceeded):run(store,boundary,settings,'2026-11-13',clock=clock,opener=p,deliver=False)
    with pytest.raises(PermissionError):ListingOnlyClient(store,policy(settings),clock,p).get('/avm/rent/long-term')
    assert not p.calls

def test_build_check_does_not_execute_pending_catchup(settings,monkeypatch):
    from rentalsourcing import cloud
    monkeypatch.setenv('CATCH_UP_DISCOVERY_DAY','2026-11-13')
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.setattr(cloud.Settings,'from_env',lambda:policy(settings))
    monkeypatch.setattr('rentalsourcing.sync_probe.inspect',lambda:{'state':'fixture'})
    def forbidden(*a,**k):raise AssertionError('Build checks may not execute provider recovery')
    monkeypatch.setattr('rentalsourcing.catch_up.run',forbidden)
    cloud.main(['--check'])
