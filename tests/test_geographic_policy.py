"""Only offline fixtures and bundled authoritative public polygons; no provider calls."""
import json,math,datetime as dt
from pathlib import Path
from dataclasses import replace
from urllib.parse import parse_qs,urlsplit
import pytest
from rentalsourcing.core import verification,report_eligible,tax_verification,EXCLUDED_ZIPS
from rentalsourcing.geography import load_boundary,EligibilityBoundary,load_region
from rentalsourcing.request import listing_parameters
from rentalsourcing.criteria import reevaluate
from rentalsourcing.cloud import daily_discovery
from rentalsourcing.collector import Client,LIVE
from rentalsourcing.engine import Engine
from rentalsourcing.digest import Digest
from conftest import listing,Provider,taxes
ROOT=Path(__file__).resolve().parents[1]
@pytest.fixture
def policy():return load_boundary(ROOT/'data/pittsburgh.geojson')
def point(region):
 p=region.geometry.representative_point();return dict(longitude=p.x,latitude=p.y)
def at(region,**kw):return listing(**dict(point(region),**kw))

def test_official_brighton_and_bellevue_and_postal_labels(policy):
 assert verification(at(policy.brighton),policy)==('Candidate',[])
 for zipcode in ['15202','15212','15299']:
  row=at(policy.bellevue,zipCode=zipcode,city='Postal Pittsburgh')
  assert not policy.city.contains(row)
  assert verification(row,policy)==('Candidate',[])
 assert verification(listing(longitude=-79.9961,latitude=40.4406,zipCode='15212'),policy)[0]=='Rejected'
 assert 'outside_approved_neighborhoods_15212' in verification(listing(longitude=-79.9961,latitude=40.4406),policy)[1]
 assert verification(listing(longitude=-79.9961,latitude=40.4406,zipCode='15222'),policy)==('Candidate',[])

def test_postal_15202_never_substitutes_for_municipality(policy):
 # Public-map reference points in neighboring municipalities; no property/address data.
 for lon,lat in [(-80.0676,40.5009),(-80.0831,40.508),(-80.0953,40.510),(-80.0395,40.4965)]:
  row=listing(longitude=lon,latitude=lat,zipCode='15202',city='Bellevue')
  assert not policy.bellevue.contains(row)
  if not policy.city.contains(row):assert verification(row,policy)[0]=='Rejected'
 for field,value in [('longitude',None),('latitude',None)]:
  row=at(policy.bellevue,zipCode='15202',**{field:value});assert verification(row,policy)[0]=='Pending Verification';assert not report_eligible(row,policy)

@pytest.mark.parametrize('kind',['Townhouse','Townhome','Town House','Town-Home','Row House','Single Family Attached','Single Family - Townhouse','Terraced House'])
def test_townhouse_exclusion_everywhere(policy,kind):
 for region,zip in [(policy.spring_hill,'15212'),(policy.brighton,'15212'),(policy.bellevue,'15202'),(policy.city,'15222')]:
  row=at(region,zipCode=zip,propertyType=kind)
  assert verification(row,policy)[0]=='Rejected'
  assert not report_eligible(row,policy)

def test_subtype_and_ambiguity_do_not_confuse_architectural_style(policy):
 row=at(policy.bellevue,zipCode='15202',propertyType='Single Family',architecturalStyle='Townhouse',style='Townhome')
 assert verification(row,policy)==('Candidate',[])
 assert verification(dict(row,propertySubType='Townhouse'),policy)[0]=='Rejected'
 for change in [dict(propertyType=None),dict(propertyType='Residential'),dict(isAttached=True),dict(structureType='Attached')]:
  candidate=dict(row,**change);assert verification(candidate,policy)[0]=='Pending Verification'
  assert report_eligible(candidate,policy)

def test_hard_limits_and_excluded_zips_unchanged(policy):
 row=at(policy.bellevue,zipCode='15202');assert verification(row,policy)[0]=='Candidate'
 for zip in EXCLUDED_ZIPS:assert verification(dict(row,zipCode=zip),policy)[0]=='Rejected'
 for field,value in [('price',200001),('bedrooms',2),('bathrooms',0),('squareFootage',999)]:assert verification(dict(row,**{field:value}),policy)[0]=='Rejected'
 assert tax_verification(taxes(amount=1899.99),dt.date(2026,11,1))[0]=='Qualified'
 assert tax_verification(taxes(amount=1900),dt.date(2026,11,1))[0]=='Rejected'

