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
