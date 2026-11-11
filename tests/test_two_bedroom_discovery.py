import datetime as dt,json
from dataclasses import replace
import pytest
from conftest import listing,Provider
from rentalsourcing.core import verification
from rentalsourcing.criteria import reconcile_bedroom_policy
from rentalsourcing.collector import LIVE
from rentalsourcing.cloud import daily_discovery,scheduled_scope
from rentalsourcing.schedule import scheduled_slot,ISRAEL

@pytest.mark.parametrize('beds,price,expected',[(2,149999,'Candidate'),(2,150000,'Candidate'),(2,150001,'Rejected'),(3,200001,'Candidate'),(4,200001,'Candidate'),(3,200002,'Rejected'),(4,200002,'Rejected'),(1,100000,'Rejected'),(5,100000,'Rejected')])
def test_bedroom_price_matrix(boundary,beds,price,expected):
 assert verification(listing(bedrooms=beds,price=price),boundary)[0]==expected

@pytest.mark.parametrize('changes',[{'squareFootage':999},{'bathrooms':0},{'propertyType':'Condo'},{'propertyType':'Townhouse'},{'zipCode':'15203'},{'listingDescription':'Fully turnkey investment property'}])
def test_two_bedrooms_never_bypass_other_rules(boundary,changes):
 assert verification(listing(bedrooms=2,price=150000,**changes),boundary)[0]=='Rejected'

def test_historical_automated_rejections_only_and_no_stale_revival(store,boundary,clock):
 for identity in ('fresh','stale','human'):
  p=listing(identity,bedrooms=2,price=150000)
  observed=clock()-dt.timedelta(days=2) if identity=='stale' else clock()
  store.save_property(identity,p,'Rejected',['bedrooms'],LIVE,1,observed.isoformat())
 store.save_setting('penten_review:human',{'status':'rejected'})
 r=reconcile_bedroom_policy(store,boundary,clock())
 assert r['fresh_candidates']==1 and r['pending_verification']==1 and r['stale_held']==1 and r['human_decisions_preserved']==1
 assert r['source_ids']==['fresh']
 assert store.get_property('human')['state']=='Rejected' and store.get_property('stale')['state']=='Rejected'
 assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
 assert reconcile_bedroom_policy(store,boundary,clock())==r

def test_morning_evening_dst_slots():
 for day in (dt.date(2026,3,27),dt.date(2026,10,25),dt.date(2027,3,26),dt.date(2027,10,31)):
  for hour,slot in ((8,'morning'),(20,'evening')):
   now=dt.datetime.combine(day,dt.time(hour),tzinfo=ISRAEL).astimezone(dt.timezone.utc)
   assert scheduled_slot(now)==(day.isoformat(),slot)
   assert scheduled_slot(now+dt.timedelta(hours=1)) is None

def test_evening_discovers_two_bedrooms_without_digest_and_is_idempotent(store,boundary,clock,settings,monkeypatch):
 monkeypatch.delenv('ENABLE_COMMAND_CENTER_REVIEW_FEEDBACK',raising=False)
 monkeypatch.delenv('ENABLE_REVIEW_INVENTORY_SYNC',raising=False)
 p=Provider([listing('unseen-two',bedrooms=2,price=150000)])
 s=replace(settings,enabled=True,email_enabled=True,per_run=1)
 first=daily_discovery(store,boundary,s,clock().date().isoformat(),clock=clock,opener=p,slot='evening')
 assert first['status']=='evening_no_email' and first['requests']==1
 assert not p.messages and store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0
 assert store.get_property('unseen-two')['state']=='Pending Verification'
 again=daily_discovery(store,boundary,s,clock().date().isoformat(),clock=clock,opener=p,slot='evening')
 assert again['already_executed'] and len(p.calls)==1

def test_scope_cursor_retains_matching_query_after_partial_page(store,boundary,clock):
 day=clock().date().isoformat();job,query,n=scheduled_scope(store,day,'morning',boundary)
 assert query['bedrooms']=='2:4'
 store.job(job,day);store.checkpoint(job,500,False,{})
 assert scheduled_scope(store,day,'evening',boundary)[:2]==(job,query)

def test_resumed_page_delivers_new_identities_without_reusing_frozen_first_batch(store,boundary,clock,monkeypatch):
 from rentalsourcing.future_discovery import process
 delivered=[]
 def receive(*args,**kwargs):
  delivered.append((kwargs['job_key'],kwargs['source_ids']))
  return {'batches':[{'payload':json.dumps({'opportunities':[{}]})}]}
 monkeypatch.setattr('rentalsourcing.future_discovery.run',receive)
 for identity in ('first-page','second-page'):
  p=listing(identity,bedrooms=2,price=150000)
  store.save_property(identity,p,'Pending Verification',['tax_not_verified'],LIVE,1,clock().isoformat())
  store.save_setting('first_discovery:'+identity,{'source_id':identity,'job_id':'partial-scan','at':clock().isoformat(),'non_tax_state':'Candidate'})
  r=process(store,boundary,'partial-scan',clock(),config=('https://fixture.test','x','x'))
  assert r['new_source_listings']==1 and r['synced']==1
 assert delivered[0][0]!=delivered[1][0] and delivered[1][1]=={'second-page'}
 assert process(store,boundary,'partial-scan',clock(),config=('https://fixture.test','x','x'))['new_source_listings']==0
