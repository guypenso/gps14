import datetime as dt
import pytest
from conftest import listing,taxes
from test_offmarket import item
from rentalsourcing.offmarket import evaluate,cache_program_contacts,PLB,fetch_public
from rentalsourcing.core import verification
from rentalsourcing.offmarket_intelligence import analyze

def evidence(p,now,kind,**extra):
 return dict({'kind':kind,'matched_property':p,'observed_at':now.isoformat(),'source_url':'https://supplier.example.test/offer','verification_method':'authorized_supplier'},**extra)

def ready_item(now):
 d=item();p=d['property']
 d['evidence']=[evidence(p,now,'asking_price',amount=p['price'],price_kind='asking'),evidence(p,now,'contact',name='Fixture sales organization',role='Wholesaler',email='salesperson@supplier.example.test',relationship_confirmed=True,relationship='Authorized supplier for this offering'),evidence(p,now,'condition',scope='whole_property',status='confirmed',category='Light Renovation')]+[evidence(p,now,'market',source_name=n,status='no_active_sale',source_url='https://www.'+host+'/fixture') for n,host in [('zillow','zillow.com'),('redfin','redfin.com'),('realtor','realtor.com')]]
 return d

@pytest.mark.parametrize('amount,expected',[(29999,'Rejected'),(30000,'Pending Verification'),(200001,'Pending Verification'),(200002,'Rejected')])
def test_offmarket_price_boundaries_do_not_change_daily(store,boundary,clock,amount,expected):
 d=item();d['property']['price']=amount
 assert evaluate(store,boundary,[d],clock())[0]['criteria_state']==expected
 assert verification(d['property'],boundary)[0]==('Rejected' if amount>200000 else 'Candidate')

@pytest.mark.parametrize('kind',['assessed_value','auction_bid','estimate','rehab','unknown'])
def test_non_asking_numbers_never_documented(store,boundary,clock,kind):
 d=ready_item(clock());d['evidence'][0]['price_kind']=kind
 r=evaluate(store,boundary,[d],clock())[0]
 assert r['acquisition']['asking_price']['state']=='unknown';assert not r['acquisition']['queue']=='Ready to Pursue'

def test_full_gate_contacts_and_separate_leads(clock):
 d=ready_item(clock());p=d['property'];tax={'state':'verified'}
 def result(d):return analyze(p,d,'Candidate',[],tax,d['evidence'],None,clock())
 assert result(d)['queue']=='Ready to Pursue'
 d['evidence'][1]['relationship_confirmed']=False
 assert result(d)['queue']=='Research Required'
 d['evidence'][1].update(role='Public Agency',verification_method='official_api')
 assert result(d)['queue']=='Research Required' # generic municipal route is insufficient
 d['evidence'][1]['relationship_confirmed']=True
 assert result(d)['queue']=='Ready to Pursue'
 d.update(kind='lead');d['evidence'].append(evidence(p,clock(),'official_signal',signal='official_inventory'))
 assert result(d)['queue']=='Potential Seller'

def test_stored_valid_tax_does_not_imply_verified_asking_or_market(store,boundary,clock):
 d=item();p=d['property'];store.save_property(p['id'],p,'Pending Verification',[],'rentcast-live',1,(clock()-dt.timedelta(days=3)).isoformat())
 store.save_tax(p['id'],taxes(p['id']),'rentcast-live',clock().isoformat(),(clock()+dt.timedelta(days=1)).isoformat())
 r=evaluate(store,boundary,[d],clock())[0]
 assert r['tax']['state']=='verified';assert r['criteria_state']=='Pending Verification';assert r['acquisition']['queue']=='Research Required'
 assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0

def test_official_program_contact_only_applies_to_fetched_offering(store,boundary,clock):
 d=item(source_id='pittsburgh-land-bank',external_id='verified-id');d['property'].pop('price')
 result={'records':[d],'program_contact_confirmed':True,'attempted_at':clock().isoformat(),'source_sha256':'fixture'}
 cache_program_contacts(store,result)
 r=evaluate(store,boundary,[d],clock())[0]
 assert r['acquisition']['contact']['usable'];assert r['acquisition']['queue']=='Pending Verification';assert r['acquisition']['asking_price']['state']=='unknown'
 d['external_id']='unrelated-property'
 assert not evaluate(store,boundary,[d],clock())[0]['acquisition']['contact']['usable']

