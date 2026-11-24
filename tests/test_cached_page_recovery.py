import datetime as dt
import hashlib
import hmac
import io
import json
import urllib.error
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import Response, listing
from rentalsourcing import early_continuation as early
from rentalsourcing import reliability
from rentalsourcing.billing import cycle_key
from rentalsourcing.collector import LIVE, request_url, response_key
from rentalsourcing.monthly_allocation import listing_plan, period
from rentalsourcing.municipal_coverage import UNIFIED, unified_query
from rentalsourcing.page_recovery import reconcile_page
from rentalsourcing.partition_discovery import PREFIX
from rentalsourcing.storage import BudgetExceeded
from rentalsourcing.sync_runtime import get
from test_monthly_allocation import grant
from test_municipal_hybrid_discovery import from_area

CONFIG = ('https://penten.example.test/api/rentalsourcing/review-sync','a'*40,'test-service')
HEAD_AT = '2026-11-23T18:02:16.384040+00:00'


class Receiver:
    def __init__(self, matched=()):
        self.matched = set(matched)
        self.batches = []
        self.deliveries = []

    def open(self, req, timeout):
        body = json.loads(req.data)
        if req.full_url.endswith('/review-inventory-audit'):
            ids = body['source_ids']
            self.batches.append(ids)
            results = [{'source_id':i,'matched':i in self.matched,'review_status':'new','tax_status':'pending_verification'} for i in ids]
            value = {'accepted':True,'results':results,'nonce':body['nonce']}
            raw = json.dumps(value).encode()
            response = io.BytesIO(raw)
            response.status, response.headers = 200, {}
            response.headers['X-RentalSourcing-Response-Signature'] = 'v1='+hmac.new(CONFIG[1].encode(),raw,hashlib.sha256).hexdigest()
            return response
        assert req.full_url.endswith('/review-reports')
        ids = [row['source_id'] for row in body['opportunities']]
        assert not set(ids).intersection(self.matched)
        self.deliveries.extend(ids)
        self.matched.update(ids)
        response=io.BytesIO(json.dumps({'accepted':True,'report_id':body['report_id'],'counts':{'inserted':len(ids),'updated':0,'rejected':0}}).encode())
        response.status,response.headers=200,{}
        return response


def put_cache(store, query, offset, rows, total, when):
    store.cache_put(response_key(request_url('/listings/sale',dict(query,offset=offset))),
                    {'data':rows,'total':str(total),'fetched_at':when},LIVE,when)


def test_cached_replay_rechecks_all_rows_and_delivers_only_fresh_missing(store, boundary, clock):
    now = clock()
    query={'limit':500,'offset':0}
    rows=[listing('pending'),listing('old'),listing('managed'),listing('condo',propertyType='Condo')]
    for r in rows:
        store.save_property(r['id'],r,'Pending Verification',[],LIVE,1,now.isoformat())
    store.save_setting('penten_review:managed',{'status':'rejected'})
    put_cache(store,query,0,rows,4,now.isoformat())
    page={'offset_requested':0,'fetched_at':now.isoformat(),'job_id':'job','records':4}
    run={'run_key':'closed','new_source_ids':['condo','pending'],'partitions':{UNIFIED:{'query':query}}}
    destination=Receiver(['old'])
    result=reconcile_page(store,boundary,now,run,page,CONFIG,destination)
    assert result['dispositions']=={'hard_rejected':1,'owner_decision_preserved':1,'represented_in_penten':2}
    assert result['delivery_acknowledgments']['inserted']==1 and destination.deliveries==['pending']
    assert len(result['new_sources'])==2 and result['missing_after_delivery']==0
    assert reconcile_page(store,boundary,now,run,page,CONFIG,destination)==result
    assert destination.deliveries==['pending'] and store.count_usage(now.strftime('day:%Y-%m-%d'))==0
    assert reconcile_page(store,boundary,now,run,dict(page,fetched_at='wrong'),CONFIG,destination)['state']=='exact_cached_response_unavailable'


def test_authenticated_inventory_each_batch_uses_same_signing_secret():
    receiver=Receiver()
    ids=[str(i) for i in range(52)]
    assert list(reliability.destination_inventory(CONFIG,ids,receiver))==ids
    assert [len(p) for p in receiver.batches]==[25,25,2]


