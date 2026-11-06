"""Offline polygon and pipeline regression tests; provider calls are mocked."""
import datetime as dt
import json
from pathlib import Path
from dataclasses import replace
import pytest
from rentalsourcing.core import verification,report_eligible
from rentalsourcing.geography import load_boundary,load_region
from rentalsourcing.criteria import reevaluate
from rentalsourcing.cloud import daily_discovery
from rentalsourcing.collector import LIVE,Client
from rentalsourcing.engine import Engine
from rentalsourcing.digest import Digest
from conftest import listing,Provider
ROOT=Path(__file__).resolve().parents[1]
@pytest.fixture
def policy():return load_boundary(ROOT/'data/pittsburgh.geojson')
def sheraden_row(policy,**changes):
 point=policy.sheraden.geometry.representative_point()
 return listing(**dict(dict(longitude=point.x,latitude=point.y,zipCode='15205'),**changes))
def other_row(**changes):return listing(**dict(dict(longitude=-80.05,latitude=40.43,zipCode='15205'),**changes))
@pytest.mark.parametrize('zip',['15205','15204','15202','15222','15299'])
def test_physical_sheraden_excluded_regardless_of_postal_label(policy,zip):
 row=sheraden_row(policy);row.update(zipCode=zip,neighborhood='Not Sheraden',city='Bellevue')
 assert policy.city.contains(row)
 state,reasons=verification(row,policy)
 assert state=='Rejected' and 'sheraden_neighborhood' in reasons and not report_eligible(row,policy)

def test_other_15205_remains_eligible_and_reported_neighborhood_does_not_override_coordinates(policy):
 row=other_row(neighborhood='Sheraden')
 assert policy.city.contains(row) and not policy.sheraden.contains(row)
 assert verification(row,policy)==('Candidate',[]) and report_eligible(row,policy)

def test_unknown_geography_not_reportable_and_sheraden_edge_not_approved(policy):
 for field in ['latitude','longitude']:
  row=other_row(**{field:None});assert verification(row,policy)[0]=='Pending Verification';assert not report_eligible(row,policy)
 geometry=policy.sheraden.geometry;polygon=geometry if geometry.geom_type=='Polygon' else geometry.geoms[0]
 x,y=list(polygon.exterior.coords)[0];row=other_row(longitude=x,latitude=y)
 assert verification(row,policy)[0]=='Rejected' and not report_eligible(row,policy)

def test_sheraden_source_integrity_and_absence_fail_closed(tmp_path):
 source=ROOT/'data/sheraden.geojson';target=tmp_path/source.name
 target.write_bytes(source.read_bytes()+b' ');target.with_suffix('.source.json').write_bytes(source.with_suffix('.source.json').read_bytes())
 with pytest.raises(ValueError,match='checksum'):load_region(tmp_path,'sheraden')
 target.unlink()
 with pytest.raises(FileNotFoundError):load_region(tmp_path,'sheraden')

def test_daily_email_and_sync_exclude_sheraden_but_keep_verified_and_pending_15205(store,settings,clock,policy):
 rows=[sheraden_row(policy,id='sheraden-15205',formattedAddress='FICTIONAL SHERADEN 15205'),sheraden_row(policy,id='sheraden-other',formattedAddress='FICTIONAL SHERADEN OTHER'),other_row(id='eligible-15205',formattedAddress='FICTIONAL ELIGIBLE 15205'),other_row(id='pending-15205',formattedAddress='FICTIONAL PENDING 15205'),other_row(id='unlocated',longitude=None,formattedAddress='FICTIONAL UNKNOWN GEOGRAPHY')]
 rows[1]['zipCode']='15222';provider=Provider(rows);provider.records.pop('pending-15205')
 settings=replace(settings,page_size=100,per_run=4)
 result=daily_discovery(store,policy,settings,clock().date().isoformat(),clock=clock,opener=provider)
 assert result['status']=='sent' and provider.tax_calls==2
 report=json.loads(store.execute('SELECT payload FROM command_center_reports WHERE outbox_id=?',(result['outbox_id'],)).fetchone()[0])
 assert {p['source_id'] for p in report['opportunities']}=={'eligible-15205','pending-15205'}
 assert {p['source_id'] for p in report['opportunities'] if p['qualification']=='verified'}=={'eligible-15205'}
 for section in ['text','html']:
  assert 'FICTIONAL ELIGIBLE 15205' in provider.messages[0][section]
  assert 'FICTIONAL PENDING 15205' in provider.messages[0][section]
  assert 'FICTIONAL SHERADEN' not in provider.messages[0][section] and 'FICTIONAL UNKNOWN GEOGRAPHY' not in provider.messages[0][section]
 assert sum('/listings/sale?' in r.full_url for r in provider.calls)==1