def test_public_contact_extraction_requires_actual_program_text(clock):
 class R:
  status=200
  def __init__(self,body):self.body=body
  def __enter__(self):return self
  def __exit__(self,*a):pass
  def read(self,n):return self.body
 class O:
  def __init__(self,text):self.text=text
  def open(self,req,**kwargs):return R(b'User-agent: *\nAllow: /' if req.full_url.endswith('robots.txt') else self.text)
 html=b'<a href="https://example.test/listing">123 FICTIONAL ST LISTING</a><h2>Please contact Pittsburgh Land Bank staff with any additional questions!</h2>staff@pghlandbank.org | 412-255-6566'
 assert fetch_public(clock(),O(html))['program_contact_confirmed']
 assert not fetch_public(clock(),O(html.replace(b'Please contact Pittsburgh Land Bank staff',b'Unrelated footer')))['program_contact_confirmed']

def test_complete_offering_can_qualify_but_not_missing_contact(store,boundary,clock):
 d=ready_item(clock());p=d['property']
 store.save_property(p['id'],p,'Pending Verification',[],'rentcast-live',1,(clock()-dt.timedelta(days=3)).isoformat())
 store.save_tax(p['id'],taxes(p['id']),'rentcast-live',clock().isoformat(),(clock()+dt.timedelta(days=1)).isoformat())
 r=evaluate(store,boundary,[d],clock())[0]
 assert r['criteria_state']=='Qualified';assert r['acquisition']['queue']=='Ready to Pursue'
 d['evidence']=[e for e in d['evidence'] if e['kind']!='contact']
 assert evaluate(store,boundary,[d],clock())[0]['acquisition']['queue']=='Research Required'
 assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0

def test_startup_records_program_evidence_before_backend_reconciliation(store,clock,monkeypatch):
 import rentalsourcing.offmarket as om
 import rentalsourcing.sync_runtime as runtime
 from rentalsourcing.sync_runtime import get
 d=item(source_id='pittsburgh-land-bank',external_id='startup-fixture')
 store.save_setting('offmarket_reconciliation_v2',{'state':'success','summary':{'verified_contacts':0}})
 monkeypatch.setenv('ENABLE_REVIEW_INVENTORY_SYNC','true')
 monkeypatch.setattr('rentalsourcing.storage.Store',lambda *a,**k:store)
 monkeypatch.setattr(store,'close',lambda:None)
 monkeypatch.setattr(om,'fetch_public',lambda now:{'records':[d],'program_contact_confirmed':True,'attempted_at':clock().isoformat()})
 monkeypatch.setattr(om,'claim_schedule',lambda *a:False)
 monkeypatch.setattr('rentalsourcing.command_center.review_configuration',lambda:('https://fixture.example.test/api/rentalsourcing/review','fixture-key','fixture-token'))
 called=[]
 def request(*args,**kwargs):
  assert get(store,'offmarket_plb_contacts_v2')['contacts']['startup-fixture']['relationship_confirmed']
  assert args[1]=='offmarket_reconcile';assert args[2]=='stored:v3:'
  result={'state':'success','next_cursor':None};kwargs['validate'](result);called.append(True);return result
 monkeypatch.setattr(runtime,'request_json',request)
 def stop(*a):raise StopIteration
 monkeypatch.setattr(om.time,'sleep',stop)
 class Thread:
  def __init__(self,*,target,**kw):self.target=target
  def start(self):
   with pytest.raises(StopIteration):self.target()
 monkeypatch.setattr(om.threading,'Thread',Thread)
 om.start_scheduler(store.test_url)
 assert called==[True];assert get(store,'offmarket_reconciliation_v3')['state']=='success'
