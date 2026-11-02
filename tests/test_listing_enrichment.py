import json
from rentalsourcing.listing_enrichment import evidence,run
from rentalsourcing.collector import LIVE
from conftest import listing

def test_agent_is_only_explicit_listing_agent_not_office_or_advertiser():
    p=listing('fixture',listingAgent={'name':'Listing','phone':'4125550100'},listingOffice={'name':'Office','email':'office@example.test'},buyersAgent={'name':'Buyer'})
    e=evidence(p,'2026-11-02T06:00:00Z');assert e['agents'][0]['name']=='Listing' and e['agents'][0]['email'] is None
    assert e['agents'][0]['brokerage']=='Office'
    assert evidence(listing('fixture',agent={'name':'Advertiser'}),'2026-11-02T06:00:00Z')['agents']==[]

def test_resumable_existing_enrichment_uses_no_provider_requests_or_emails(store,clock):
    p=listing('fixture');store.save_property('fixture',p,'Pending Verification',[],LIVE,1,clock().isoformat())
    class R:
        status=200
        def read(self,n):return json.dumps({'accepted':True,'next_cursor':None,'summary':{'processed':1}}).encode()
        def __enter__(self):return self
        def __exit__(self,*a):pass
    class O:
        calls=0
        def open(self,req,timeout):self.calls+=1;assert req.full_url.endswith('/review-listing-enrichment');return R()
    o=O();config=('https://example.test/api/rentalsourcing/review-sync','s'*32,'service-token')
    assert run(store,config,opener=o)['done'];assert run(store,config,opener=o)['done'];assert o.calls==1
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0