class PreviousPolicy:
 def __init__(self,policy):self.city=policy.city
 def evaluate(self,row):return ('Candidate',None) if self.city.contains(row) else ('Rejected','outside_city_or_boundary')
 def contains(self,row):return self.evaluate(row)[0]=='Candidate'

def test_reassessment_cancels_unsent_sheraden_without_deleting_history_or_sending(store,settings,clock,policy):
 row=sheraden_row(policy,id='saved-sheraden',formattedAddress='FICTIONAL SAVED SHERADEN');provider=Provider([row]);old=PreviousPolicy(policy)
 client=Client(store,settings,clock,provider,lambda _:None);Engine(store,old,client,settings,clock).run('old-selection',clock().date().isoformat())
 outbox=Digest(store,old,settings,clock,provider).prepare('old-selection',clock().date().isoformat(),{'discovery_state':'fresh'})
 history=store.execute('SELECT COUNT(*) FROM price_history').fetchone()[0]
 reevaluate(store,policy,clock())
 assert store.get_property(row['id'])['state']=='Rejected'
 assert 'sheraden_neighborhood' in store.get_property(row['id'])['reasons']
 assert store.execute('SELECT COUNT(*) FROM price_history').fetchone()[0]==history
 assert store.get_tax(row['id'],clock().isoformat()) is not None
 assert Digest(store,policy,settings,clock,provider).send(outbox)=='cancelled_stale'
 assert provider.messages==[]

def test_price_reduction_does_not_reintroduce_sheraden(store,settings,clock,policy):
 old=PreviousPolicy(policy);rows=[sheraden_row(policy,id='sheraden-reduction'),other_row(id='allowed-reduction')]
 provider=Provider(rows);client=Client(store,settings,clock,provider,lambda _:None);Engine(store,old,client,settings,clock).run('old',clock().date().isoformat())
 clock.advance(minutes=20);engine=Engine(store,policy,client,settings,clock)
 for row in rows:engine.ingest('reduction',dict(row,price=row['price']-10000))
 identity=Digest(store,policy,settings,clock,provider).prepare('reduction',clock().date().isoformat(),{'discovery_state':'fresh'})
 report=json.loads(store.execute('SELECT payload FROM command_center_reports WHERE outbox_id=?',(identity,)).fetchone()[0])
 assert len(report['opportunities'])==1 and report['opportunities'][0]['source_id']=='allowed-reduction'
 assert report['opportunities'][0]['section']=='price_reduction'

def test_sheraden_rule_does_not_expand_discovery_or_exclude_zip_15205_wholesale(policy):
 from rentalsourcing.request import listing_parameters
 from rentalsourcing.core import EXCLUDED_ZIPS
 assert '15205' not in EXCLUDED_ZIPS
 query=listing_parameters(policy)
 assert query['latitude']==40.4349024 and query['longitude']==-79.9805549 and query['radius']==7.9
 assert 'city' not in query and 'zipCode' not in query
 assert query['price']=='*:200001' and query['bedrooms']=='3:4' and query['bathrooms']=='1:*' and query['squareFootage']=='1000:*'
