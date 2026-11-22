import datetime as dt

from rentalsourcing.reliability import audit_scheduled_slots
from rentalsourcing.sync_runtime import get


def test_watchdog_distinguishes_missing_fresh_and_budget_blocked_slots(store):
    morning=dt.datetime(2026,11,15,6,41,tzinfo=dt.timezone.utc)
    first=audit_scheduled_slots(store,morning)
    assert first['morning']['state']=='missing' and first['morning']['provider_calls']==0
    assert 'evening' not in first
    store.save_setting('discovery_run:2026-11-15:morning',{'finished_at':morning.isoformat()})
    store.save_setting('multi-discovery:2026-11-15:morning',{
        'finished_at':morning.isoformat(),'discovery_state':'fresh',
        'requests_consumed':1,'pages':[{'records':3,'requests':1}],
        'received_this_run':3,'delivery_totals':{'synced':1},
        'penten_receipts':{'inserted':1,'updated':0,'rejected':0}})
    fresh=audit_scheduled_slots(store,morning)['morning']
    assert fresh['state']=='fresh' and fresh['penten_receipts']['inserted']==1
    evening=dt.datetime(2026,11,15,18,41,tzinfo=dt.timezone.utc)
    store.save_setting('discovery_run:2026-11-15:evening',{'finished_at':evening.isoformat()})
    store.save_setting('multi-discovery:2026-11-15:evening',{
        'finished_at':evening.isoformat(),'discovery_state':'skipped_budget_exhausted',
        'requests_consumed':0,'pages':[],'received_this_run':0,
        'delivery_totals':{'synced':0},'penten_receipts':{}})
    checked=audit_scheduled_slots(store,evening)
    assert checked['morning']['state']=='fresh' and checked['evening']['state']=='budget_blocked'
    assert get(store,'scheduled_slot_audit:2026-11-15:evening')['reason']=='approved_provider_budget_unavailable'
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0


def test_watchdog_rejects_fresh_scan_with_missing_penten_acknowledgment(store):
    now=dt.datetime(2026,11,15,6,41,tzinfo=dt.timezone.utc)
    store.save_setting('discovery_run:2026-11-15:morning',{'finished_at':now.isoformat()})
    store.save_setting('multi-discovery:2026-11-15:morning',{
        'finished_at':now.isoformat(),'discovery_state':'fresh','requests_consumed':1,
        'pages':[{'records':1,'requests':1}],'received_this_run':1,
        'delivery_totals':{'synced':1},'penten_receipts':{}})
    assert audit_scheduled_slots(store,now)['morning']['state']=='delivery_unacknowledged'


def test_watchdog_rejects_new_candidates_without_delivery(store):
    now=dt.datetime(2026,11,15,6,41,tzinfo=dt.timezone.utc)
    store.save_setting('discovery_run:2026-11-15:morning',{'finished_at':now.isoformat()})
    store.save_setting('multi-discovery:2026-11-15:morning',{
        'finished_at':now.isoformat(),'discovery_state':'fresh','requests_consumed':1,
        'pages':[{'records':1,'requests':1}],'received_this_run':1,
        'delivery_totals':{'new_pending':1,'new_qualified':0,'synced':0},
        'penten_receipts':{}})
    checked=audit_scheduled_slots(store,now)['morning']
    assert checked['reason']=='new_opportunities_not_delivered'


def test_paced_slot_with_failed_mailbox_cannot_pass_and_later_proof_is_separate(store):
    from rentalsourcing.window_contract import free_source_status, outcome
    now=dt.datetime(2026,11,22,18,45,tzinfo=dt.timezone.utc)
    start=now-dt.timedelta(minutes=42)
    window='2026-11-22:evening'
    failure=free_source_status({'state':'partial','email':{'authorized_accounts':1,'results':[{'state':'failed'}]}})
    result={'discovery_state':'scheduled_pacing_no_listing_request','free_source':failure}
    assert outcome(result)['exit_failure']
    store.save_setting('discovery_run:'+window,{'started_at':start.isoformat(),'finished_at':(start+dt.timedelta(minutes=2)).isoformat(),'state':'partial'})
    store.save_setting('multi-discovery:'+window,{'started_at':start.isoformat(),'finished_at':start.isoformat(),**result})
    store.save_setting('window_stages:'+window,result)
    before=audit_scheduled_slots(store,now)['evening']
    assert before['state']=='partial' and before['rentcast_state']=='paced' and before['source_retry_pending']
    store.save_setting('offmarket_scheduler',{'attempted_at':(now-dt.timedelta(minutes=4)).isoformat(),
        'completed_at':(now-dt.timedelta(minutes=3)).isoformat(),'state':'success','result':{
        'state':'success','email':{'authorized_accounts':1,'results':[{
            'state':'success','mailbox_checked':True,'messages_returned':0}]}}})
    after=audit_scheduled_slots(store,now)['evening']
    assert after['state']=='partial' and after['source_recovery']['source']['completed']
    assert not after['source_retry_pending']
    assert get(store,'discovery_run:'+window)['state']=='partial'
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0


def test_blocked_gmail_leaves_other_authorized_source_processing_available():
    from rentalsourcing.window_contract import free_source_status, outcome
    free=free_source_status({'state':'partial','email':{'authorized_accounts':1,'results':[{
        'state':'blocked','retryable':False,'error_type':'gmail_reconnect_required'}]},'public_inventory':{'state':'fetched'}})
    assert free['checked'] and free['public_checked'] and not free['mailbox_checked']
    assert not free['completed'] and not free['requires_retry']
    result=outcome({'discovery_state':'scheduled_pacing_no_listing_request','free_source':free})
    assert result['state']=='partial' and not result['exit_failure']


def test_read_only_inspection_exposes_persisted_slot_audit(store,boundary,settings,monkeypatch):
    from rentalsourcing.config import Settings
    from rentalsourcing.tax_intelligence import snapshot
    now=dt.datetime(2026,11,15,6,41,tzinfo=dt.timezone.utc)
    audit_scheduled_slots(store,now)
    monkeypatch.setattr(Settings,'from_env',classmethod(lambda cls:settings))
    result=snapshot(store,boundary,now)
    assert result['scheduled_slot_audits']['2026-11-15:morning']['state']=='missing'
    assert result['rental_estimates']['saved_estimates']==0
    assert result['realscout_intake']['properties']==0
    assert result['provider_calls']==0
