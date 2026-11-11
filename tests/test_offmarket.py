import datetime as dt,json
import pytest
from rentalsourcing.offmarket import evaluate,parse_public,fetch_public,PLB
from conftest import listing,taxes

def item(**changes):
 p=listing(formattedAddress='123 Fictional St, Pittsburgh, PA 15212',addressLine1='123 Fictional St')
 return {**dict(property=p,kind='offering',availability='available',evidence_url='https://supplier.example.test/deal',purchase_eligibility='permitted'),**changes}

def test_authoritative_filters_unknown_tax_no_paid_requests(store,boundary,clock):
 r=evaluate(store,boundary,[item()],clock())[0]
 assert r['criteria_state']=='Pending Verification';assert r['tax']['state']=='unknown'
 assert 'annual_tax_bill_unverified' in r['reasons']
 assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
 for field,value,reason in [('bedrooms',5,'bedrooms'),('bedrooms',2,'price'),('price',200002,'price'),('propertyType','Condominium','condominium'),('propertyType','Townhome','townhouse'),('description','turn-key investor package','turnkey'),('zipCode','15204','excluded_zip')]:
  d=item();d['property'][field]=value;r=evaluate(store,boundary,[d],clock())[0]
  assert r['criteria_state']=='Rejected';assert reason in r['reasons']

def test_missing_not_invented_and_restrictions_not_qualified(store,boundary,clock):
 d=item();d['property']={'formattedAddress':'123 Fictional St, Pittsburgh, PA','state':'PA'}
 r=evaluate(store,boundary,[d],clock())[0];assert r['criteria_state']=='Pending Verification';assert r['tax']['state']=='unknown';assert 'coordinates' in r['reasons']
 d=item(purchase_eligibility='ineligible')
 assert evaluate(store,boundary,[d],clock())[0]['criteria_state']=='Rejected'

def test_cached_tax_and_estimates_never_inferred_verified(store,boundary,clock):
 p=item()['property'];store.save_property(p['id'],p,'Pending Verification',[],'RentCast',1,clock().isoformat())
 store.save_tax(p['id'],taxes(p['id']),'rentcast-live',clock().isoformat(),(clock()+dt.timedelta(days=1)).isoformat())
 r=evaluate(store,boundary,[item()],clock())[0]
 assert r['tax']['state']=='verified';assert r['criteria_state']=='Rejected';assert 'confirmed_active_public_listing' in r['reasons']
 store.execute('DELETE FROM tax_history');store.save_setting('public_tax_batch:2026',{'estimates':{p['id']:{'state':'estimated','annual':1200,'parcel_id':'fixture'}}})
 r=evaluate(store,boundary,[item()],clock())[0];assert r['tax']['state']=='estimated';assert r['criteria_state']=='Rejected'

def test_public_parser_preserves_real_offering_vs_unavailable_and_no_zillow_scraping(clock):
 result=parse_public('<a href="https://www.zillow.com/homedetails/fixture/123_zpid/">2118 ROCKLEDGE ST LISTING</a><a href="https://example.test/other">123 ST LISTING - PENDING</a>',clock())
 assert len(result)==2;assert result[0]['availability']=='available';assert result[1]['availability']=='unavailable'
 assert result[0]['evidence_url']==PLB;assert 'price' not in result[0]['property'];assert result[0]['purchase_eligibility']=='unknown'

def test_public_access_denied_not_success(clock):
 class R:
  status=200
  def __enter__(self):return self
  def __exit__(self,*a):pass
  def read(self,n):return b'User-agent: *\nDisallow: /'
 class O:
  def open(self,*a,**k):return R()
 with pytest.raises(ValueError,match='disallows'):fetch_public(clock(),O())

def test_backend_authentication_and_signed_criteria_response(store,boundary,clock,monkeypatch):
 import hashlib,hmac
 from rentalsourcing.dashboard import create_app
 monkeypatch.setenv('DASHBOARD_PASSWORD','fixture-dashboard-password-long');key='fixture-offmarket-key'*3;monkeypatch.setenv('COMMAND_CENTER_REVIEW_SECRET',key)
 app=create_app(store.test_url,testing=True,boundary=boundary,clock=clock);c=app.test_client();url='/api/offmarket/backend'
 assert c.post(url,json={'action':'sources'}).status_code==401
 body=json.dumps({'action':'evaluate','nonce':'a'*32,'records':[item()]});stamp=str(int(clock().timestamp()));sig='v1='+hmac.new(key.encode(),(stamp+'.'+body).encode(),hashlib.sha256).hexdigest()
 r=c.post(url,data=body,headers={'Content-Type':'application/json','X-RentalSourcing-Timestamp':stamp,'X-RentalSourcing-Signature':sig})
 assert r.status_code==200;assert r.json['nonce']=='a'*32;assert r.json['evaluations'][0]['tax']['state']=='unknown'
 assert r.headers['X-RentalSourcing-Response-Signature']=='v1='+hmac.new(key.encode(),r.data,hashlib.sha256).hexdigest()

def test_independent_schedule_is_persistent_and_not_claimed_twice(store,clock,monkeypatch):
 from rentalsourcing.offmarket import claim_schedule
 monkeypatch.setattr(store,'lock',lambda:pytest.fail('Off-market must not take the paid discovery lock'))
 assert claim_schedule(store,clock())
 assert not claim_schedule(store,clock())
 clock.advance(hours=6,seconds=1)
 assert claim_schedule(store,clock())


def test_non_live_cached_tax_never_becomes_verified(store,boundary,clock):
 p=item()['property'];store.save_property(p['id'],p,'Pending Verification',[],'rentcast-live',1,clock().isoformat())
 store.save_tax(p['id'],taxes(p['id']),'unverified-supplier',clock().isoformat(),(clock()+dt.timedelta(days=1)).isoformat())
 result=evaluate(store,boundary,[item()],clock())[0]
 assert result['tax']['state']=='unknown';assert result['criteria_state']=='Rejected';assert result['tax']['state']!='verified'


def test_controlled_listing_validation_uses_existing_claim_and_preserves_cadence(store,clock):
 from rentalsourcing.offmarket import request_listing_validation,claim_schedule,complete_listing_validation
 from rentalsourcing.sync_runtime import get
 due=(clock()+dt.timedelta(hours=4)).isoformat()
 store.save_setting('offmarket_scheduler',{'state':'success','next_at':due})
 request=request_listing_validation(store,clock())
 assert request_listing_validation(store,clock())==request
 assert request['original_next_at']==due
 assert claim_schedule(store,clock())
 assert not claim_schedule(store,clock())
 receipt={'state':'success','email':{'results':[{'state':'success','extracted':0,'failed':0}]}}
 assert complete_listing_validation(store,request,receipt,clock())==due
 assert get(store,'realscout_scheduler_validation_v1')['state']=='done'
 assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
