import datetime as dt
from conftest import listing,taxes
from rentalsourcing.collector import LIVE
from rentalsourcing.listing_intake import evaluate


def alert(**changes):
    p=listing('source',addressLine1='100 Fixture Ave');p.update(changes)
    return dict(source_id='realscout-email',external_id='fixture-mail-1',received_at='2026-10-16T08:00:00+00:00',listing_status='Active',evidence_url='https://mail.google.com/',property=p)


def test_listing_channel_reuses_exact_identity_and_cache_without_paid_calls(store,boundary,clock):
    p=listing('known',addressLine1='100 Fixture Ave')
    store.save_property('known',p,'Pending Verification',[],LIVE,1,clock().isoformat())
    store.save_tax('known',taxes('known',1500),LIVE,clock().isoformat(),(clock()+dt.timedelta(days=7)).isoformat())
    r=evaluate(store,boundary,[alert()],clock())
    assert r['counts']['known_rentcast']==1 and r['counts']['qualified']==1
    assert r['cards'][0]['source']=='RealScout' and r['cards'][0]['source_id']=='known'
    store.save_setting('penten_review:known',{'status':'rejected'})
    assert evaluate(store,boundary,[alert()],clock())['cards']==[]
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0


def test_unseen_property_pending_and_bad_bedrooms_rejected(store,boundary,clock):
    r=evaluate(store,boundary,[alert()],clock())
    assert r['counts']['not_known_rentcast']==1 and r['cards'][0]['qualification']=='pending_verification'
    assert r['cards'][0]['annual_tax'] is None
    assert evaluate(store,boundary,[alert(bedrooms=5)],clock())['counts']['rejected']==1


def test_unknown_geography_is_research_not_approved(store,boundary,clock):
    item=alert(latitude=None,longitude=None)
    r=evaluate(store,boundary,[item],clock())
    assert not r['cards'] and r['counts']['needs_research']==1
    assert 'coordinates' in r['evaluations'][0]['reasons']


def test_stale_alert_does_not_become_qualified_with_cached_tax(store,boundary,clock):
    p=listing('known',addressLine1='100 Fixture Ave');store.save_property('known',p,'Qualified',[],LIVE,1,clock().isoformat())
    store.save_tax('known',taxes('known',1500),LIVE,clock().isoformat(),(clock()+dt.timedelta(days=7)).isoformat())
    item=alert();item['received_at']=(clock()-dt.timedelta(days=2)).isoformat()
    r=evaluate(store,boundary,[item],clock())
    assert r['cards'][0]['qualification']=='pending_verification'


def test_official_geocoder_exact_match_and_permanent_denial_cache(store,boundary,clock):
    import io,json,urllib.error
    from rentalsourcing.listing_intake import geocode
    p=listing('new',addressLine1='100 Fixture Ave',formattedAddress='100 Fixture Ave, Pittsburgh, PA 15212')
    class Response(io.BytesIO):
        def __enter__(self):return self
        def __exit__(self,*a):pass
    class Opener:
        calls=0
        def open(self,*a,**k):
            self.calls+=1
            return Response(json.dumps({'result':{'addressMatches':[{'matchedAddress':p['formattedAddress'],'addressComponents':{'state':'PA','zip':p['zipCode']},'coordinates':{'x':p['longitude'],'y':p['latitude']}}]}}).encode())
    o=Opener();first=geocode(store,p,boundary,clock(),o);assert first['matched']
    assert geocode(store,p,boundary,clock(),o)['matched'] and o.calls==1
    class Denied:
        calls=0
        def open(self,*a,**k):self.calls+=1;raise urllib.error.HTTPError('https://official.test',403,'denied',{},None)
    denied=Denied()
    geocode(store,listing('second',addressLine1='102 Other St'),boundary,clock(),denied)
    geocode(store,listing('third',addressLine1='104 Other St'),boundary,clock(),denied)
    assert denied.calls==1
