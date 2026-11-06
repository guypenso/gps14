import datetime as dt,hashlib,hmac,io,json,urllib.error
import pytest
from conftest import Response,Provider,listing
from rentalsourcing.sync_runtime import request_json,SyncFailure,get,component,incident
from rentalsourcing.review_feedback import refresh
from rentalsourcing.reliability import snapshot,begin,stage,finish,current_inventory_audit,reconcile_completed_enrichment
CONFIG=('https://penten.example.test/api/rentalsourcing/sync','s'*40,'private-fixture-token')
def test_completed_enrichment_reconciles_only_its_own_orphaned_batch(store,clock):
 job='listing-contact-enrichment:2026-11-05:fixture'
 store.save_setting(job,{'done':True,'offset':648,'cursor':None})
 store.save_setting('listing-contact-enrichment:unfinished',{'done':False,'offset':25})
 store.save_setting('sync_operation:listing_enrichment:done',{'state':'pending','id':job+':25:10000','component':'listing_enrichment','error':{'category':'recovery_time_budget'}})
 store.save_setting('sync_operation:listing_enrichment:unfinished',{'state':'pending','id':'listing-contact-enrichment:unfinished:0:0','component':'listing_enrichment'})
 result=reconcile_completed_enrichment(store,clock())
 assert result=={'reconciled':1,'unmatched_pending':1}
 assert get(store,'sync_operation:listing_enrichment:done')['state']=='reconciled'
 assert get(store,'sync_operation:listing_enrichment:unfinished')['state']=='pending'
 assert snapshot(store,clock())['counts']['pending_operations']==1
def test_current_inventory_audit_compares_stable_source_ids_without_provider_calls(store,boundary,clock,monkeypatch):
 from types import SimpleNamespace
 monkeypatch.setattr('rentalsourcing.geography.load_boundary',lambda _:boundary)
 monkeypatch.setattr('rentalsourcing.config.Settings.from_env',lambda:SimpleNamespace(boundary_file='fixture'))
 for identity in ('mapped','missing'):
  store.save_property(identity,listing(identity),'Pending Verification',['tax_not_verified'],'rentcast-live',1,clock().isoformat())
 class Receiver:
  def open(self,req,timeout):
   assert req.full_url.endswith('/review-inventory-audit')
   body=json.loads(req.data)
   assert body['source_ids']==['mapped','missing']
   raw=json.dumps({'accepted':True,'nonce':body['nonce'],'results':[{'source_id':'mapped','matched':True,'review_status':'monitor','tax_status':'pending_verification'},{'source_id':'missing','matched':False}]},separators=(',',':')).encode()
   class Result:
    status=200
    headers={'X-RentalSourcing-Response-Signature':'v1='+hmac.new(CONFIG[1].encode(),raw,hashlib.sha256).hexdigest()}
    def read(self,n):return raw
    def __enter__(self):return self
    def __exit__(self,*a):pass
   return Result()
 report=current_inventory_audit(store,CONFIG,Receiver())
 assert report['source_records']==2 and report['pending']==2 and report['mapped']==1 and report['missing_pending']==1
 assert report['destination_review_statuses']=={'monitor':1}
 assert report['destination_tax_statuses']=={'pending_verification':1}
 assert report['pending_tax_status_mismatch']==0
 assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
class Wire:
 def __init__(self,responses):self.responses=list(responses);self.calls=[]
 def open(self,req,timeout):
  self.calls.append(req);value=self.responses.pop(0)
  if isinstance(value,Exception):raise value
  return Response(value)
def error(code):return urllib.error.HTTPError(CONFIG[0],code,'never-log-this-private-text',{'Retry-After':'120'} if code==429 else {},io.BytesIO(b'{}'))
@pytest.mark.parametrize('status',[401,403,400,404,409,413,415,422,307])
def test_permanent_failures_are_held_without_redirects_or_repeated_requests(store,clock,status):
 wire=Wire([error(status),{'accepted':True}])
 with pytest.raises(SyncFailure):request_json(store,'property_sync','report1',CONFIG,b'{}',opener=wire,now=clock(),sleeper=lambda _:None)
 clock.advance(days=1)
 with pytest.raises(SyncFailure):request_json(store,'property_sync','report1',CONFIG,b'{}',opener=wire,now=clock(),sleeper=lambda _:None)
 assert len(wire.calls)==1
 assert 'never-log' not in json.dumps(get(store,'health_incident:property_sync'))
 # Corrected configuration releases the hold; no secret is exposed.
 new=(CONFIG[0]+'-corrected',CONFIG[1],CONFIG[2])
 assert request_json(store,'property_sync','report1',new,b'{}',opener=wire,now=clock())['accepted']
 assert get(store,'health_incident:property_sync')['state']=='resolved'
