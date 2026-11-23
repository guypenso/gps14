"""Separate current-cycle approval and purpose budgets; no real provider calls."""
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from rentalsourcing.monthly_allocation import active, available, limits, listing_limit_now, listing_plan, period, rent_limit_now, snapshot
from rentalsourcing.partition_discovery import PREFIX, run
from rentalsourcing.municipal_coverage import UNIFIED
from rentalsourcing.storage import BudgetExceeded, Store
from test_partition_discovery import AreaProvider, MAIN
from conftest import Response, listing
from test_rental_intelligence import avm


def grant(settings):
    return replace(settings, approval_mode='monthly_budget', approval_id='fixture-nov15-only',
                   approved_requests=0, billing_anchor='2026-11-15T00:00:00Z',
                   billing_baseline=0, approval_expires_at='2026-12-15T00:00:00Z',
                   provider_cycle_verified_at='2026-11-15T00:30:00Z',
                   monthly_budget_requests=50, provider_included_requests=50,
                   per_month=50, per_day=4, per_run=4, supplemental_requests=0)


def test_new_cycle_needs_independent_grant_and_verified_baseline(store,settings,clock):
    clock.value=dt.datetime(2026,11,15,6,tzinfo=dt.timezone.utc)
    missing=replace(grant(settings),approval_expires_at='')
    assert not active(missing,clock()) and store.budget_health(missing,clock())['remaining']==0
    with pytest.raises(BudgetExceeded):store.reserve_api(missing,clock(),purpose='listing',scope=('multi-discovery:2026-11-15:morning',4))
    unverified=replace(grant(settings),provider_cycle_verified_at='')
    assert not active(unverified,clock())
    with pytest.raises(BudgetExceeded):store.reserve_api(unverified,clock(),purpose='listing',scope=('multi-discovery:2026-11-15:morning',4))
    old=replace(grant(settings),billing_anchor='2026-10-15T00:00:00Z',approval_expires_at='2026-11-14T10:00:00Z')
    assert not active(old,clock())
    with pytest.raises(BudgetExceeded):store.reserve_api(old,clock(),purpose='listing',scope=('multi-discovery:2026-11-15:morning',4))
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0


def test_50_request_cycle_is_allocated_and_paced_across_israel_slots(store,settings,clock):
    s=grant(settings)
    clock.value=dt.datetime(2026,11,15,6,tzinfo=dt.timezone.utc)
    assert limits(s)=={'listing':36,'rent':8,'targeted':4,'recovery':2}
    assert listing_limit_now(s,clock())==1
    scope=('multi-discovery:2026-11-15:morning',4)
    assert available(store,s,clock(),'listing')==1
    store.reserve_api(s,clock(),purpose='listing',scope=scope)
    assert available(store,s,clock(),'listing')==0
    with pytest.raises(BudgetExceeded):store.reserve_api(s,clock(),purpose='listing',scope=scope)
    clock.value=dt.datetime(2026,11,15,18,tzinfo=dt.timezone.utc)
    assert listing_limit_now(s,clock())==2
    store.reserve_api(s,clock(),purpose='listing',scope=('multi-discovery:2026-11-15:evening',4))
    assert snapshot(store,s,clock())['used']['listing']==2
    assert available(store,s,clock(),'listing')==0
    assert store.budget_health(s,clock())['used']==2


def test_rent_cannot_steal_listing_scope_and_tax_is_not_allocated(store,settings,clock):
    s=replace(grant(settings),per_day=10);clock.value=dt.datetime(2026,11,15,6,tzinfo=dt.timezone.utc)
    with pytest.raises(BudgetExceeded):store.reserve_api(s,clock(),purpose='rent',scope=('multi-discovery:2026-11-15:morning',4))
    with pytest.raises(BudgetExceeded):store.reserve_api(s,clock(),purpose='tax')
    assert rent_limit_now(s,clock())==1
    store.reserve_api(s,clock(),purpose='rent')
    with pytest.raises(BudgetExceeded):store.reserve_api(s,clock(),purpose='rent')
    assert available(store,s,clock(),'listing')==1
    clock.value=dt.datetime(2026,12,14,6,tzinfo=dt.timezone.utc)
    assert rent_limit_now(s,clock())==8
    for _ in range(7):store.reserve_api(s,clock(),purpose='rent')
    assert available(store,s,clock(),'rent')==0
    with pytest.raises(BudgetExceeded):store.reserve_api(s,clock(),purpose='rent')
    assert store.budget_health(s,clock())['used']==8