def test_draining_acknowledged_prices_and_errors_are_reported_separately(store,clock,monkeypatch):
    now=clock()
    started=(now-dt.timedelta(minutes=4)).isoformat()
    counts=dict(pending_reports=0,pending_operations=1,dead_letters=0,pending_inventory_jobs=0,pending_price_observations=320)
    details=[{'state':'pending','component':'price_sync','error':None}]
    monkeypatch.setattr(reliability,'audit_scheduled_slots',lambda *a,**kw:{'evening':{'state':'fresh'}})
    monkeypatch.setattr(reliability,'snapshot',lambda *a:{'counts':counts,'incidents':[],'pending_operation_details':details})
    store.save_setting('price-sync:last-ack',{'observations':30,'at':now.isoformat()})
    outcome=reliability.scheduled_window_outcome(store,'evening',started,now)
    assert outcome['current_window_critical']==0 and outcome['price_delivery']['state']=='draining'
    details[0]['error']={'category':'server_error'}
    assert reliability.scheduled_window_outcome(store,'evening',started,now)['current_window_critical']>0
    details[0]['error']=None
    clock.advance(minutes=11)
    assert reliability.scheduled_window_outcome(store,'evening',started,clock())['price_delivery']['state']=='awaiting_recovery'


def prepare(store,boundary,settings,clock,monkeypatch):
    clock.value=dt.datetime(2026,11,24,7,tzinfo=dt.timezone.utc)
    cfg=replace(grant(settings),approval_id='rentcast-free-20261115-cycle-only',approval_expires_at='2026-12-14T10:00:00Z',page_size=500)
    query=unified_query(boundary)[0]
    rows=[from_area(boundary,'pittsburgh','head-'+str(i),propertyType='Townhouse') for i in range(500)]
    put_cache(store,query,0,rows,741,HEAD_AT)
    for row in rows:store.save_property(row['id'],row,'Rejected',['townhouse'],LIVE,1,HEAD_AT)
    head=dict(offset_before=0,offset_after=500,offset_requested=0,fetched_at=HEAD_AT,job_id=early.JOB,records=500,partition=UNIFIED)
    store.save_setting(early.HEAD,{'finished_at':HEAD_AT,'run_key':early.HEAD,'new_source_ids':[r['id'] for r in rows[:50]],
                                  'pages':[head],'partitions':{UNIFIED:{'query':query}}})
    store.save_setting('discovery_run:'+early.DAY+':morning',{'finished_at':'2026-11-24T06:10:00+00:00'})
    store.save_setting(PREFIX+UNIFIED,dict(job_id=early.JOB,offset=500,query_complete=False,query=query,
        seen_ids=dict(early._fingerprints(rows)),query_hash=early.QUERY_HASH,total=741))
    store.job(early.JOB,early.DAY)
    store.checkpoint(early.JOB,500,False,{'received':500,'last_listing_page':{'offset_requested':0}})
    for key,amount in ((cycle_key(cfg,clock()),15),(period(cfg,clock(),'listing'),12),(period(cfg,clock(),'rent'),3)):
        store.execute('INSERT INTO usage VALUES (?,?)',(key,amount))
    monkeypatch.setattr('rentalsourcing.review_feedback.refresh',lambda *a:{'state':'fresh'})
    remaining=rows[475:]+[from_area(boundary,'bethel-park','new-review'),from_area(boundary,'bellevue','new-condo',propertyType='Condo')]
    return cfg,query,remaining


@pytest.mark.parametrize('behavior',['fetch','cached','denied'])
def test_exact_continuation_requires_cache_check_and_is_never_retransmitted(store,official_boundary,settings,clock,monkeypatch,behavior):
    cfg,query,rows=prepare(store,official_boundary,settings,clock,monkeypatch)
    receiver=Receiver()
    calls=[]
    if behavior=='cached':put_cache(store,query,475,rows,502,clock().isoformat())
    class Provider:
        def open(self,request,timeout):
            url=urlsplit(request.full_url)
            assert url.path=='/v1/listings/sale' and parse_qs(url.query)['offset']==['475']
            calls.append(url.path)
            if behavior=='denied':raise urllib.error.URLError('Tunnel connection failed: 403 Forbidden')
            return Response(rows,502)
    def execute():
        return early.run(store,official_boundary,cfg,clock,CONFIG,provider=Provider(),destination=receiver)
    if behavior=='denied':
        with pytest.raises(urllib.error.URLError):execute()
        with pytest.raises(BudgetExceeded):execute()
        assert len(calls)==1 and get(store,early.KEY)['failure']['stage']=='proxy_connect'
        assert get(store,PREFIX+UNIFIED)['offset']==500
        assert store.count_usage(cycle_key(cfg,clock()))==16
        return
    receipt=execute()
    assert len(calls)==int(behavior=='fetch') and receipt['matching_cache_at_start']==(behavior=='cached')
    assert receipt['state']=='complete' and receipt['new_identities']==2
    assert receipt['page']['duplicates']==25 and receipt['page']['query_complete']
    assert receiver.deliveries==['new-review'] and receipt['acknowledgments']['inserted']==1
    assert execute()==receipt and len(calls)==int(behavior=='fetch')
    assert len(get(store,early.HEAD)['pages'])==1
    assert store.count_usage(cycle_key(cfg,clock()))==15+len(calls)
    assert get(store,'discovery_run:'+early.DAY+':morning')=={'finished_at':'2026-11-24T06:10:00+00:00'}
    if behavior=='fetch':
        evening=clock().replace(hour=18)
        assert listing_plan(store,cfg,evening)['available']==0
        assert store.count_usage('request_scope:multi-discovery:'+early.DAY+':evening')==0


