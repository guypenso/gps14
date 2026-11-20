import datetime as dt,hashlib,json
from dataclasses import replace
import pytest
from conftest import listing,Response
from rentalsourcing.collector import LIVE
from rentalsourcing.sync_runtime import get,request_json,SyncFailure
from rentalsourcing.window_contract import outcome,free_source_status
from rentalsourcing.reliability_acceptance import ensure,snapshot as campaign
from rentalsourcing.investment_digest import select,render,freeze,acknowledge


def free_result(holds=1,failed=0):
    return {'state':'partial','email':{'results':[{'state':'success','failed':failed,'messages_processed':0,'unresolved_listing_blocks':holds}]}}


def test_expected_pacing_and_missing_tax_do_not_mean_failed_processing():
    stats={'discovery_state':'scheduled_pacing_no_listing_request','partial':True,'discovery_partial':True,'free_source':free_source_status(free_result())}
    r=outcome(stats)
    assert r['state']=='paced' and not r['exit_failure'] and not r['fresh_rentcast_discovery']
    assert r['evidence_holds']==1 and not r['market_coverage_verified']
    assert outcome({**stats,'listing_intake_state':'failed'})['state']=='partial'
    assert outcome(stats,critical=1)['exit_failure']


def test_fresh_requires_receipt_pages_not_only_green_exit():
    assert outcome({'discovery_state':'fresh'})['state']=='partial'
    data={'discovery_state':'fresh','fresh_request_made':True,'requests_consumed':1,'pages':[{}],'discovery_complete':False}
    assert outcome(data)['state']=='fresh' and not outcome(data)['provider_query_complete']
    assert outcome({**data,'property_quality':{'state':'pending_recovery'}})['state']=='partial'
    assert outcome({'discovery_state':'failed'})['state']=='failed'


def test_historical_ambiguity_does_not_force_five_minute_ingestion_loop(clock):
    from rentalsourcing.offmarket import next_source_refresh
    now=clock()
    assert dt.datetime.fromisoformat(next_source_refresh(now,free_result()))>now+dt.timedelta(minutes=5)
    assert dt.datetime.fromisoformat(next_source_refresh(now,free_result(failed=1)))==now+dt.timedelta(minutes=5)


def test_retries_escalate_after_twelve_consecutive_failures_preserving_payload(store,clock):
    class Down:
        def open(self,*a,**k):raise TimeoutError()
    cfg=('https://penten.example.test/api/rentalsourcing/review-reports','s'*40,'fixture')
    for _ in range(4):
        with pytest.raises(SyncFailure):request_json(store,'inventory_sync','same',cfg,b'{"stable":true}',now=clock(),opener=Down(),sleeper=lambda _:None)
        clock.advance(minutes=6)
    ops=[json.loads(r[0]) for r in store.execute("SELECT payload FROM settings WHERE name LIKE 'sync_operation:%'").fetchall()]
    assert len(ops)==1 and ops[0]['state']=='dead_letter' and ops[0]['consecutive_failures']==12
    assert ops[0]['digest']==hashlib.sha256(b'{"stable":true}').hexdigest()
    assert ops[0]['error']['category']=='retry_exhausted:timeout'
    with pytest.raises(SyncFailure):request_json(store,'inventory_sync','same',cfg,b'{"stable":true}',now=clock(),opener=Down())
    assert store.count_usage('all_time')==0


def setup_candidate(store,clock,*,discovered=None,status='new',canonical='real-canonical'):
    p=listing('fixture',formattedAddress='100 Fixture St, Pittsburgh, PA 15212',addressLine1='100 Fixture St')
    at=clock().isoformat();store.save_property('fixture',p,'Pending Verification',['tax_not_verified'],LIVE,1,at)
    store.save_setting('penten_review:fixture',{'source_id':'fixture','status':status,'canonical_property_id':canonical,'discovered_at':discovered or at})
    return p


def test_digest_does_not_repeat_old_pending_properties_or_claim_fresh_query(store,boundary,clock):
    setup_candidate(store,clock,discovered=(clock()-dt.timedelta(days=8)).isoformat())
    plan=select(store,boundary,clock());assert plan['updates']==[]
    subject,text,markup=render(clock().date().isoformat(),plan,{'discovery_state':'scheduled_pacing_no_listing_request'},'https://penten.example.test')
    assert 'No New Opportunities' in subject and 'No fresh RentCast query' in text
    assert 'tax_not_verified' not in text and 'Tax: Unknown' not in text and '<pre' not in markup
    assert '100 Fixture St' not in text


