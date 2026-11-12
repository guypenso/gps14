import datetime as dt,json
from dataclasses import replace
from conftest import listing,taxes
from rentalsourcing.collector import LIVE
from rentalsourcing.listing_enrichment import stored_evidence,research_context
from rentalsourcing.rental_intelligence import key

def test_stored_rent_tax_and_listing_agent_travel_without_provider_calls(store,clock):
 p=listing('new-cache',formattedAddress='120 Fixture St, Pittsburgh, PA 15212',addressLine1='120 Fixture St')
 at=clock().isoformat();end=(clock()+dt.timedelta(days=1)).isoformat()
 record=taxes(p['id']);record['listingAgent']={'name':'Fixture Agent','email':'fixture@example.test','phone':'4125550100'}
 store.save_tax(p['id'],record,LIVE,at,end)
 rent={'subjectProperty':p,'rent':1800,'rentRangeLow':1600,'rentRangeHigh':2000,'comparables':[{'formattedAddress':str(i)+' Fixture Comp St','price':1800} for i in range(3)]}
 store.save_setting(key(p),{'source':'RentCast','raw':rent,'observed_at':at})
 result=stored_evidence(store,p,at,clock())
 assert result['rent_estimate']['monthly']==1800 and result['provider_tax']['state']=='provider_verified'
 assert result['agents'][0]['name']=='Fixture Agent'
 assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
 assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0

def test_wrong_or_demo_property_cache_never_becomes_live_tax_evidence(store,clock):
 p=listing('new-cache');at=clock().isoformat();end=(clock()+dt.timedelta(days=1)).isoformat()
 for record in [taxes('wrong'),dict(taxes(p['id']),demo=True)]:
  store.save_tax(p['id'],record,LIVE,at,end)
  assert 'provider_tax' not in stored_evidence(store,p,at,clock())

def test_research_budget_context_is_not_spending_authorization(store,clock,settings,monkeypatch):
 monkeypatch.setattr('rentalsourcing.config.Settings.from_env',lambda:replace(settings,enabled=True,approved_requests=27))
 monkeypatch.setenv('ENABLE_RENT_ESTIMATES','false')
 v=research_context(store);assert not v['rent_authorized'] and v['approval_cap']==27
 monkeypatch.setattr('rentalsourcing.config.Settings.from_env',lambda:replace(settings,enabled=False))
 assert research_context(store)==v
 assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