def test_early_approval_cannot_fund_another_endpoint_or_renew(store,official_boundary,settings,clock,monkeypatch):
    cfg,_,_=prepare(store,official_boundary,settings,clock,monkeypatch)
    store.save_setting(early.KEY,{'state':'prepared','offset_requested':475})
    for purpose,when in [('rent',clock()),('listing',clock()+dt.timedelta(days=1)),('listing',clock().replace(hour=18))]:
        with pytest.raises(BudgetExceeded):store.reserve_api(cfg,when,purpose=purpose,scope=(early.SCOPE,1))
    assert store.count_usage(cycle_key(cfg,clock()))==15


def test_active_operation_counts_use_state_index_and_leave_historical_receipts(store):
    from rentalsourcing.sync_runtime import operation_counts
    for index, state in enumerate(['success']*30+['reconciled','dead_letter','pending','pending']):
        store.save_setting('sync_operation:test:'+str(index),{'state':state,'receipt':{'retained':True}})
    assert operation_counts(store)=={'dead_letter':1,'pending':2}
    assert get(store,'sync_operation:test:0')['receipt']['retained']


def test_price_evidence_counts_original_observations_once_and_excludes_later_baselines(store,clock):
    from rentalsourcing.price_sync import enqueue, observation_key
    from rentalsourcing.page_recovery import page_price_receipts
    rows=[listing('observed'),listing('not-tracked')]
    query={'limit':500}
    stamp=clock().isoformat()
    put_cache(store,query,0,rows,2,stamp)
    for row in rows:
        enqueue(store,row,stamp,stamp,observation_id='source:'+row['id'])
    enqueue(store,rows[0],stamp,stamp,observation_id='tracked-baseline:observed',historical=True)
    first=observation_key(rows[0],stamp)
    store.save_setting(first,{**get(store,first),'synced_at':stamp,'receiver_disposition':'acknowledged'})
    clock.advance(minutes=10)
    second=observation_key(rows[1],stamp)
    store.save_setting(second,{**get(store,second),'synced_at':clock().isoformat(),'receiver_disposition':'untracked'})
    report=page_price_receipts(store,{'partitions':{UNIFIED:{'query':query}}},
        {'partition':UNIFIED,'offset_requested':0,'fetched_at':stamp},stamp)
    assert report['price_observation_count']==2
    assert report['price_observations_with_recorded_ack_after_slot_close']==1
    assert {r['disposition']:r['count'] for r in report['price_receipts']}=={'acknowledged':1,'untracked':1}


def test_changed_inventory_reports_unexpected_overlap_after_final_offset():
    from rentalsourcing.partition_discovery import _record_page, traversal_coverage
    state={'seen_ids':{str(i):'fingerprint' for i in range(500)},'query':{'limit':500},'total':741}
    page={'offset_before':500,'offset_requested':475,'offset_after':750,'requests':1,'query_complete':True,
          'fetched_at':HEAD_AT,'records':275,'area_observations':{}}
    _record_page(state,page,[(str(i),'fingerprint') for i in range(454,729)],750)
    assert page['query_complete'] and page['continuity_warning'] and page['duplicates']==46
    assert set(page['continuity_reasons'])=={'provider_total_changed','unexpected_page_overlap','distinct_identities_differ_from_latest_total'}
    assert page['distinct_identities_observed']==729
    old_state={**state,'last_page':{**page,'continuity_warning':False},'continuity_warning':False,'continuity_reasons':[]}
    assert traversal_coverage(old_state)['continuity_warning']
