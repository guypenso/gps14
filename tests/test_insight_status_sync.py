import datetime as dt,json
from conftest import listing
from rentalsourcing.listing_enrichment import evidence
from rentalsourcing.price_sync import enqueue

def test_evidence_preserves_provider_claims_without_inventing_verified_rent(clock):
 p=listing(description='Renovated kitchen. New HVAC in 2023.',features={'garage':True,'basement':True,'coolingType':'Central'},rentEstimate=1600)
 e=evidence(p,clock().isoformat())
 assert e['description']==p['description'];assert e['insight_facts']['hasGarage'] is True
 assert 'documented_monthly_rent' not in e['insight_facts']
 p['currentMonthlyRent']=1200
 assert evidence(p,clock().isoformat())['insight_facts']['documented_monthly_rent']==1200

def test_status_without_price_is_durable_and_does_not_call_providers_or_mail(store,clock):
 p=listing(price=None,status='Sold');stamp=clock().isoformat()
 assert enqueue(store,p,stamp,stamp,observation_id='status-only-fixture')
 assert not enqueue(store,p,stamp,stamp,observation_id='status-only-fixture')
 row=json.loads(store.execute("SELECT payload FROM settings WHERE name LIKE ?",('price-observation:%',)).fetchone()[0])
 assert row['observation']['price_cents'] is None;assert row['observation']['listing_status']=='Sold'
 assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
 assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0

def test_reuses_valid_matching_cached_property_features_without_provider_calls(store,clock):
 from rentalsourcing.listing_enrichment import stored_evidence
 p=listing();stamp=clock().isoformat();record={'id':p['id'],'features':{'garage':True,'heatingType':'Forced Air'}}
 store.save_tax(p['id'],record,'rentcast-live',stamp,(clock()+dt.timedelta(days=1)).isoformat())
 result=stored_evidence(store,p,stamp,clock())
 assert result['insight_facts']['hasGarage'] is True
 assert result['insight_facts_provenance']['heatingType']['source']=='RentCast cached property record'
 assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
 store.save_tax(p['id'],{'id':'wrong-identity','features':{'garage':True}},'rentcast-live',stamp,(clock()+dt.timedelta(days=1)).isoformat())
 assert 'hasGarage' not in stored_evidence(store,p,stamp,clock())['insight_facts']

def test_initial_enrichment_does_not_wait_for_or_take_the_paid_scan_lock(store,monkeypatch):
 import rentalsourcing.listing_enrichment as module
 monkeypatch.setattr(store,'lock',lambda:(_ for _ in ()).throw(AssertionError('Paid discovery lock must remain untouched')))
 calls=[]
 def snapshot(current,config,**kwargs):
  assert kwargs['acquire_lock'] is False
  calls.append(kwargs['job_key']);current.save_setting(kwargs['job_key'],{'done':True});return {'state':'complete'}
 monkeypatch.setattr(module,'run',snapshot)
 assert module.initial_insights(store,('https://fixture.test/receiver','fixture','fixture'))['state']=='complete'
 assert module.initial_insights(store,('https://fixture.test/receiver','fixture','fixture'))['state']=='complete'
 assert len(calls)==1
 assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
 assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0
