import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import listing
from test_partition_discovery import AreaProvider,allocated
from rentalsourcing.partition_discovery import MAIN,PREFIX,run as scheduled,get
from rentalsourcing.approved_catchup import DAY,SLOT,APPROVAL,KEY,RUN,LIMITS,activate,remaining,run
from rentalsourcing.storage import Store,BudgetExceeded


@pytest.fixture
def approved(store,boundary,allocated,clock,monkeypatch):
    monkeypatch.setattr('time.sleep',lambda _:None)
    provider=AreaProvider(main=3)
    old,_=scheduled(store,boundary,allocated,'2026-11-13','evening',clock=clock,opener=provider)
    assert old['requests_consumed']==4 and store.budget_health(allocated,clock())['used']==45
    clock.advance(hours=12,minutes=20)
    store.save_setting('multi-discovery:'+DAY+':morning',{'finished_at':clock().isoformat(),'requests_consumed':0,'pages':[]})
    monkeypatch.setenv('CATCH_UP_DISCOVERY_APPROVAL_ID',APPROVAL)
    monkeypatch.setenv('CATCH_UP_DISCOVERY_DAY',DAY)
    assert activate(store,allocated,clock())['limit']==4
    return allocated


def test_exact_partition_caps_use_corrected_scheduler_and_preserve_morning(store,boundary,approved,clock):
    before=get(store,'multi-discovery:'+DAY+':morning')
    old_jobs={slug:get(store,PREFIX+slug)['job_id'] for slug in LIMITS}
    provider=AreaProvider(main=8,bethel=4)
    result=run(store,boundary,approved,DAY,clock=clock,opener=provider,deliver=False)
    assert provider.calls==[(MAIN,0),(MAIN,2),('Bethel Park',0),('Pleasant Hills',0)]
    assert result['requests']==4 and result['state']=='partial'
    assert result['stats']['version']=='multi-partition-v2'
    assert result['stats']['received_this_run']==6
    assert result['outgoing_emails']==result['paid_tax_or_rent_requests']==0
    assert store.budget_health(approved,clock())['used']==49
    assert store.budget_health(approved,clock())['remaining']==1
    assert get(store,'multi-discovery:'+DAY+':morning')==before
    assert all(store.execute('SELECT collection_done FROM scan_jobs WHERE id=?',(job,)).fetchone()[0] for job in old_jobs.values())
    assert get(store,PREFIX+MAIN)['offset']==4
    again=run(store,boundary,approved,DAY,clock=clock,opener=provider,deliver=False)
    assert again['already_executed'] and len(provider.calls)==4
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0


def test_stop_early_when_queries_complete(store,boundary,approved,clock):
    result=run(store,boundary,approved,DAY,clock=clock,opener=AreaProvider(main=1),deliver=False)
    assert result['requests']==3 and result['state']=='complete'
    assert store.budget_health(approved,clock())['remaining']==2


def test_expired_or_changed_capacity_stops_before_any_request(store,boundary,approved,clock):
    provider=AreaProvider()
    store.execute('UPDATE usage SET used=46 WHERE period=?',('cycle:2026-10-15T00:00:00+00:00',))
    with pytest.raises(BudgetExceeded):run(store,boundary,approved,DAY,clock=clock,opener=provider)
    assert provider.calls==[]
    clock.value=dt.datetime(2026,11,14,10,tzinfo=dt.timezone.utc)
    assert remaining(store,approved,clock())==0
    assert activate(store,approved,clock())['state']=='expired'


def test_each_request_rechecks_cutoff(store,boundary,approved,clock):
    clock.value=dt.datetime(2026,11,14,9,59,59,tzinfo=dt.timezone.utc)
    provider=AreaProvider()
    original=provider.open
    def expires(request,timeout=None):
        value=original(request,timeout)
        clock.advance(seconds=2)
        return value
    provider.open=expires
    result=run(store,boundary,approved,DAY,clock=clock,opener=provider,deliver=False)
    assert result['requests']==1 and provider.calls==[(MAIN,0)]
    assert result['state']=='partial'