def test_discovery_circle_covers_both_entire_municipalities_without_extra_request(policy):
 q=listing_parameters(policy);assert 'city' not in q and 'zipCode' not in q
 assert q['radius']<10
 def distance(x,y):
  a,b=math.radians(q['latitude']),math.radians(y);return 3958.7613*2*math.asin(math.sqrt(math.sin((b-a)/2)**2+math.cos(a)*math.cos(b)*math.sin(math.radians(x-q['longitude'])/2)**2))
 for region in [policy.city,policy.bellevue]:
  polys=list(region.geometry.geoms) if region.geometry.geom_type=='MultiPolygon' else [region.geometry]
  assert all(distance(x,y)<=q['radius'] for poly in polys for x,y in poly.exterior.coords)
 assert q['price']=='*:200000' and q['bedrooms']=='3:4' and q['bathrooms']=='1:*' and q['squareFootage']=='1000:*'

def test_region_checksum_and_missing_polygon_fail_closed(tmp_path):
 src=ROOT/'data/bellevue.geojson';target=tmp_path/src.name;target.write_bytes(src.read_bytes()+b' ');target.with_suffix('.source.json').write_bytes(src.with_suffix('.source.json').read_bytes())
 with pytest.raises(ValueError,match='checksum'):load_region(tmp_path,'bellevue')
 with pytest.raises(FileNotFoundError):load_region(tmp_path,'brighton-heights')

def test_pipeline_email_and_sync_same_selection_with_no_extra_discovery(store,settings,clock,policy):
 rows=[at(policy.spring_hill,id='spring-hill',formattedAddress='FICTIONAL SPRING HILL FIXTURE'),at(policy.brighton,id='brighton',formattedAddress='FICTIONAL BRIGHTON FIXTURE'),at(policy.bellevue,id='bellevue',zipCode='15202',formattedAddress='FICTIONAL BELLEVUE FIXTURE'),at(policy.bellevue,id='tax-pending',zipCode='15202',formattedAddress='FICTIONAL PENDING FIXTURE'),at(policy.bellevue,id='townhouse',zipCode='15202',propertyType='Townhouse'),listing('other-15212',longitude=-79.9961,latitude=40.4406),listing('not-bellevue',longitude=-80.0953,latitude=40.510,zipCode='15202'),at(policy.bellevue,id='type-pending',zipCode='15202',propertyType=None),at(policy.bellevue,id='geo-pending',zipCode='15202',longitude=None)]
 provider=Provider(rows);provider.records.pop('tax-pending');s=replace(settings,page_size=100,per_run=4)
 result=daily_discovery(store,policy,s,clock().date().isoformat(),clock=clock,opener=provider)
 assert result['status']=='sent'
 listings=[r for r in provider.calls if '/listings/sale?' in r.full_url];assert len(listings)==1
 params=parse_qs(urlsplit(listings[0].full_url).query);assert 'city' not in params and 'latitude' in params
 report=json.loads(store.execute('SELECT payload FROM command_center_reports WHERE outbox_id=?',(result['outbox_id'],)).fetchone()[0]);ids={p['source_id'] for p in report['opportunities']}
 assert ids=={'spring-hill','brighton','bellevue','tax-pending','type-pending'}
 assert {p['source_id'] for p in report['opportunities'] if p['qualification']=='verified'}=={'spring-hill','brighton','bellevue'}
 assert len(provider.calls)-len(provider.messages)<=4
 text=provider.messages[0]['text'];assert 'FICTIONAL BELLEVUE FIXTURE' in text and 'FICTIONAL BRIGHTON FIXTURE' in text
 for id in ['townhouse','other-15212','not-bellevue']:assert store.get_property(id)['state']=='Rejected'


