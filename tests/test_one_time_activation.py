"""Non-renewing remainder allowance: no provider timestamps invented or requests sent."""
import datetime as dt
from dataclasses import replace
from conftest import Provider
from rentalsourcing.cloud import daily_discovery
from rentalsourcing.storage import BudgetExceeded
import pytest


def remainder(settings):
    return replace(settings,approval_mode='one_time',approval_id='FICTIONAL-owner-approved-remainder',approved_requests=27,
                   billing_anchor='',billing_day=14,billing_baseline=-1,per_month=40,per_day=4,per_run=4,retries=0)


def test_one_time_approved_daily_discovers_without_unknown_billing_anchor(store,boundary,settings,clock):
    clock.value=dt.datetime(2026,10,27,6,tzinfo=dt.timezone.utc)
    p=Provider();s=remainder(settings)
    result=daily_discovery(store,boundary,s,'2026-10-27',clock=clock,opener=p)
    assert result['scan']=='fresh' and result['status']=='sent'
    assert result['requests']==2 and p.tax_calls==1 and len(p.messages)==1
    assert store.count_usage('approval:'+s.approval_id)==2
    again=daily_discovery(store,boundary,s,'2026-10-27',clock=clock,opener=p)
    assert again['requests']==0 and len(p.messages)==1


def test_lifetime_cap_preserves_daily_scans_and_never_renews_with_local_month(store,settings):
    s=remainder(settings);days=[]
    for index in range(25):
        now=dt.datetime(2026,10,27,6,tzinfo=dt.timezone.utc)+dt.timedelta(days=index)
        allowance=store.scheduled_allowance(s,now)
        for call in range(allowance):store.reserve_api(s,now,tax_request=call>0)
        days.append(allowance)
        assert 13+store.count_usage('approval:'+s.approval_id)<=40
    assert all(n>=1 for n in days[:18])
    assert sum(days)==27 and sum(max(0,n-1) for n in days)==9
    assert all(n==0 for n in days[18:])
    with pytest.raises(BudgetExceeded):store.reserve_api(s,now)
    assert store.count_usage('approval:'+s.approval_id)==27
