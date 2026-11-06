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
    assert r['cards'][0]['tax_status']=='provider_verified'
    assert r['cards'][0]['annual_tax']==1500
    assert 'tax_verification_required' not in r['cards'][0]['reasons']


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


def test_listing_alert_cannot_erase_known_turnkey_or_ownership_exclusions(store,boundary,clock):
    for changes in [{'description':'Fully turnkey investment package'},{'propertyType':'Single Family','ownershipType':'Condominium'},{'propertyType':'Single Family','propertySubType':'Townhome'}]:
        p=listing('known',addressLine1='100 Fixture Ave',**changes)
        store.save_property('known',p,'Rejected',[],LIVE,1,clock().isoformat())
        item=alert();item['property'].pop('propertyType',None)
        result=evaluate(store,boundary,[item],clock())
        assert result['cards']==[] and result['counts']['rejected']==1


def test_read_only_audit_keeps_every_disposition_and_original_identity(store,boundary,clock):
    from rentalsourcing.listing_intake import audit
    records=[alert(),alert(bedrooms=5),alert(latitude=None,longitude=None)]
    for i,r in enumerate(records):
        r['external_id']='fixture-email|'+str(i);r['property']['addressLine1']=str(100+i)+' Fixture Ave'
    evaluate(store,boundary,records,clock())
    result=audit(store,boundary,clock())
    assert result['summary']['unique_properties']==3
    assert {r['disposition'] for r in result['properties']}=={'pending_delivery','rejected_hard_criterion','pending_additional_evidence'}
    assert all(r['external_id'].startswith('fixture-email|') for r in result['properties'])
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0


def test_geocoder_uses_postal_city_and_replaces_only_legacy_negative_cache(store,boundary,clock):
    import hashlib,io,json
    from rentalsourcing.listing_intake import geocode
    from rentalsourcing.offmarket import street_key
    p=listing('unseen',addressLine1='100 Fixture',formattedAddress='100 Fixture, Morningside, PA 15212')
    key='listing_geocode:'+hashlib.sha256((street_key(p)+'|'+p['zipCode']).encode()).hexdigest()
    store.save_setting(key,{'matched':False,'next_at':(clock()+dt.timedelta(days=7)).isoformat()})
    class Response(io.BytesIO):
        def __enter__(self):return self
        def __exit__(self,*a):pass
    class Opener:
        calls=0
        def open(self,request,**kwargs):
            self.calls+=1
            assert 'Pittsburgh' in request.full_url and 'Morningside' not in request.full_url
            return Response(json.dumps({'result':{'addressMatches':[{'matchedAddress':'100 Fixture St, Pittsburgh, PA 15212','addressComponents':{'state':'PA','zip':'15212'},'coordinates':{'x':-80,'y':40.45}}]}}).encode())
    o=Opener();assert geocode(store,p,boundary,clock(),o)['matched']
    assert geocode(store,p,boundary,clock(),o)['matched'] and o.calls==1


def test_official_point_in_excluded_area_is_rejected_not_silently_unmatched(store,boundary,clock):
    import io,json
    from rentalsourcing.listing_intake import geocode
    p=listing('excluded',addressLine1='102 Other St',formattedAddress='102 Other St, Pittsburgh, PA 15212')
    class Response(io.BytesIO):
        def __enter__(self):return self
        def __exit__(self,*a):pass
    class Opener:
        def open(self,*a,**k):return Response(json.dumps({'result':{'addressMatches':[{'matchedAddress':p['formattedAddress'],'addressComponents':{'state':'PA','zip':'15212'},'coordinates':{'x':-80.27,'y':40.32}}]}}).encode())
    result=geocode(store,p,boundary,clock(),Opener());assert result['matched']
    p.update(latitude=result['latitude'],longitude=result['longitude'])
    from rentalsourcing.core import geographic_verification
    assert geographic_verification(p,boundary)==('Rejected','sheraden_neighborhood')


def test_all_nine_captured_unknown_properties_have_an_explicit_disposition(store,clock):
    import json
    from pathlib import Path
    from rentalsourcing.listing_intake import audit
    from rentalsourcing.geography import load_boundary
    clock.value=dt.datetime(2026,11,6,11,tzinfo=dt.timezone.utc)
    actual_boundary=load_boundary('data/pittsburgh.geojson')
    rows=json.loads(Path('tests/fixtures/realscout-nine-20261106.json').read_text())['records']
    records=[]
    class NoMatch:
        def open(self,*a,**k):raise OSError('Isolated test: official source unavailable')
    for row in rows:
        p={k:row[k] for k in ['bedrooms','bathrooms','squareFootage','price']}
        p.update(addressLine1=row['street'],formattedAddress=row['address'],city='Pittsburgh',state='PA',zipCode=row['zip'])
        if row['street']=='1111 Duffield St':p.update(latitude=40.47880551376,longitude=-79.932045106514)
        records.append({'source_id':'realscout-email','external_id':row['source_email_id'],'received_at':row['observed_at'],'listing_status':row['status'],'property':p})
    for item in records:evaluate(store,actual_boundary,[item],clock(),NoMatch())
    checked=audit(store,actual_boundary,clock())['properties']
    assert len(checked)==9
    counts={d:sum(r['disposition']==d for r in checked) for d in ['pending_delivery','rejected_hard_criterion','pending_additional_evidence']}
    assert counts=={'pending_delivery':1,'rejected_hard_criterion':5,'pending_additional_evidence':3}
    for r in checked:
        if r['disposition']=='rejected_hard_criterion':assert 'bedrooms' in r['reasons'] or 'excluded_zip' in r['reasons']
        assert r['tax_status']=='unknown'
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