def test_no_global_or_partition_budget_can_be_spent_twice(store,approved,clock):
    if not store.pg:pytest.skip('Atomic concurrent PostgreSQL sessions')
    def reserve(slug):
        db=Store(store.test_url,initialize=False)
        try:
            try:
                db.reserve_api(approved,clock(),purpose='listing',scope=(RUN+':'+slug,LIMITS[slug]))
                return slug
            except BudgetExceeded:return None
        finally:db.close()
    with ThreadPoolExecutor(max_workers=8) as pool:result=list(pool.map(reserve,list(LIMITS)*6))
    assert result.count(MAIN)==2 and result.count('bethel-park')==result.count('pleasant-hills')==1
    assert store.count_usage('request_scope:'+RUN)==4
    assert store.budget_health(approved,clock())['used']==49


def test_other_jobs_and_enrichment_cannot_use_catchup_allowance(store,approved,clock):
    for purpose,scope in [('rent',(RUN+':'+MAIN,2)),('tax',(RUN+':'+MAIN,2)),('listing',(RUN,4)),('listing',None)]:
        with pytest.raises(BudgetExceeded):store.reserve_api(approved,clock(),purpose=purpose,scope=scope)
    assert store.count_usage('request_scope:'+RUN)==0


def test_changed_closed_morning_receipt_blocks_catchup(store,boundary,approved,clock):
    store.save_setting('multi-discovery:'+DAY+':morning',{'changed':True})
    with pytest.raises(BudgetExceeded):run(store,boundary,approved,DAY,clock=clock,opener=AreaProvider())
    assert store.count_usage('request_scope:'+RUN)==0


def test_real_processing_delivery_ack_and_retry_never_repeat_provider_or_email(store,boundary,approved,clock,monkeypatch):
    from conftest import Response
    monkeypatch.setattr('rentalsourcing.command_center.review_configuration',lambda:('https://penten.example.test/api/rentalsourcing/review-sync','s'*40,'fixture'))
    store.save_setting('penten_review:owner-rejected',{'status':'rejected'})
    source=AreaProvider(main=0,bethel=0,pleasant=0)
    source.rows[MAIN]=[listing('owner-rejected'),listing('new-pending'),listing('ineligible-townhouse',propertySubType='Townhouse')]
    class Receiver:
        def __init__(self):self.fail=True;self.cards=[]
        def open(self,request,timeout=None):
            if request.full_url.startswith('https://api.rentcast.io/'):return source.open(request,timeout)
            assert request.full_url=='https://penten.example.test/api/rentalsourcing/review-reports'
            if self.fail:raise TimeoutError('fixture temporary sync outage')
            body=json.loads(request.data);self.cards.extend(body['opportunities'])
            raw=json.dumps({'accepted':True,'report_id':body['report_id'],'counts':{'inserted':len(body['opportunities']),'updated':0,'rejected':0,'duplicate_cards':0}}).encode()
            r=Response({});r.read=lambda n:raw
            return r
    receiver=Receiver()
    first=run(store,boundary,approved,DAY,clock=clock,opener=receiver)
    assert first['state']=='pending_delivery' and first['requests']==4
    assert len(source.calls)==4
    receiver.fail=False
    # Model the durable retry becoming due; this does not create a provider grant.
    for name,raw in store.execute('SELECT name,payload FROM settings WHERE name LIKE ?',('sync_operation:inventory_sync:%',)).fetchall():
        row=json.loads(raw);row['next_attempt_at']=None;store.save_setting(name,row)
    second=run(store,boundary,approved,DAY,clock=clock,opener=receiver)
    assert second['requests_this_invocation']==0 and len(source.calls)==4
    assert second['delivery_totals']['new_source_listings']==3
    assert second['delivery_totals']['new_pending']==1
    assert second['penten_receipts']['inserted']==1
    assert [c['source_id'] for c in receiver.cards]==['new-pending']
    assert receiver.cards[0]['qualification']=='pending_verification'
    assert get(store,'penten_review:owner-rejected')['status']=='rejected'
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0
    assert store.budget_health(approved,clock())['used']==49
