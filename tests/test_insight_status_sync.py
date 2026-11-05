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
