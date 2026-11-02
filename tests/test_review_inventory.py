import datetime as dt,hashlib,hmac,io,json
from pathlib import Path
from dataclasses import replace
import pytest
from conftest import listing,taxes
from rentalsourcing.collector import LIVE
from rentalsourcing.review_inventory import select,run,JOB
from rentalsourcing.review_feedback import refresh
from rentalsourcing.public_tax import match_csv,calculate,norm


def add(store,identity,clock,**kw):
    p=listing(identity,**kw);store.save_property(identity,p,'Pending Verification',['tax_not_verified'],LIVE,1,clock().isoformat());return p


def test_all_stored_pending_are_reviewable_even_if_stale_and_ids_stay_stable(store,boundary,clock):
    add(store,'fresh',clock);old=clock();clock.advance(days=2);add(store,'newer',clock)
    add(store,'excluded',clock,zipCode='15211');add(store,'town',clock,propertyType='Townhouse')
    cards,counts=select(store,boundary,clock());assert counts['actionable']==2 and counts['stale']==1
    assert [c['source_id'] for c,p in cards]==['fresh','newer']
    assert all(c['qualification']=='pending_verification' and c['annual_tax'] is None for c,p in cards)
    assert 'stale_listing_status_requires_refresh' in cards[0][0]['reasons']
    assert cards[0][0]['listing_observed_at']==old.isoformat()


def test_review_feedback_is_authenticated_and_rejection_is_permanent(store,clock):
    config=('https://example.test/api/rentalsourcing/sync','s'*32,'service-fixture')
    class Response:
        status=200
        def __init__(self,body):self.body=body;self.headers={'X-RentalSourcing-Response-Signature':'v1='+hmac.new(config[1].encode(),body,hashlib.sha256).hexdigest()}
        def read(self,n):return self.body
        def __enter__(self):return self
        def __exit__(self,*a):pass
    class Opener:
        status='rejected'
        def open(self,req,timeout):
            assert req.full_url.endswith('/classifications')
            nonce=json.loads(req.data)['nonce'];return Response(json.dumps({'nonce':nonce,'decisions':[{'source_id':'fixture','status':self.status,'reviewed_at':clock().isoformat()}],'next_cursor':None},separators=(',',':')).encode())
    o=Opener();refresh(store,clock(),o,config);o.status='new';refresh(store,clock(),o,config)
    assert json.loads(store.execute('SELECT payload FROM settings WHERE name=?',('penten_review:fixture',)).fetchone()[0])['status']=='rejected'
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0


def row(**changes):
    base=dict(PARID='0001A00001000000',PROPERTYHOUSENUM='100',PROPERTYADDRESS='Fixture Avenue',PROPERTYFRACTION='',PROPERTYUNIT='',PROPERTYZIP='15212',MUNIDESC='1st Ward - PITTSBURGH',SCHOOLDESC='Pittsburgh',COUNTYTOTAL='40000',LOCALTOTAL='40000',TAXYEAR='2026',ASOFDATE='2026-10-07',TAXCODE='T',HOMESTEADFLAG='',FARMSTEADFLAG='',ABATEMENTFLAG='',CLEANGREEN='')
    base.update(changes);return base


def rates():return json.loads((Path(__file__).resolve().parents[1]/'data/tax-rates-2026.json').read_text())

def test_estimate_formula_includes_all_levies_and_never_verifies_bill():
    now=dt.datetime(2026,11,2,tzinfo=dt.timezone.utc);e=calculate(row(),rates(),now,'fixture-sha')
    assert e['state']=='estimated' and e['annual']==1092.28 and e['threshold_with_buffer']==1192.28
    assert len(e['inputs'])==5 and e['below_threshold']
    assert 'annual_tax' not in e and 'qualification' not in e
    assert calculate(row(HOMESTEADFLAG='HOM'),rates(),now,'fixture')['state']=='unknown'
    assert calculate(row(TAXYEAR='2025'),rates(),now,'fixture')['state']=='unknown'


def test_ambiguous_unmatched_units_and_municipalities_never_guessed(boundary):
    import csv
    now=dt.datetime(2026,11,2,tzinfo=dt.timezone.utc);p=listing('fixture',addressLine1='100 Fixture Ave')
    stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=list(row()));writer.writeheader();writer.writerow(row());writer.writerow(row(PARID='0001A00002000000'));stream.seek(0)
    result=match_csv(stream,[p],boundary,rates(),now,'fixture')
    assert result['fixture']['state']=='unknown' and result['fixture']['match_status']=='ambiguous'


def test_durable_chunked_backfill_recovers_without_provider_or_email_calls(store,boundary,clock):
    import urllib.error
    config=('https://example.test/api/rentalsourcing/review-sync','s'*32,'service-fixture')
    for i in range(126):add(store,'queue-'+str(i),clock,formattedAddress=str(100+i)+' Fixture St, Pittsburgh, PA 15212',addressLine1=str(100+i)+' Fixture St')
    class Response:
        status=200
        def __init__(self,body,signed=False):
            self.body=json.dumps(body,separators=(',',':')).encode();self.headers={}
            if signed:self.headers['X-RentalSourcing-Response-Signature']='v1='+hmac.new(config[1].encode(),self.body,hashlib.sha256).hexdigest()
        def read(self,n):return self.body
        def __enter__(self):return self
        def __exit__(self,*a):pass
    class Receiver:
        fail=True
        calls=[]
        def open(self,req,timeout):
            body=json.loads(req.data)
            if req.full_url.endswith('/review-classifications'):return Response({'nonce':body['nonce'],'decisions':[],'next_cursor':None},True)
            assert req.full_url.endswith('/review-sync')
            assert all(p['qualification']=='pending_verification' and p['annual_tax'] is None for p in body['opportunities'])
            self.calls.append(body['report_id'])
            if len(body['opportunities'])==1 and self.fail:raise urllib.error.HTTPError(req.full_url,503,'fixture',{},io.BytesIO(b''))
            return Response({'accepted':True,'report_id':body['report_id'],'counts':{'inserted':len(body['opportunities'])}})
    receiver=Receiver()
    with pytest.raises(ValueError,match='HTTP 503'):run(store,boundary,clock(),execute=True,config=config,opener=receiver)
    receiver.fail=False;result=run(store,boundary,clock(),execute=True,config=config,opener=receiver)
    assert result['synced_at'] and result['receipt_counts']['inserted']==126
    assert receiver.calls.count(receiver.calls[0])==1
    assert len(receiver.calls)==3
    run(store,boundary,clock(),execute=True,config=config,opener=receiver)
    assert len(receiver.calls)==3
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0
    assert store.execute('SELECT COUNT(*) FROM properties').fetchone()[0]==126