@pytest.mark.parametrize('failure',[TimeoutError(),urllib.error.URLError('network'),error(502),error(503),error(504)])
def test_bounded_retry_uses_same_body_and_never_spends_provider_budget(store,clock,failure):
 wire=Wire([failure,{'accepted':True}]);delays=[]
 assert request_json(store,'property_sync','stable',CONFIG,b'{"frozen":true}',opener=wire,now=clock(),sleeper=delays.append)['accepted']
 assert len(wire.calls)==2 and delays==[.25]
 assert wire.calls[0].data==wire.calls[1].data
 assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
 assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0
 assert request_json(store,'property_sync','stable',CONFIG,b'{"frozen":true}',opener=wire,now=clock())['accepted']
 assert len(wire.calls)==2
 with pytest.raises(SyncFailure,match='frozen_operation_changed'):request_json(store,'property_sync','stable',CONFIG,b'{"changed":true}',opener=wire,now=clock())
def test_persisted_backoff_and_safe_restart(store,clock):
 wire=Wire([TimeoutError(),TimeoutError(),TimeoutError(),{'accepted':True}])
 with pytest.raises(SyncFailure):request_json(store,'price_sync','persistent',CONFIG,b'{}',opener=wire,now=clock(),sleeper=lambda _:None)
 assert len(wire.calls)==3
 from rentalsourcing.storage import Store
 resumed=Store(store.test_url,initialize=False)
 try:
  with pytest.raises(SyncFailure,match='recovery_scheduled'):request_json(resumed,'price_sync','persistent',CONFIG,b'{}',opener=wire,now=clock())
  assert len(wire.calls)==3;clock.advance(minutes=2)
  assert request_json(resumed,'price_sync','persistent',CONFIG,b'{}',opener=wire,now=clock())['accepted']
 finally:resumed.close()
def test_rate_limit_obeys_retry_after_without_immediate_replay(store,clock):
 wire=Wire([error(429),{'accepted':True}])
 with pytest.raises(SyncFailure):request_json(store,'feedback','limited',CONFIG,b'{}',opener=wire,now=clock(),sleeper=lambda _:pytest.fail('No immediate 429 retry'))
 clock.advance(seconds=60)
 with pytest.raises(SyncFailure,match='recovery_scheduled'):request_json(store,'feedback','limited',CONFIG,b'{}',opener=wire,now=clock())
 assert len(wire.calls)==1;clock.advance(seconds=61)
 assert request_json(store,'feedback','limited',CONFIG,b'{}',opener=wire,now=clock())['accepted']
class Feed:
 def __init__(self,decision):self.decision=decision
 def open(self,req,timeout):
  d={'nonce':json.loads(req.data)['nonce'],'decisions':[self.decision],'next_cursor':None};raw=json.dumps(d).encode();r=Response(d);r.headers={'X-RentalSourcing-Response-Signature':'v1='+hmac.new(CONFIG[1].encode(),raw,hashlib.sha256).hexdigest()};r.read=lambda n:raw;return r
def test_out_of_order_feedback_never_overwrites_human_actions_or_tombstones(store,clock):
 d={'source_id':'fixture','status':'monitor','reviewed_at':clock().isoformat()};wire=Feed(d);refresh(store,clock(),wire,CONFIG)
 wire.decision={**d,'status':'pursue','reviewed_at':(clock()-dt.timedelta(days=1)).isoformat()};result=refresh(store,clock(),wire,CONFIG)
 assert result['stale_ignored']==1 and get(store,'penten_review:fixture')['status']=='monitor'
 wire.decision={**d,'status':'rejected'};refresh(store,clock(),wire,CONFIG)
 wire.decision={**d,'status':'new','reviewed_at':(clock()+dt.timedelta(days=1)).isoformat()};refresh(store,clock(),wire,CONFIG)
 assert get(store,'penten_review:fixture')['status']=='rejected'
 assert store.execute('SELECT COUNT(*) FROM settings WHERE name LIKE ?',('penten_review_history:%',)).fetchone()[0]>0
def test_email_sent_does_not_mask_unsynchronized_report(store,clock):
 from rentalsourcing.storage import Store
 begin(store,'2026-10-16',clock());stage(store,'2026-10-16','email_delivery','success',clock())
 store.execute("INSERT INTO outbox VALUES ('report','daily:2026-10-16','sent','{}','[]','accepted',NULL,?)",(clock().isoformat(),))
 store.execute("INSERT INTO command_center_reports(outbox_id,payload) VALUES ('report','{}')")
 assert finish(store,'2026-10-16',clock())=='partial'
 assert snapshot(store,clock())['counts']['pending_reports']==1
 assert store.execute('SELECT state FROM outbox').fetchone()[0]=='sent'
