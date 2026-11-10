import datetime as dt,json
from conftest import listing,taxes
from rentalsourcing.collector import LIVE
from rentalsourcing.core import tax_verification,verification
from rentalsourcing.geography import load_boundary
from rentalsourcing.rental_intelligence import normalize,cached_estimate,key,job_key,enrich

def avm(p,rent=2400):
    return {'rent':rent,'rentRangeLow':2200,'rentRangeHigh':2600,'subjectProperty':p,
            'comparables':[{'formattedAddress':f'{i} Fictional Comp St','price':2400,'propertyType':'Single Family'} for i in range(3)]}

def test_exact_property_and_current_evidence_required_for_tax_exception(store,clock):
    p=listing('rent-fixture',addressLine1='100 Fixture Ave');raw=avm(p)
    store.save_property(p['id'],p,'Pending Verification',[],LIVE,1,clock().isoformat())
    store.save_tax(p['id'],taxes(p['id'],3000),LIVE,clock().isoformat(),(clock()+dt.timedelta(days=60)).isoformat())
    for monthly,expected in [(2300,'Rejected'),(2300.01,'Qualified')]:
        store.save_setting(key(p),{'source':'RentCast','raw':avm(p,monthly),'observed_at':clock().isoformat()})
        record=store.get_tax(p['id'],clock().isoformat())[0]
        state,tax,_=tax_verification(record,clock().date());assert state==expected
        if state=='Qualified':assert tax['eligibility_path']=='high_rent_exception'
    assert tax_verification({'id':p['id'],'_rentcast_rent':cached_estimate(store,p,clock())},clock().date())[0]=='Pending Verification'
    assert normalize(raw,{**p,'addressLine1':'102 Other Ave'},clock().isoformat(),clock()) is None
    assert normalize(raw,p,(clock()-dt.timedelta(days=31)).isoformat(),clock()) is None
    assert normalize({**raw,'comparables':[]},p,clock().isoformat(),clock()) is None
    clock.advance(days=31)
    assert tax_verification(store.get_tax(p['id'],clock().isoformat())[0],clock().date())[0]=='Rejected'

def test_only_two_new_municipalities_and_neighbors_stay_excluded(store,clock):
    b=load_boundary('data/pittsburgh.geojson')
    for name,region in b.additional_municipalities.items():
        point=region.geometry.representative_point();p=listing('new-muni',latitude=point.y,longitude=point.x,zipCode='15102' if name.startswith('Bethel') else '15236')
        assert verification(p,b)[0]=='Candidate'
        assert b.municipality(p)==name
        assert verification({**p,'bedrooms':5},b)[0]=='Rejected'
        edge=list(region.geometry.exterior.coords)[0]
        assert b.evaluate({**p,'longitude':edge[0],'latitude':edge[1]})[0]=='Pending Verification'
    for x,y in [(-80.045,40.38),(-79.90,40.36),(-80.05,40.20),(-80.3,40.7),(-79.6,40.3)]:
        assert b.evaluate({'longitude':x,'latitude':y,'zipCode':'15102'})[0]=='Rejected'

def test_cached_rent_reused_without_authorizing_provider_calls(store,boundary,clock,settings):
    p=listing('rent-reuse',addressLine1='100 Fixture Ave')
    store.save_property(p['id'],p,'Pending Verification',[],LIVE,1,clock().isoformat())
    store.save_setting(key(p),{'source':'RentCast','raw':avm(p),'observed_at':clock().isoformat()})
    result=enrich(store,boundary,settings,clock())
    assert result['cached']==1 and result['paid_calls']==0
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0

def test_realscout_only_candidate_reuses_rent_without_rentcast_listing(store,boundary,clock,settings):
    p=listing('realscout:fixture',addressLine1='200 Fixture Ave')
    store.save_setting('listing_identity:'+p['id'],{'source_id':p['id'],'property':p,'observed_at':clock().isoformat()})
    store.save_setting(key(p),{'source':'RentCast','raw':avm(p),'observed_at':clock().isoformat()})
    result=enrich(store,boundary,settings,clock())
    assert result['cached']==1 and result['paid_calls']==0

