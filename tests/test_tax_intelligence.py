"""Tax command center is factual, read-only and never invokes a provider."""
import datetime as dt
import hashlib
import hmac
import json

from rentalsourcing.collector import LIVE
from rentalsourcing.tax_intelligence import snapshot,provider_evidence
from rentalsourcing.tax_dataset_import import preserve_evidence
from rentalsourcing.dashboard import create_app
from conftest import listing,taxes


def test_saved_estimate_remains_pending_and_deferrals_stay_out(store,boundary,clock,monkeypatch):
    first=listing('first')
    second=listing('second')
    for p in (first,second):
        store.save_property(p['id'],p,'Pending Verification',['tax_verification_required'],LIVE,1,clock().isoformat())
    store.save_setting('penten_review:second',{'status':'rejected'})
    store.save_setting('public_tax_batch:2026',{'manifest':{'source':'official-assessment','year':2026},'estimates':{'first':{'state':'estimated','annual':900,'year':2026,'match_status':'unique_parcel','parcel_id':'ABC'}}})
    before=store.execute('SELECT COUNT(*) FROM tax_history').fetchone()[0]
    report=snapshot(store,boundary,clock())
    assert report['actionable_total']==1 and report['owner_deferred']==1
    assert report['actionable_evidence']['estimated']==1
    assert report['queue'][0]['tax_evidence']['state']=='official_data_estimate'
    assert report['queue'][0]['tax_evidence']['official_bill'] is None
    assert report['provider_calls']==0 and report['qualification_changed'] is False
    assert store.execute('SELECT COUNT(*) FROM tax_history').fetchone()[0]==before


def test_expired_provider_tax_is_historical_not_verified(clock):
    p=taxes('first',1500)
    row=(json.dumps(p),LIVE,(clock()-dt.timedelta(days=15)).isoformat(),(clock()-dt.timedelta(days=1)).isoformat())
    result=provider_evidence('first',row,clock())
    assert result['state']=='historical'
    assert result['qualification_gate']=='Pending Verification'
    assert provider_evidence('other',row,clock()) is None


def test_new_dataset_preserves_prior_estimate_and_flags_parcel_conflict():
    now=dt.datetime(2026,11,6,tzinfo=dt.timezone.utc)
    old={'missing':{'state':'estimated','annual':900,'parcel_id':'A','warnings':[]},'conflict':{'state':'estimated','annual':1200,'parcel_id':'B','warnings':[]}}
    fresh={'missing':{'state':'unknown'},'conflict':{'state':'estimated','annual':1200,'parcel_id':'C'}}
    assert preserve_evidence(fresh,old,'WPRDC',now)==(1,1)
    assert fresh['missing']['retained_from_prior_batch'] is True
    assert fresh['missing']['state']=='estimated'
    assert fresh['conflict']['state']=='unknown' and fresh['conflict']['match_status']=='conflict'


def test_signed_tax_endpoint_auth_bounds_and_response(store,boundary,clock,monkeypatch):
    monkeypatch.setenv('DASHBOARD_PASSWORD','fixture-dashboard-password-123')
    secret='tax-intelligence-test-secret-at-least-32-bytes'
    monkeypatch.setenv('COMMAND_CENTER_REVIEW_SECRET',secret)
    app=create_app(store.test_url,testing=True,clock=clock,boundary=boundary)
    client=app.test_client()
    body=json.dumps({'action':'snapshot','nonce':'a'*32,'offset':0,'limit':5},separators=(',',':')).encode()
    stamp=str(int(clock().timestamp()))
    headers={'X-RentalSourcing-Timestamp':stamp,'X-RentalSourcing-Signature':'v1='+hmac.new(secret.encode(),stamp.encode()+b'.'+body,hashlib.sha256).hexdigest(),'Content-Type':'application/json'}
    assert client.post('/api/operations/tax-intelligence',data=body).status_code==401
    response=client.post('/api/operations/tax-intelligence',data=body,headers=headers)
    assert response.status_code==200 and response.get_json()['nonce']=='a'*32
    assert hmac.compare_digest(response.headers['X-RentalSourcing-Response-Signature'],'v1='+hmac.new(secret.encode(),response.data,hashlib.sha256).hexdigest())
    invalid=json.dumps({'action':'snapshot','nonce':'a'*32,'limit':True}).encode()
    stamp=str(int(clock().timestamp()))
    headers['X-RentalSourcing-Signature']='v1='+hmac.new(secret.encode(),stamp.encode()+b'.'+invalid,hashlib.sha256).hexdigest()
    assert client.post('/api/operations/tax-intelligence',data=invalid,headers=headers).status_code==400
