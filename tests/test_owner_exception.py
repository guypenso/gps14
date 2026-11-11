import datetime as dt
from rentalsourcing.owner_exception import snapshot,matches
from conftest import listing

def test_owner_address_is_exact():
 assert matches({'addressLine1':'4472 FIRST ST','zipCode':'15102','state':'PA'})
 assert not matches({'addressLine1':'4470 1st St','zipCode':'15102'})
 assert not matches({'addressLine1':'4472 1st St','zipCode':'15102','addressLine2':'A'})
 assert not matches({'addressLine1':'4472 1st St','zipCode':'15101'})

def test_stored_rejected_evidence_lookup_is_readonly_and_no_paid_requests(store,clock):
 from rentalsourcing.engine import Engine
 from rentalsourcing.geography import load_boundary
 from rentalsourcing.config import Settings
 from pathlib import Path
 from conftest import Provider
 p=listing(id='existing-owner-candidate',addressLine1='4472 1st St',formattedAddress='4472 1st St, Bethel Park, PA 15102',city='Bethel Park',zipCode='15102',squareFootage=980)
 engine=Engine(store,load_boundary(Path(__file__).resolve().parents[1]/'data/pittsburgh.geojson'),Provider([]),Settings.from_env(),clock)
 engine.ingest('owner-test',p)
 before=store.execute('SELECT id,payload,state,reasons FROM properties').fetchall();usage=store.execute('SELECT * FROM usage').fetchall()
 data=snapshot(store,clock());assert data['provider_calls']==0;assert len(data['matches'])==1;assert data['matches'][0]['source_id']==p['id'];assert data['matches'][0]['stored_state']=='Rejected'
 assert store.execute('SELECT id,payload,state,reasons FROM properties').fetchall()==before;assert store.execute('SELECT * FROM usage').fetchall()==usage

def test_demo_cache_cannot_supply_verified_owner_tax(store,clock):
 from rentalsourcing.collector import LIVE
 from conftest import listing
 p=listing(id='owner-demo-guard',addressLine1='4472 1st St',formattedAddress='4472 1st St, Bethel Park, PA 15102',city='Bethel Park',zipCode='15102',squareFootage=980)
 store.execute('INSERT INTO properties(id,payload,source,observed_at,state,reasons) VALUES (?,?,?,?,?,?)',(p['id'],__import__('json').dumps(p),LIVE,clock().isoformat(),'Rejected','["sqft"]'))
 record={**p,'demo':True,'propertyTaxes':{str(clock().year):{'total':1455}}}
 store.save_tax(p['id'],record,LIVE,clock().isoformat(),(clock()+dt.timedelta(days=1)).isoformat())
 assert snapshot(store,clock())['matches'][0]['cached_tax'] is None