def test_digest_frozen_events_advance_only_after_confirmed_acceptance(store,boundary,clock):
    setup_candidate(store,clock);plan=select(store,boundary,clock());assert len(plan['updates'])==1
    store.execute('INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?)',('test','daily:fixture','pending','{}','[]',None,None,clock().isoformat()))
    freeze(store,'test',plan);assert not acknowledge(store,'test',clock())
    assert select(store,boundary,clock())['updates']==[]  # queued/uncertain stays frozen
    store.execute("UPDATE outbox SET state='uncertain' WHERE id='test'");assert select(store,boundary,clock())['updates']==[]
    store.execute("UPDATE outbox SET state='sent',provider_id='accepted' WHERE id='test'");assert acknowledge(store,'test',clock())
    assert select(store,boundary,clock())['updates']==[]
    assert store.get_property('fixture')['state']=='Pending Verification'


def test_new_source_alias_never_becomes_a_new_canonical_opportunity(store,boundary,clock):
    p=setup_candidate(store,clock,discovered=(clock()-dt.timedelta(days=8)).isoformat())
    store.save_property('new-alias',{**p,'id':'new-alias'},'Pending Verification',[],LIVE,1,clock().isoformat())
    store.save_setting('penten_review:new-alias',{'source_id':'new-alias','status':'new','canonical_property_id':'real-canonical','discovered_at':(clock()-dt.timedelta(days=8)).isoformat()})
    assert select(store,boundary,clock())['updates']==[]


def test_signed_canonical_price_trigger_is_reported_once_and_linked_exactly(store,boundary,clock):
    setup_candidate(store,clock,discovered=(clock()-dt.timedelta(days=8)).isoformat())
    review=get(store,'penten_review:fixture');review['conditional_watch']={'state':'triggered','cycle_id':'one','reactivated_at':clock().isoformat(),'last_price_cents':6000000,'target_cents':6000000}
    store.save_setting('penten_review:fixture',review)
    plan=select(store,boundary,clock());assert [r['kind'] for r in plan['updates']]==['reactivated']
    subject,text,markup=render(clock().date().isoformat(),plan,{},'https://penten.example.test')
    assert '1 New Opportunities' in subject and 'opportunity=real-canonical' in text and '$60,000' in text and '.00' not in text
    assert 'Tax: Unknown' not in markup


def test_rejected_or_active_conditional_watch_is_never_advertised(store,boundary,clock):
    setup_candidate(store,clock,status='rejected');assert not select(store,boundary,clock())['updates']
    review=get(store,'penten_review:fixture');review.update(status='new',conditional_watch={'active':True})
    store.save_setting('penten_review:fixture',review);assert not select(store,boundary,clock())['updates']


def test_seven_day_campaign_only_counts_actual_future_scheduled_windows(store,clock):
    clock.value=dt.datetime(2026,11,20,13,tzinfo=dt.timezone.utc)
    ensure(store,clock());first=campaign(store,clock())
    assert first['expected_windows']==14 and first['elapsed_windows']==0 and first['passed_windows']==0
    start=get(store,'reliability_acceptance:20261120')['activated_at'];clock.advance(hours=2)
    ensure(store,clock());assert get(store,'reliability_acceptance:20261120')['activated_at']==start
    clock.advance(hours=4);s=campaign(store,clock());assert s['elapsed_windows']==1 and s['missing_receipts']==1
    assert s['state']=='needs_attention' and not s['future_runs_verified']


def test_adaptive_coverage_prioritizes_stale_main_without_starving_other_areas(clock):
    from rentalsourcing.coverage_strategy import rank
    order=('pittsburgh-bellevue','bethel-park','pleasant-hills')
    old={'last_success_at':(clock()-dt.timedelta(days=4)).isoformat(),'selection_pages':10}
    recent={'last_success_at':clock().isoformat(),'selection_pages':0}
    assert rank(order[0],old,clock(),order)<rank(order[1],recent,clock(),order)
    assert rank(order[2],old,clock(),order)<rank(order[0],recent,clock(),order)


def test_explicit_empty_suppression_creates_no_unsent_sync_or_email(store,boundary,settings,clock,monkeypatch):
    from rentalsourcing.digest import Digest
    monkeypatch.setattr('rentalsourcing.command_center.review_configuration',lambda:('https://penten.example.test/api/rentalsourcing/review-sync','s'*40,'fixture'))
    monkeypatch.setenv('DAILY_DIGEST_EMPTY_POLICY','suppress')
    d=Digest(store,boundary,settings,clock)
    out=d.prepare('slot',clock().date().isoformat(),{'run_key':'multi-discovery:fixture','discovery_state':'scheduled_pacing_no_listing_request'})
    assert store.execute('SELECT state FROM outbox WHERE id=?',(out,)).fetchone()[0]=='suppressed'
    assert store.execute('SELECT COUNT(*) FROM command_center_reports').fetchone()[0]==0
    assert d.send(out)=='suppressed'
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
