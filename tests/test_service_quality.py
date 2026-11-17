import datetime as dt
from rentalsourcing.service_quality import snapshot


def test_seven_day_report_uses_matching_receipts_and_never_counts_future_runs(store,clock):
    clock.value=dt.datetime(2026,11,17,16,tzinfo=dt.timezone.utc)
    began='2026-11-17T06:00:01+00:00';ended='2026-11-17T06:02:00+00:00'
    store.save_setting('discovery_run:2026-11-17:morning',{'state':'partial','started_at':began,'finished_at':ended})
    store.save_setting('multi-discovery:2026-11-17:morning',{'started_at':began,'finished_at':ended,'discovery_state':'fresh',
        'fresh_request_made':True,'requests_consumed':2,'pages':[{},{}],'received_this_run':179,
        'delivery_totals':{'new_source_listings':11,'new_pending':1,'synced':1},'penten_receipts':{'updated':1}})
    report=snapshot(store,clock())
    assert report['expected_elapsed_windows']==13 and report['execution_records']==1
    assert report['current_complete_receipt_pairs']==report['fresh_discovery_windows']==1
    assert report['workflow_complete_windows']==0 and report['unaccounted_delivery_windows']==12
    assert report['windows'][-1]['new_source_identities']==11
    assert report['windows'][-1]['delivery_accounted_for']
    assert not report['future_runs_verified'] and report['provider_calls']==0
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0


def test_stale_receipts_and_unacknowledged_delivery_do_not_pass(store,clock):
    clock.value=dt.datetime(2026,11,17,16,tzinfo=dt.timezone.utc)
    store.save_setting('discovery_run:2026-11-17:morning',{'state':'success','started_at':'2026-11-17T06:01:00Z','finished_at':'2026-11-17T06:02:00Z'})
    data={'started_at':'2026-11-17T06:00:00Z','finished_at':'2026-11-17T06:02:00Z','requests_consumed':1,'fresh_request_made':True,'pages':[{}]}
    store.save_setting('multi-discovery:2026-11-17:morning',data)
    assert snapshot(store,clock(),1)['current_complete_receipt_pairs']==0
    data.update(started_at='2026-11-17T06:01:01Z',delivery_totals={'new_pending':1,'synced':1})
    store.save_setting('multi-discovery:2026-11-17:morning',data)
    assert snapshot(store,clock(),1)['unaccounted_delivery_windows']==1