def test_cycle_expiry_does_not_reuse_the_same_approval(store,settings,clock):
    s=grant(settings);clock.value=dt.datetime(2026,11,15,6,tzinfo=dt.timezone.utc)
    store.reserve_api(s,clock(),purpose='rent')
    clock.value=dt.datetime(2026,12,15,0,tzinfo=dt.timezone.utc)
    assert store.budget_health(s,clock())['remaining']==0
    with pytest.raises(BudgetExceeded):store.reserve_api(s,clock(),purpose='rent')
    assert store.count_usage('cycle:2026-12-15T00:00:00+00:00')==0


def test_paced_pages_advance_the_same_all_area_query(store,official_boundary,settings,clock):
    s=replace(grant(settings),per_run=1)
    clock.value=dt.datetime(2026,11,15,6,tzinfo=dt.timezone.utc)
    provider=AreaProvider(main=8,bethel=8,pleasant=8)
    for day,hour,offset in [(15,6,0),(15,18,2),(16,6,4),(16,18,None),(17,6,6),(18,6,0)]:
        clock.value=dt.datetime(2026,11,day,hour,tzinfo=dt.timezone.utc)
        slot='morning' if hour==6 else 'evening'
        before=len(provider.calls)
        receipt,_=run(store,official_boundary,s,f'2026-11-{day:02}',slot,clock=clock,opener=provider)
        if offset is None:
            assert len(provider.calls)==before and receipt['requests_consumed']==0
            assert receipt['discovery_state']=='scheduled_pacing_no_listing_request'
        else:
            assert provider.calls[before:]==[(MAIN,offset)]
            assert receipt['requests_consumed']==1
            assert receipt['pages'][0]['partition']==UNIFIED
    assert store.budget_health(s,clock())['used']==5


def test_nov22_advance_preserves_separate_budgets_and_only_one_request(store,settings,clock):
    s=replace(grant(settings),approval_expires_at='2026-12-14T10:00:00Z')
    clock.value=dt.datetime(2026,11,22,18,2,tzinfo=dt.timezone.utc)
    store.execute('INSERT INTO usage VALUES (?,?)',(period(s,clock(),'listing'),10))
    store.execute('INSERT INTO usage VALUES (?,?)',(period(s,clock(),'rent'),3))
    store.execute('INSERT INTO usage VALUES (?,?)',('cycle:2026-11-15T00:00:00+00:00',13))
    store.save_setting(PREFIX+UNIFIED,{'last_success_at':'2026-11-21T06:01:31+00:00','query_complete':False,'offset':500})
    store.save_setting(PREFIX+'pleasant-hills',{'last_success_at':'2026-11-19T18:01:26+00:00','query_complete':True})
    plan=listing_plan(store,s,clock())
    assert plan['base_released']==10 and plan['available']==1
    assert plan['advance']['partition']==UNIFIED
    scope=('multi-discovery:2026-11-22:evening',4)
    if store.pg:
        def reserve(_):
            conn=Store(store.test_url,initialize=False)
            try:
                try:conn.reserve_api(s,clock(),purpose='listing',scope=scope);return 1
                except BudgetExceeded:return 0
            finally:conn.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert sum(pool.map(reserve,range(4)))==1
    else:
        store.reserve_api(s,clock(),purpose='listing',scope=scope)
        with pytest.raises(BudgetExceeded):store.reserve_api(s,clock(),purpose='listing',scope=scope)
    assert store.budget_health(s,clock())['remaining']==36
    assert snapshot(store,s,clock())['used']=={'listing':11,'rent':3,'targeted':0,'recovery':0}


