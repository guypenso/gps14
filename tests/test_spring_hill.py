"""Synthetic properties, authoritative public polygons, no live traffic."""
import datetime as dt
import json
from pathlib import Path
import pytest
from rentalsourcing.geography import load_boundary,load_region
from rentalsourcing.core import verification,report_eligible
from rentalsourcing.collector import LIVE
from rentalsourcing.targeted_sync import prepare,run,SOURCE_ID,ORIGINAL,IDENTITY,JOB
from rentalsourcing.command_center import snapshot
from conftest import listing

@pytest.fixture
def policy():return load_boundary(Path(__file__).resolve().parents[1]/'data/pittsburgh.geojson')

def spring(policy,**kw):
 p=policy.spring_hill.geometry.representative_point()
 return listing(**dict(dict(id=SOURCE_ID,longitude=p.x,latitude=p.y,zipCode='15212'),**kw))

def seed(store,policy,clock):
 row=spring(policy)
 store.save_property(SOURCE_ID,row,'Rejected',['outside_brighton_heights_15212'],LIVE,1,clock().isoformat())
 store.execute('INSERT INTO price_history VALUES (?,?,?,?,?,?)',('fixture-event',SOURCE_ID,row['price'],None,0,clock().isoformat()))
 text='2106 Rockledge St, Pittsburgh, PA 15212 | $109000 | 3 beds / 2 baths / 1232 sqft | ZIP 15212 | Status Active\nTax/year: not verified | Missing: tax_not_verified\nObserved: 2026-10-28T06:01:31.220358+00:00 | Source: RentCast\n'
 store.execute('INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?)',(ORIGINAL,'daily:2026-10-28','sent',json.dumps({'text':text}),'[]','fixture-provider',clock().isoformat(),clock().isoformat()))
 return row

def test_both_allowed_and_other_15212_rejected(policy):
 for boundary in [policy.brighton,policy.spring_hill]:
  p=boundary.geometry.representative_point();row=listing(longitude=p.x,latitude=p.y,zipCode='15212')
  assert verification(row,policy)==('Candidate',[])
 row=spring(policy,longitude=-79.9961,latitude=40.4406,neighborhood='Spring Hill')
 assert verification(row,policy)[0]=='Rejected'
 for name in ['Spring Hill','Spring Hill-City View','Spring Hill - City View','Unknown']:
  assert report_eligible(spring(policy,neighborhood=name),policy)
 assert not report_eligible(spring(policy,longitude=None),policy)

@pytest.mark.parametrize('kind',['Townhouse','Townhome','Town House','Single Family Attached'])
def test_townhouse_still_rejected_in_new_neighborhood(policy,kind):
 assert not report_eligible(spring(policy,propertyType=kind),policy)

def test_missing_spring_polygon_fail_closed(tmp_path):
 with pytest.raises(FileNotFoundError):load_region(tmp_path,'spring-hill-city-view')

def test_pending_report_and_sync_share_policy(store,policy,clock):
 row=spring(policy);store.save_property(SOURCE_ID,row,'Pending Verification',['tax_not_verified'],LIVE,1,clock().isoformat())
 # Use existing accepted fixture outbox for snapshot FK.
 seed_report='daily-'+('b'*64)
 store.execute('INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?)',(seed_report,'fixture','sent','{}','[]',None,None,clock().isoformat()))
 snapshot(store,seed_report,'2026-10-28',[],{'pending_properties':[{'listing':row,'observed_at':clock().isoformat(),'reasons':['tax_not_verified']}]},clock(),policy)
 report=json.loads(store.execute('SELECT payload FROM command_center_reports').fetchone()[0]);card=report['opportunities'][0]
 assert card['source_id']==SOURCE_ID and card['tax_status']=='pending_verification' and card['qualification']=='pending_verification'


def test_targeted_recovery_is_readonly_until_execute_and_stable_identity(store,policy,clock,capsys):
 seed(store,policy,clock);body,_,_=prepare(store,policy,clock());card=json.loads(body)['opportunities'][0]
 assert card['source_id']==SOURCE_ID and card['tax_status']=='pending_verification'
 assert json.loads(body)['report_date']=='2026-10-28' and json.loads(body)['report_id']!=ORIGINAL
 run(store,policy,clock());assert store.execute('SELECT COUNT(*) FROM settings WHERE name=?',(JOB,)).fetchone()[0]==0
 class Response:
  status=200
  def __enter__(self):return self
  def __exit__(self,*a):pass
  def read(self,n):return json.dumps({'accepted':True,'report_id':IDENTITY}).encode()
 class Receiver:
  calls=0
  def open(self,request,timeout):
   self.calls+=1;assert timeout==20
   assert request.get_header('Oai-sites-authorization')=='Bearer fixture-service'
   return Response()
 receiver=Receiver();config=('https://example.test/api/rentalsourcing/sync','x'*32,'fixture-service')
 run(store,policy,clock(),True,config,receiver);run(store,policy,clock(),True,config,receiver)
 assert receiver.calls==1
 assert store.get_property(SOURCE_ID)['state']=='Pending Verification'
 assert store.execute('SELECT COUNT(*) FROM price_history').fetchone()[0]==1
 assert store.execute('SELECT state FROM outbox').fetchone()[0]=='sent'
 assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0


def test_targeted_recovery_fails_closed_for_changed_email_or_inactive(store,policy,clock):
 row=seed(store,policy,clock)
 store.save_property(SOURCE_ID,dict(row,status='Inactive'),'Rejected',[],LIVE,1,clock().isoformat())
 with pytest.raises(ValueError,match='mandatory filters'):prepare(store,policy,clock())
 store.save_property(SOURCE_ID,row,'Pending Verification',[],LIVE,1,clock().isoformat())
 store.execute('UPDATE outbox SET payload=? WHERE id=?',(json.dumps({'text':'unverified unrelated inventory'}),ORIGINAL))
 with pytest.raises(ValueError,match='selected card'):prepare(store,policy,clock())