def test_new_source_queries_are_only_the_two_approved_municipalities():
    from rentalsourcing.request import municipal_queries
    queries=municipal_queries()
    assert [(slug,q['city'],q['zipCode']) for slug,q in queries]==[('pleasant-hills','Pleasant Hills','15236'),('bethel-park','Bethel Park','15102')]
    assert all(q['state']=='PA' and q['bedrooms']=='3:4' and q['price']=='*:200001' for slug,q in queries)

def test_authorized_avm_acquisition_accounts_once_and_reuses_cache(store,boundary,clock,settings,monkeypatch):
    from dataclasses import replace
    from conftest import Response
    from rentalsourcing.collector import Client
    from rentalsourcing.rental_intelligence import parameters
    p=listing('avm-authorized-fixture',addressLine1='100 Fixture Ave')
    store.save_property(p['id'],p,'Pending Verification',[],LIVE,1,clock().isoformat())
    settings=replace(settings,approval_mode='monthly_budget',billing_anchor='2026-10-01T00:00:00Z',billing_baseline=0,monthly_budget_requests=1000,provider_included_requests=1000)
    class AuthorizedFixtureProvider:
        count=0
        def open(self,request,timeout=None):
            assert '/v1/avm/rent/long-term?' in request.full_url
            self.count+=1
            return Response(avm(p))
    provider=AuthorizedFixtureProvider();client=Client(store,settings,clock,provider,lambda _:None)
    monkeypatch.setenv('ENABLE_RENT_ESTIMATES','true')
    first=enrich(store,boundary,settings,clock(),client)
    assert first['retrieved']==1 and first['paid_calls']==1 and provider.count==1
    second=enrich(store,boundary,settings,clock(),client)
    assert second['cached']==1 and second['paid_calls']==0 and provider.count==1
    # Reuse the exact persisted provider response even before enrichment-normalization.
    store.execute('DELETE FROM settings WHERE name=?',(key(p),))
    assert cached_estimate(store,p,clock())['monthly']==2400
    monkeypatch.delenv('ENABLE_RENT_ESTIMATES')
    assert client.get('/avm/rent/long-term',parameters(p))[0]['rent']==2400
    assert provider.count==1

def test_unapproved_avm_calls_never_reach_provider(store,clock,settings,provider):
    import pytest
    from rentalsourcing.collector import Client
    client=Client(store,settings,clock,provider,lambda _:None)
    with pytest.raises(PermissionError,match='activated monthly budget'):
        client.get('/avm/rent/long-term',{'address':'FICTIONAL TEST ADDRESS'})
    assert not provider.calls and client.count==0

def test_new_candidate_persists_budget_block_and_resumes_only_after_approved_activation(store,boundary,clock,settings,monkeypatch):
    from dataclasses import replace
    from conftest import Response
    from rentalsourcing.collector import Client
    p=listing('avm-budget-fixture',addressLine1='100 Fixture Ave')
    store.save_property(p['id'],p,'Pending Verification',[],LIVE,1,clock().isoformat())
    blocked=enrich(store,boundary,settings,clock())
    row=store.execute('SELECT payload FROM settings WHERE name=?',(job_key(p),)).fetchone()
    assert blocked['blocked_by_budget']==1 and json.loads(row[0])['state']=='Budget Blocked'
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    clock.advance(days=2)
    approved=replace(settings,approval_mode='monthly_budget',billing_anchor='2026-10-01T00:00:00Z',billing_baseline=0,monthly_budget_requests=1000,provider_included_requests=1000)
    class Provider:
        calls=0
        def open(self,request,timeout=None):
            self.calls+=1
            return Response(avm(p))
    provider=Provider();client=Client(store,approved,clock,provider,lambda _:None)
    monkeypatch.setenv('ENABLE_RENT_ESTIMATES','true')
    result=enrich(store,boundary,approved,clock(),client)
    assert result['retrieved']==1 and provider.calls==1
    assert json.loads(store.execute('SELECT payload FROM settings WHERE name=?',(job_key(p),)).fetchone()[0])['state']=='Completed'