def test_stored_reevaluation_and_unsent_pending_snapshot_preserve_history(store,settings,clock,policy):
 row=at(policy.bellevue,id='stored-townhouse',zipCode='15202')
 provider=Provider([row]);provider.records.pop(row['id']);client=Client(store,settings,clock,provider,lambda _:None)
 Engine(store,policy,client,settings,clock).run('test-job',clock().date().isoformat())
 d=Digest(store,policy,settings,clock,provider);outbox=d.prepare('test-job',clock().date().isoformat(),{'discovery_state':'fresh'})
 p=store.get_property(row['id']);store.save_property(row['id'],dict(row,propertyType='Townhouse'),p['state'],p['reasons'],p['source'],p['revision'],p['observed_at'])
 n=store.execute('SELECT COUNT(*) FROM price_history').fetchone()[0];reevaluate(store,policy,clock())
 assert store.get_property(row['id'])['state']=='Rejected' and store.execute('SELECT COUNT(*) FROM price_history').fetchone()[0]==n
 assert d.send(outbox)=='cancelled_stale' and not provider.messages

def test_price_reductions_use_current_geography_and_type_and_keep_old_records(store,settings,clock,policy):
 rows=[at(policy.bellevue,id='reduced-bellevue',zipCode='15202',price=180000,formattedAddress='FICTIONAL REDUCTION FIXTURE'),at(policy.brighton,id='became-townhouse',price=180000)]
 provider=Provider(rows);client=Client(store,settings,clock,provider,lambda _:None);e=Engine(store,policy,client,settings,clock)
 e.run('before',clock().date().isoformat());old=store.get_property('became-townhouse')
 clock.advance(minutes=20)
 for row in [dict(rows[0],price=170000),dict(rows[1],price=170000,propertyType='Townhome')]:e.ingest('reduced',row)
 # Cached annual bills remain usable without any additional detail requests.
 d=Digest(store,policy,settings,clock,provider);report_id=d.prepare('reduced',clock().date().isoformat(),{'discovery_state':'fresh'})
 report=json.loads(store.execute('SELECT payload FROM command_center_reports WHERE outbox_id=?',(report_id,)).fetchone()[0])
 assert len(report['opportunities'])==1
 assert report['opportunities'][0]['source_id']=='reduced-bellevue' and report['opportunities'][0]['section']=='price_reduction'
 assert store.get_property('became-townhouse')['state']=='Rejected'
 assert store.execute('SELECT COUNT(*) FROM price_history WHERE property_id=?',('became-townhouse',)).fetchone()[0]==2
 assert store.get_tax('became-townhouse',clock().isoformat()) is not None
 assert old['payload']['price']==180000

def test_property_record_type_evidence_cannot_approve_a_townhouse():
 record=taxes(amount=1500)
 assert tax_verification(dict(record,propertyType='Townhouse'))[0]=='Rejected'
 assert tax_verification(dict(record,propertyType='Single Family',propertySubType='Town House'))[0]=='Rejected'
 assert tax_verification(dict(record,propertyType='Unknown'))[0]=='Pending Verification'
 assert tax_verification(dict(taxes(amount=1900),propertyType='Unknown'))[0]=='Rejected'
 assert tax_verification(dict(record,propertyType='Single Family',architecturalStyle='Townhouse'))[0]=='Qualified'

def test_unverified_geography_cannot_crowd_verified_bellevue_out_of_pending_report(store,settings,clock,policy):
 for i in range(50):
  row=at(policy.bellevue,id=f'a-unknown-{i:02}',zipCode='15202',longitude=None)
  store.save_property(row['id'],row,'Pending Verification',['coordinates'],LIVE,1,clock().isoformat())
 row=at(policy.bellevue,id='zz-eligible',zipCode='15202',formattedAddress='FICTIONAL VERIFIED GEOGRAPHY')
 store.save_property(row['id'],row,'Pending Verification',['missing_tax_history'],LIVE,1,clock().isoformat())
 d=Digest(store,policy,settings,clock,Provider([]));identity=d.prepare('pending-only',clock().date().isoformat(),{'discovery_state':'fresh'})
 report=json.loads(store.execute('SELECT payload FROM command_center_reports WHERE outbox_id=?',(identity,)).fetchone()[0])
 assert [p['source_id'] for p in report['opportunities']]==['zz-eligible']