def test_incident_is_one_episode_and_recovery_is_only_recorded_after_success(store,clock):
 for _ in range(3):incident(store,'property_sync',{'category':'network','retryable':True},now=clock())
 first=get(store,'health_incident:property_sync');incident(store,'property_sync',None,now=clock(),resolved=True)
 assert get(store,'health_incident:property_sync')['id']==first['id']
 assert get(store,'health_incident:property_sync')['state']=='resolved'
def test_paid_tax_gate_does_not_depend_on_how_many_listing_calls_were_used(store,boundary,settings,clock):
 from rentalsourcing.collector import Client
 from rentalsourcing.engine import Engine
 provider=Provider();client=Client(store,settings,clock,provider,lambda _:None)
 Engine(store,boundary,client,settings,clock,allow_paid_tax=False).run('guard',clock().date().isoformat())
 assert provider.tax_calls==0;assert not provider.messages

def test_daily_discovery_and_email_survive_feedback_failure_but_health_is_partial(store,boundary,settings,clock,monkeypatch):
 from rentalsourcing.cloud import daily_discovery
 import rentalsourcing.command_center as cc
 config=(CONFIG[0].replace('/sync','/review-sync'),CONFIG[1],CONFIG[2]);monkeypatch.setattr(cc,'review_configuration',lambda:config);monkeypatch.setattr(cc,'configuration',lambda:None)
 provider=Provider()
 class Receiver:
  def open(self,req,timeout):
   if req.full_url.endswith('/review-classifications'):raise error(503)
   if req.full_url.endswith('/review-operations'):return Response({'accepted':True})
   return provider.open(req,timeout)
 result=daily_discovery(store,boundary,settings,clock().date().isoformat(),clock=clock,opener=Receiver())
 assert result['status']=='sent' and result['workflow_status']=='partial'
 assert provider.tax_calls==0 and len(provider.messages)==1
 assert result['unresolved_critical']>0
 again=daily_discovery(store,boundary,settings,clock().date().isoformat(),clock=clock,opener=Receiver())
 assert again['status']=='already_sent' and len(provider.messages)==1

def test_changed_feedback_snapshot_is_atomic_and_recoverable(store,clock):
 class Pages:
  def __init__(self):self.calls=0
  def open(self,req,timeout):
   self.calls+=1;nonce=json.loads(req.data)['nonce'];d={'nonce':nonce,'decisions':[{'source_id':str(self.calls),'status':'rejected','reviewed_at':clock().isoformat()}],'snapshot_version':('a' if self.calls==1 else 'b')*64,'next_cursor':'next' if self.calls==1 else None};raw=json.dumps(d).encode();r=Response(d);r.read=lambda n:raw;r.headers={'X-RentalSourcing-Response-Signature':'v1='+hmac.new(CONFIG[1].encode(),raw,hashlib.sha256).hexdigest()};return r
 with pytest.raises(SyncFailure,match='snapshot_changed'):refresh(store,clock(),Pages(),CONFIG)
 assert store.execute('SELECT COUNT(*) FROM settings WHERE name LIKE ?',('penten_review:%',)).fetchone()[0]==0
 assert get(store,'health_component:review_feedback')['state']=='partial'

def test_older_workflow_feedback_cannot_reverse_a_newer_contract_state(store,clock):
 d={'source_id':'workflow','status':'pursue','reviewed_at':clock().isoformat(),'contract_state':'terminated','workflow_at':clock().isoformat(),'workflow_by':'guy'};wire=Feed(d);refresh(store,clock(),wire,CONFIG)
 wire.decision={**d,'contract_state':'active','workflow_at':(clock()-dt.timedelta(days=1)).isoformat()};refresh(store,clock(),wire,CONFIG)
 assert get(store,'penten_review:workflow')['contract_state']=='terminated'
 from rentalsourcing.tax_backlog import review_deferred
 assert review_deferred('workflow',store)

def test_every_paid_tax_path_requires_fresh_feedback_when_integration_is_enabled(store,boundary,settings,clock,monkeypatch):
 from rentalsourcing.collector import Client
 from rentalsourcing.engine import Engine
 monkeypatch.setenv('ENABLE_COMMAND_CENTER_REVIEW_FEEDBACK','true')
 provider=Provider();client=Client(store,settings,clock,provider,lambda _:None)
 Engine(store,boundary,client,settings,clock).run('default-guard',clock().date().isoformat())
 assert provider.tax_calls==0

def test_missing_schedule_and_unfinished_run_are_never_healthy(store,clock):
 d=snapshot(store,clock());assert d['state'] in ('partial','failed')
 begin(store,clock().date().isoformat(),clock());assert snapshot(store,clock())['state']!='success'
 clock.advance(hours=1);snapshot(store,clock())
 assert get(store,'health_incident:daily_schedule')['state']=='open'
