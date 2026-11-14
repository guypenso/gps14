"""Separate current-cycle approval and purpose budgets; no real provider calls."""
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from rentalsourcing.monthly_allocation import active, available, limits, listing_limit_now, rent_limit_now, snapshot
from rentalsourcing.partition_discovery import run
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


def test_one_paced_page_and_fair_rotation_without_provider_overrun(store,boundary,settings,clock):
    s=grant(settings);clock.value=dt.datetime(2026,11,15,6,tzinfo=dt.timezone.utc)
    provider=AreaProvider(main=8,bethel=8,pleasant=8)
    first,_=run(store,boundary,s,'2026-11-15','morning',clock=clock,opener=provider)
    assert first['requests_consumed']==1 and provider.calls==[(MAIN,0)]
    clock.value=dt.datetime(2026,11,15,18,tzinfo=dt.timezone.utc)
    second,_=run(store,boundary,s,'2026-11-15','evening',clock=clock,opener=provider)
    assert second['requests_consumed']==1 and provider.calls[-1]==('Bethel Park',0)
    clock.value=dt.datetime(2026,11,16,6,tzinfo=dt.timezone.utc)
    third,_=run(store,boundary,s,'2026-11-16','morning',clock=clock,opener=provider)
    assert third['requests_consumed']==0 and third['discovery_state']=='skipped_budget_exhausted'
    clock.value=dt.datetime(2026,11,16,18,tzinfo=dt.timezone.utc)
    fourth,_=run(store,boundary,s,'2026-11-16','evening',clock=clock,opener=provider)
    assert fourth['requests_consumed']==1 and provider.calls[-1]==('Pleasant Hills',0)
    clock.value=dt.datetime(2026,11,17,18,tzinfo=dt.timezone.utc)
    fifth,_=run(store,boundary,s,'2026-11-17','evening',clock=clock,opener=provider)
    assert fifth['requests_consumed']==1 and provider.calls[-1]==(MAIN,2)
    clock.value=dt.datetime(2026,11,18,6,tzinfo=dt.timezone.utc)
    sixth,_=run(store,boundary,s,'2026-11-18','morning',clock=clock,opener=provider)
    assert sixth['requests_consumed']==1 and provider.calls[-1]==(MAIN,4)


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


def test_scheduled_listing_and_rent_use_separate_clients_and_ledgers(store,boundary,settings,clock,monkeypatch):
    from rentalsourcing.cloud import daily_discovery
    from rentalsourcing.collector import LIVE
    from rentalsourcing.rental_intelligence import cached_estimate
    s=replace(grant(settings),email_enabled=False)
    clock.value=dt.datetime(2026,11,15,6,tzinfo=dt.timezone.utc)
    p=listing('rent-candidate',addressLine1='100 Fixture Ave')
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
    result=daily_discovery(store,boundary,s,'2026-11-15',slot='morning',clock=clock,opener=provider)
    assert result['requests']==1 and result['rent_requests']==1
    assert len(provider.calls)==1 and provider.rent_calls==1
    assert cached_estimate(store,p,clock())['monthly']==2400
    assert store.budget_health(s,clock())['used']==2
    assert snapshot(store,s,clock())['used']=={'listing':1,'rent':1,'targeted':0,'recovery':0}