def test_concurrent_reservations_are_bounded_by_atomic_rent_allocation(store,settings,clock):
    if not store.pg:pytest.skip('PostgreSQL transaction lock required')
    s=replace(grant(settings),per_day=10);clock.value=dt.datetime(2026,12,14,6,tzinfo=dt.timezone.utc)
    def reserve(_):
        db=Store(store.test_url,initialize=False)
        try:
            try:db.reserve_api(s,clock(),purpose='rent');return True
            except BudgetExceeded:return False
        finally:db.close()
    with ThreadPoolExecutor(max_workers=12) as pool:assert sum(pool.map(reserve,range(20)))==8
    assert store.budget_health(s,clock())['used']==8


def test_scheduled_listing_and_rent_use_separate_clients_and_ledgers(store,official_boundary,settings,clock,monkeypatch):
    from rentalsourcing.cloud import daily_discovery
    from rentalsourcing.collector import LIVE
    from rentalsourcing.rental_intelligence import cached_estimate
    s=replace(grant(settings),email_enabled=False)
    clock.value=dt.datetime(2026,11,15,6,tzinfo=dt.timezone.utc)
    p=listing('rent-candidate',addressLine1='100 Fixture Ave',zipCode='15222',latitude=40.4406,longitude=-79.9959)
    store.save_property(p['id'],p,'Pending Verification',[],LIVE,1,clock().isoformat())
    class DualProvider(AreaProvider):
        def __init__(self):super().__init__(main=0,bethel=0,pleasant=0);self.rent_calls=0
        def open(self,request,timeout=None):
            if '/v1/avm/rent/long-term?' in request.full_url:
                self.rent_calls+=1
                return Response(avm(p))
            return super().open(request,timeout)
    provider=DualProvider()
    monkeypatch.setenv('ENABLE_RENT_ESTIMATES','true')
    monkeypatch.delenv('ENABLE_REVIEW_INVENTORY_SYNC',raising=False)
    monkeypatch.delenv('ENABLE_COMMAND_CENTER_REVIEW_FEEDBACK',raising=False)
    result=daily_discovery(store,official_boundary,s,'2026-11-15',slot='morning',clock=clock,opener=provider)
    assert result['requests']==1 and result['rent_requests']==1
    assert len(provider.calls)==1 and provider.rent_calls==1
    assert cached_estimate(store,p,clock())['monthly']==2400
    assert store.budget_health(s,clock())['used']==2
    assert snapshot(store,s,clock())['used']=={'listing':1,'rent':1,'targeted':0,'recovery':0}


def test_first_unified_forecast_agrees_with_atomic_reservation(store,official_boundary,settings,clock):
    from rentalsourcing.municipal_coverage import unified_query
    from rentalsourcing.partition_discovery import _state

    cfg=replace(grant(settings),page_size=500,approval_expires_at='2026-12-14T10:00:00Z')
    clock.value=dt.datetime(2026,11,23,18,2,tzinfo=dt.timezone.utc)
    for key,used in [(period(cfg,clock(),'listing'),11),(period(cfg,clock(),'rent'),3),
                     ('cycle:2026-11-15T00:00:00+00:00',14)]:
        store.execute('INSERT INTO usage VALUES (?,?)',(key,used))
    forecast=listing_plan(store,cfg,clock())
    assert forecast['available']==1 and forecast['advance']=={
        'partition':UNIFIED,'reason':'first_unified_query','from_slot':'2026-11-24T08:00:00+02:00'}
    state=_state(store,UNIFIED,unified_query(official_boundary)[0])
    assert listing_plan(store,cfg,clock(),{UNIFIED:state})==forecast
    store.reserve_api(cfg,clock(),purpose='listing',scope=('multi-discovery:2026-11-23:evening',4))
    assert listing_plan(store,cfg,clock())['available']==0
    clock.value=dt.datetime(2026,11,24,6,tzinfo=dt.timezone.utc)
    # An observed first page stayed recent. The next release was already used.
    state.update(last_success_at='2026-11-23T18:02:00+00:00',query_complete=False,offset=500)
    store.save_setting(PREFIX+UNIFIED,state)
    assert listing_plan(store,cfg,clock())['available']==0
    assert snapshot(store,cfg,clock())['used']=={'listing':12,'rent':3,'targeted':0,'recovery':0}
