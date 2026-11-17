import datetime as dt
import json
from dataclasses import replace

from conftest import listing
from test_monthly_allocation import grant
from rentalsourcing.collector import LIVE,Client
from rentalsourcing.monthly_allocation import purpose_readiness,period
from rentalsourcing.rental_intelligence import enrich,job_key,cached_estimate,job_summary
from test_rental_intelligence import avm


def test_november_17_used_credit_resumes_in_next_authorized_window_without_spending(store,boundary,clock,settings,monkeypatch):
    clock.value=dt.datetime(2026,11,17,15,tzinfo=dt.timezone.utc)
    s=replace(grant(settings),approval_expires_at='2026-12-14T10:00:00+00:00')
    store.execute('INSERT INTO usage VALUES (?,?)',(period(s,clock(),'rent'),1))
    p=listing('new-rent-candidate',addressLine1='100 Fixture Ave')
    store.save_property(p['id'],p,'Pending Verification',[],LIVE,1,clock().isoformat())
    monkeypatch.setenv('ENABLE_RENT_ESTIMATES','true')
    before=store.execute('SELECT period,used FROM usage ORDER BY period').fetchall()
    ready=purpose_readiness(store,s,clock(),'rent')
    assert ready['state']=='scheduled' and ready['reason']=='monthly_rent_pacing'
    assert ready['next_at']=='2026-11-18T06:00:00+00:00'
    result=enrich(store,boundary,s,clock())
    job=json.loads(store.execute('SELECT payload FROM settings WHERE name=?',(job_key(p),)).fetchone()[0])
    assert job['state']=='Retry Scheduled' and job['next_at']==ready['next_at']
    assert result['blocked_by_budget']==0 and result['retry_scheduled']==1
    assert store.execute('SELECT period,used FROM usage ORDER BY period').fetchall()==before
    clock.value=dt.datetime(2026,11,18,6,tzinfo=dt.timezone.utc)
    class Provider:
        calls=0
        def open(self,request,timeout):
            from conftest import Response
            self.calls+=1;return Response(avm(p))
    provider=Provider();client=Client(store,s,clock,provider,lambda _:None)
    outcome=enrich(store,boundary,s,clock(),client)
    assert outcome['retrieved']==1 and provider.calls==1
    assert cached_estimate(store,p,clock())['monthly']==2400


def test_purpose_cap_and_expiry_do_not_create_false_future_approval(store,clock,settings):
    clock.value=dt.datetime(2026,11,17,15,tzinfo=dt.timezone.utc);s=grant(settings)
    store.execute('INSERT INTO usage VALUES (?,?)',(period(s,clock(),'rent'),8))
    r=purpose_readiness(store,s,clock(),'rent')
    assert r['state']=='blocked' and r['reason']=='rent_purpose_allocation_exhausted' and r['next_at'] is None
    clock.value=dt.datetime(2026,12,15,6,tzinfo=dt.timezone.utc)
    assert purpose_readiness(store,s,clock(),'rent')['next_at'] is None


def test_historical_owner_rejection_is_preserved_and_not_counted_as_active_enrichment(store,boundary,clock,settings,monkeypatch):
    clock.value=dt.datetime(2026,11,17,15,tzinfo=dt.timezone.utc);s=grant(settings)
    p=listing('owner-rejected-rent',addressLine1='100 Fixture Ave')
    store.save_property(p['id'],p,'Pending Verification',[],LIVE,1,clock().isoformat())
    decision={'status':'rejected','reviewed_at':clock().isoformat(),'reviewed_by':'guy'}
    store.save_setting('penten_review:'+p['id'],decision)
    store.save_setting(job_key(p),{'source_id':p['id'],'state':'Budget Blocked','attempts':0,'reason':'previous_approval_expired'})
    monkeypatch.setenv('ENABLE_RENT_ESTIMATES','true')
    assert enrich(store,boundary,s,clock())['paid_calls']==0
    report=job_summary(store)
    assert report['inactive_history']==1 and report['active']==0
    assert json.loads(store.execute('SELECT payload FROM settings WHERE name=?',('penten_review:'+p['id'],)).fetchone()[0])==decision
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0


def test_provider_latency_does_not_reject_a_just_acquired_estimate(store,boundary,clock,settings,monkeypatch):
    from conftest import Response
    clock.value=dt.datetime(2026,11,18,6,tzinfo=dt.timezone.utc);s=grant(settings)
    p=listing('elapsed-avm-fixture',addressLine1='100 Fixture Ave')
    store.save_property(p['id'],p,'Pending Verification',[],LIVE,1,clock().isoformat())
    monkeypatch.setenv('ENABLE_RENT_ESTIMATES','true')
    began=clock()
    class DelayedProvider:
        calls=0
        def open(self,request,timeout):
            self.calls+=1;clock.advance(seconds=7);return Response(avm(p))
    provider=DelayedProvider();client=Client(store,s,clock,provider,lambda _:None)
    result=enrich(store,boundary,s,began,client)
    assert result['retrieved']==1 and result['invalid']==0 and provider.calls==1
    assert cached_estimate(store,p,clock())['observed_at']==clock().isoformat()


def test_interrupted_persistence_recovers_cache_without_another_request_or_manual_reclassification(store,boundary,clock,settings):
    import hashlib,urllib.parse
    from rentalsourcing.rental_intelligence import ENDPOINT,parameters,key
    p=listing('cached-avm-fixture',addressLine1='100 Fixture Ave')
    store.save_property(p['id'],p,'Pending Verification',[],LIVE,1,clock().isoformat())
    decision={'status':'missed_under_contract','reviewed_at':clock().isoformat()}
    store.save_setting('penten_review:'+p['id'],decision)
    url=ENDPOINT+'?'+urllib.parse.urlencode(sorted(parameters(p).items()))
    observed=clock().isoformat()
    store.cache_put(hashlib.sha256(url.encode()).hexdigest(),{'data':avm(p),'fetched_at':observed,'total':None},LIVE,(clock()+dt.timedelta(hours=6)).isoformat())
    clock.advance(hours=22)
    assert store.execute('SELECT payload FROM settings WHERE name=?',(key(p),)).fetchone() is None
    outcome=enrich(store,boundary,settings,clock())
    assert outcome['cached']==1 and outcome['paid_calls']==0
    deliveries=store.execute('SELECT payload FROM settings WHERE name LIKE ?',('listing-contact-enrichment:rent:%',)).fetchall()
    assert len(deliveries)==1 and json.loads(deliveries[0][0])['source_ids']==[p['id']]
    assert json.loads(deliveries[0][0])['done'] is False
    enrich(store,boundary,settings,clock())
    assert store.execute('SELECT COUNT(*) FROM settings WHERE name LIKE ?',('listing-contact-enrichment:rent:%',)).fetchone()[0]==1
    assert store.execute('SELECT payload FROM settings WHERE name=?',(key(p),)).fetchone()
    assert cached_estimate(store,p,clock())['observed_at']==observed
    assert json.loads(store.execute('SELECT payload FROM settings WHERE name=?',('penten_review:'+p['id'],)).fetchone()[0])==decision
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    clock.advance(days=31)
    assert cached_estimate(store,p,clock()) is None
