import datetime as dt,json
from conftest import listing,taxes
from rentalsourcing.collector import LIVE
from rentalsourcing.engine import Engine
from rentalsourcing.future_discovery import process

class Provider:
    source=LIVE

def test_resumed_old_job_is_recovered_without_counting_old_properties_as_new(store,boundary,settings,clock,monkeypatch):
    engine=Engine(store,boundary,Provider(),settings,clock)
    engine.ingest('resumed',listing('old-observation'))
    clock.advance(hours=1)
    started=clock().isoformat()
    engine.ingest('resumed',listing('new-observation'))
    def accepted(*a,**kw):
        return {'batches':[{'payload':json.dumps({'opportunities':[{},{}]})}],
                'receipt_counts':{'inserted':1,'updated':1}}
    monkeypatch.setattr('rentalsourcing.future_discovery.run',accepted)
    result=process(store,boundary,'resumed',clock(),config=('https://fixture.test','key','token'),new_since=started)
    assert result['new_source_listings']==1 and result['recovered_source_listings']==1
    assert result['new_pending']==1 and result['synced']==2
    assert result['receipt_counts']=={'inserted':1,'updated':1}

def test_first_discovery_is_durable_and_old_decisions_are_not_reopened(store,boundary,settings,clock):
    engine=Engine(store,boundary,Provider(),settings,clock)
    engine.ingest('future',listing('new'))
    engine.ingest('later',listing('new'))
    store.save_property('old',listing('old'),'Pending Verification',[],LIVE,1,clock().isoformat())
    engine.ingest('future',listing('old'))
    report=process(store,boundary,'future',clock())
    assert report['new_source_listings']==1 and report['new_pending']==1
    assert process(store,boundary,'later',clock())['new_source_listings']==0
    assert report['paid_tax_calls']==0 and store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0


def test_new_cache_qualified_estimate_unknown_and_tax_rejected(store,boundary,settings,clock):
    engine=Engine(store,boundary,Provider(),settings,clock)
    for identity in ['verified','estimate','unknown','rejected']:
        if identity in ['verified','rejected']:
            store.save_tax(identity,taxes(identity,1500 if identity=='verified' else 2300.01),LIVE,clock().isoformat(),(clock()+dt.timedelta(days=7)).isoformat())
        engine.ingest('future',listing(identity))
    store.save_setting('public_tax_batch:2026',{'estimates':{'estimate':{'state':'estimated','annual':900,'match_status':'unique_parcel'}}})
    report=process(store,boundary,'future',clock())
    assert report['new_qualified']==1 and report['new_pending']==2
    assert report['new_estimated_tax']==1 and report['new_unknown_tax']==1 and report['new_rejected']==1


def test_failed_delivery_persists_frozen_future_batch_without_email(store,boundary,settings,clock):
    engine=Engine(store,boundary,Provider(),settings,clock);engine.ingest('future',listing('new'))
    class Unavailable:
        def open(self,*args,**kwargs):raise TimeoutError()
    report=process(store,boundary,'future',clock(),config=('https://example.test/api/rentalsourcing/sync','s'*32,'fixture'),opener=Unavailable())
    assert report['state']=='pending_recovery'
    ledger=json.loads(store.execute('SELECT payload FROM settings WHERE name LIKE ?',('stored-review-inventory:future:future:%',)).fetchone()[0])
    assert ledger['delivery_scope']=='daily' and len(ledger['batches'])==1
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0


def test_daily_default_does_not_buy_tax_records(store,boundary,settings,clock,monkeypatch):
    from test_minimal_daily import Provider as DailyProvider,approved
    from rentalsourcing.cloud import daily_discovery
    monkeypatch.delenv('ENABLE_PAID_TAX_VERIFICATION',raising=False)
    provider=DailyProvider()
    daily_discovery(store,boundary,approved(settings),clock().date().isoformat(),clock=clock,opener=provider)
    assert not any('/properties/' in req.full_url for req in provider.calls)
    report=json.loads(store.execute('SELECT payload FROM settings WHERE name LIKE ?',('future_discovery_report:%',)).fetchone()[0])
    assert report['new_source_listings']==1 and report['new_pending']==1


def test_new_qualified_and_pending_delivery_retries_are_idempotent(store,boundary,settings,clock):
    engine=Engine(store,boundary,Provider(),settings,clock)
    store.save_tax('verified',taxes('verified',1500),LIVE,clock().isoformat(),(clock()+dt.timedelta(days=7)).isoformat())
    for identity in ['verified','unknown']:engine.ingest('future',listing(identity))
    class Response:
        status=200
        headers={}
        def __init__(self,p):self.body=json.dumps({'accepted':True,'report_id':p['report_id'],'counts':{'received':len(p['opportunities'])}}).encode()
        def read(self,n):return self.body
        def __enter__(self):return self
        def __exit__(self,*args):pass
    class Receiver:
        calls=0
        def open(self,req,timeout):
            self.calls+=1;p=json.loads(req.data)
            assert req.full_url.endswith('/sync')
            cards={c['source_id']:c for c in p['opportunities']}
            assert cards['verified']['qualification']=='verified'
            assert cards['unknown']['qualification']=='pending_verification'
            return Response(p)
    receiver=Receiver();config=('https://example.test/api/rentalsourcing/sync','s'*32,'fixture')
    assert process(store,boundary,'future',clock(),config=config,opener=receiver)['synced']==2
    repeated=process(store,boundary,'future',clock(),config=config,opener=receiver)
    assert repeated['synced']==0 and repeated['new_source_listings']==0
    assert receiver.calls==1
