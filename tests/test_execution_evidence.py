import datetime as dt
import json
from conftest import listing
from rentalsourcing.collector import LIVE
from rentalsourcing.execution_evidence import snapshot,compact_quality
from rentalsourcing.digest import render


def test_execution_evidence_is_select_only_and_preserves_frozen_email(store,clock,boundary,monkeypatch):
    day=clock().date().isoformat()
    payload={'subject':'Fixture morning report','text':'Existing listing evidence rechecked; no fresh discovery request in this run.',
             'to':['private-owner@example.test'],'from':'private-sender@example.test','secret':'never expose'}
    store.execute('INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?)',('outbox-id','daily:'+day,'sent',json.dumps(payload),'[]','provider-accepted-id',clock().isoformat(),clock().isoformat()))
    store.save_setting('discovery_run:'+day+':morning',{'day':day,'slot':'morning','state':'partial','result':{'requests':0,'status':'sent','unresolved_critical':6}})
    store.save_setting('health_incident:source',{'component':'source','state':'open','error':{'category':'source_blocked','retryable':False,'private':'secret'}})
    store.save_setting('sync_operation:offmarket:test',{'component':'offmarket_refresh','state':'pending','endpoint':'https://fixture.test/api/source?secret=hidden','error':None})
    original=store.execute
    def readonly(sql,args=()):
        assert sql.lstrip().upper().startswith('SELECT ')
        return original(sql,args)
    monkeypatch.setattr(store,'execute',readonly)
    report=snapshot(store,clock(),boundary)
    assert report['result']['requests']==0 and report['email']['provider_receipt_present']
    assert report['email']['content_excerpt']==payload['text']
    assert report['operations']['pending_count']==report['operations']['open_incident_count']==1
    text=json.dumps(report)
    for secret in ['private-owner','private-sender','never expose','secret=hidden']:
        assert secret not in text
    assert report['provider_calls']==0 and report['qualification_changed'] is False


def test_quality_response_is_bounded_without_erasing_stored_evidence():
    value={'counts':{'evidence_hold':1},'records':[{'id':str(i),'address':'fixture',
        'state':'evidence_hold','type':{'evidence':['large evidence '*1000]*10}} for i in range(100)]}
    result=compact_quality(value)
    assert len(result['records'])==50 and result['records_total']==100
    assert len(json.dumps(result))<15000 and len(value['records'][0]['type']['evidence'])==10


def test_verification_only_report_never_claims_zero_market_opportunities():
    text,_=render('2026-11-14',[],{'discovery_state':'verification_only','discovery_complete':True},{'remaining':5},[])
    assert 'לא בוצעה בקשת מודעות חדשה' in text
    assert 'אין נכסים חדשים או הורדות מחיר חדשות שעברו' not in text
