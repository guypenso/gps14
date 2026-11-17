import hashlib,io,json,urllib.error
from conftest import listing,Response
from rentalsourcing.collector import LIVE
from rentalsourcing.incident_batch_revalidation import run,JOB,KEY
from rentalsourcing.sync_runtime import get

CONFIG=('https://fixture.test/api/rentalsourcing/review-sync','s'*40,'fixture-service-token')

def failed(store,clock):
    p=listing('existing-property');store.save_property(p['id'],p,'Pending Verification',[],LIVE,1,clock().isoformat())
    body=json.dumps({'cursor':10000,'records':[{'source_id':p['id']}]})
    store.save_setting(JOB,{'offset':0,'cursor':10000,'batches':0,'done':False,'inflight':{'body':body,'records':1}})
    identity=JOB+':0:10000';key='sync_operation:listing_enrichment:'+hashlib.sha256(identity.encode()).hexdigest()
    store.save_setting(key,{'state':'dead_letter','digest':hashlib.sha256(body.encode()).hexdigest(),'error':{'http_status':400,'category':'contract'}})
    return key,body

def test_exact_incident_batch_has_only_one_separate_acknowledged_revalidation(store,clock,monkeypatch):
    monkeypatch.setattr('rentalsourcing.incident_batch_revalidation.review_configuration',lambda:CONFIG)
    key,body=failed(store,clock)
    class Wire:
        calls=0
        def open(self,request,timeout):
            self.calls+=1
            assert request.data.decode()==body and request.full_url.endswith('/review-listing-enrichment')
            return Response({'accepted':True,'matched':1,'next_cursor':None,'summary':{}})
    wire=Wire();result=run(store,clock(),wire)
    assert result['state']=='accepted' and result['receipt']['matched']==1
    assert get(store,JOB)['done'] and get(store,key)['state']=='reconciled'
    assert get(store,key)['error']['http_status']==400 and get(store,key)['original_execution_confirmed'] is False
    assert run(store,clock(),wire)==result and wire.calls==1
    assert store.execute('SELECT COUNT(*) FROM properties').fetchone()[0]==1
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0

def test_failed_diagnosis_is_held_without_loops_or_exposing_secrets(store,clock,monkeypatch):
    monkeypatch.setattr('rentalsourcing.incident_batch_revalidation.review_configuration',lambda:CONFIG)
    key,_=failed(store,clock)
    class Wire:
        calls=0
        def open(self,request,timeout):
            self.calls+=1
            raise urllib.error.HTTPError(request.full_url,400,'private',{},io.BytesIO(json.dumps({'error':'D1_ERROR: unsafe-agent@example.test '+('s'*40)}).encode()))
    wire=Wire();result=run(store,clock(),wire);run(store,clock(),wire)
    assert result['state']=='held' and result['http_status']==400 and wire.calls==1
    assert 'unsafe-agent' not in result['error_hint'] and 's'*40 not in result['error_hint']
    assert get(store,key)['state']=='dead_letter' and get(store,JOB)['offset']==0
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0

def test_changed_frozen_batch_cannot_be_replayed(store,clock,monkeypatch):
    monkeypatch.setattr('rentalsourcing.incident_batch_revalidation.review_configuration',lambda:CONFIG)
    key,_=failed(store,clock);job=get(store,JOB);job['inflight']['body']='{}';store.save_setting(JOB,job)
    assert run(store,clock())['state']=='frozen_digest_mismatch'
    assert get(store,KEY) is None and get(store,key)['state']=='dead_letter'
