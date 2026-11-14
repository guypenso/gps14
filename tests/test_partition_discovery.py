"""Real PostgreSQL/SQLite persistence; synthetic providers cannot reach the network."""
import datetime as dt
import json
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import Response, listing
from rentalsourcing.collector import LIVE
from rentalsourcing.discovery_allocation import ID, KEY, activate, allocation_scope, allowance, snapshot
from rentalsourcing.partition_discovery import MAIN, PREFIX, get, preflight, run
from rentalsourcing.storage import Store, BudgetExceeded


class AreaProvider:
    def __init__(self, main=14, bethel=1, pleasant=0):
        self.rows = {MAIN: [listing('main-'+str(i)) for i in range(main)],
                     'Bethel Park': [listing('bethel-'+str(i)) for i in range(bethel)],
                     'Pleasant Hills': [listing('pleasant-'+str(i)) for i in range(pleasant)]}
        self.calls = []
        self.fail_main = None
    def open(self, request, timeout=None):
        parts = urlsplit(request.full_url)
        assert parts.hostname == 'api.rentcast.io' and parts.path == '/v1/listings/sale'
        query = parse_qs(parts.query)
        area = query.get('city', [MAIN])[0]
        if area == 'Pittsburgh': area = MAIN  # Simple test boundary lacks search_parameters.
        offset, limit = int(query['offset'][0]), int(query['limit'][0])
        self.calls.append((area, offset))
        if area == MAIN and self.fail_main:
            raise self.fail_main
        rows = self.rows[area]
        return Response(rows[offset:offset+limit], len(rows))


@pytest.fixture
def allocated(store, settings, clock, monkeypatch):
    clock.value = dt.datetime(2026, 11, 13, 16, tzinfo=dt.timezone.utc)
    s = replace(settings, approval_mode='monthly_budget', approval_id='rentcast-free-supplement-20261113',
                approved_requests=27, per_run=5, per_day=6, per_month=50, billing_day=15,
                billing_anchor='2026-10-15T00:00:00+00:00', billing_baseline=40,
                monthly_budget_requests=50, provider_included_requests=50, supplemental_requests=10,
                approval_expires_at='2026-11-14T10:00:00Z')
    for key, n in [('cycle:2026-10-15T00:00:00+00:00',41), ('supplemental:2026-10-15T00:00:00+00:00',1), ('day:2026-11-13',1)]:
        store.execute('INSERT INTO usage VALUES (?,?)', (key,n))
    monkeypatch.setenv('DISCOVERY_ALLOCATION_ID', ID)
    assert activate(store,s,clock())['additional_limit'] == 9
    clock.value = dt.datetime(2026, 11, 13, 18, tzinfo=dt.timezone.utc)
    return s


def test_five_evening_four_morning_with_independent_cursors(store,boundary,allocated,clock):
    provider = AreaProvider()
    result, client = run(store,boundary,allocated,'2026-11-13','evening',clock=clock,opener=provider)
    assert provider.calls == [(MAIN,0),('Bethel Park',0),('Pleasant Hills',0),(MAIN,2),(MAIN,4)]
    assert client.count == result['requests'] == 5
    assert result['received_this_run'] == 7
    assert get(store,PREFIX+MAIN)['offset'] == 6
    assert get(store,PREFIX+'bethel-park')['offset'] == 1
    assert get(store,PREFIX+'pleasant-hills')['query_complete'] is True
    assert store.budget_health(allocated,clock())['remaining'] == 4
    again, duplicate = run(store,boundary,allocated,'2026-11-13','evening',clock=clock,opener=provider)
    assert again['already_executed'] and duplicate.count == 0 and len(provider.calls) == 5
    clock.advance(hours=12)
    morning, _ = run(store,boundary,allocated,'2026-11-14','morning',clock=clock,opener=provider)
    assert provider.calls[5:] == [(MAIN,6),(MAIN,8),(MAIN,10),(MAIN,12)]
    assert morning['requests'] == 4 and not morning['discovery_complete']
    assert get(store,PREFIX+MAIN)['query_complete'] is True
    assert morning['partitions']['bethel-park']['state']=='budget_not_reached'
    assert store.budget_health(allocated,clock())['used'] == 50
    assert store.count_usage('allocation:'+ID) == 9
    assert store.execute('SELECT COUNT(*) FROM properties').fetchone()[0] == 15
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0


def test_completed_evening_queries_refresh_on_next_scheduled_slot(store,boundary,allocated,clock):
    clock.advance(minutes=1)
    provider = AreaProvider(main=1)
    first,_ = run(store,boundary,allocated,'2026-11-13','evening',clock=clock,opener=provider)
    assert first['requests'] == 3 and first['discovery_complete']
    old_job=get(store,PREFIX+MAIN)['job_id']
    provider.rows[MAIN].append(listing('overnight-new'))
    clock.advance(hours=11,minutes=59)
    second,_ = run(store,boundary,allocated,'2026-11-14','morning',clock=clock,opener=provider)
    assert second['requests_consumed'] == 3 and second['discovery_complete']
    assert len(provider.calls) == 6 and second['new_source_ids']==['overnight-new']
    assert second['fresh_request_made'] is True
    state=get(store,PREFIX+MAIN)
    assert state['job_id']!=old_job and state['previous_generation']['job_id']==old_job
    assert store.execute('SELECT offset_value FROM scan_jobs WHERE id=?',(old_job,)).fetchone()[0]==1


def test_unscoped_jobs_tax_rent_wrong_day_and_legacy_cannot_spend(store,allocated,clock):
    scope = allocation_scope('2026-11-13','evening')
    for purpose, trial_scope in [('tax',scope),('rent',scope),('listing',None),('listing',('catchup-discovery:2026-11-13',1))]:
        with pytest.raises(BudgetExceeded): store.reserve_api(allocated,clock(),purpose=purpose,scope=trial_scope)
    with pytest.raises(BudgetExceeded): store.reserve(100,100,clock())
    clock.advance(hours=-1)
    with pytest.raises(BudgetExceeded): store.reserve_api(allocated,clock(),purpose='listing',scope=scope)
    assert store.count_usage('allocation:'+ID) == 0
    assert store.budget_health(allocated,clock())['used'] == 41


def test_phase_counters_do_not_reset_on_deploy_and_cutoff_does_not_renew(store,allocated,clock):
    scope = allocation_scope('2026-11-13','evening')
    for _ in range(5): store.reserve_api(allocated,clock(),purpose='listing',scope=scope)
    with pytest.raises(BudgetExceeded): store.reserve_api(allocated,clock(),purpose='listing',scope=scope)
    assert activate(store,allocated,clock())['state'] == 'already_active'
    assert allowance(store,allocated,clock(),'2026-11-13','evening') == 0
    clock.value = dt.datetime(2026,11,14,10,tzinfo=dt.timezone.utc)
    assert snapshot(store,clock())['remaining'] == 0
    with pytest.raises(BudgetExceeded): store.reserve_api(allocated,clock(),purpose='listing',scope=scope)
    clock.advance(days=32)
    with pytest.raises(BudgetExceeded): store.reserve_api(allocated,clock(),purpose='listing',scope=scope)
    assert activate(store,allocated,clock())['state']=='expired'
    new_approval=replace(allocated,approval_id='separately-approved-future-cycle',billing_anchor='2026-12-15T00:00:00+00:00',
                         supplemental_requests=0,approval_expires_at='',billing_baseline=0)
    clock.value=dt.datetime(2026,12,15,6,tzinfo=dt.timezone.utc)
    assert allowance(store,new_approval,clock(),'2026-12-15','morning') is None


def test_concurrent_workers_cannot_take_tomorrows_four(store,allocated,clock):
    if not store.pg: pytest.skip('PostgreSQL concurrent transaction enforcement')
    def reserve(_):
        db = Store(store.test_url,initialize=False)
        try:
            try:
                db.reserve_api(allocated,clock(),purpose='listing',scope=allocation_scope('2026-11-13','evening'))
                return True
            except BudgetExceeded: return False
        finally: db.close()
    with ThreadPoolExecutor(max_workers=8) as pool: assert sum(pool.map(reserve,range(12))) == 5
    assert store.budget_health(allocated,clock())['remaining'] == 4


def test_source_error_is_reported_without_starving_other_areas(store,boundary,allocated,clock):
    provider = AreaProvider(main=3)
    provider.fail_main = urllib.error.HTTPError('https://fixture.test',403,'Forbidden',{},None)
    report,_ = run(store,boundary,allocated,'2026-11-13','evening',clock=clock,opener=provider)
    assert report['partitions'][MAIN]['state'] == 'source_blocked'
    assert ('Bethel Park',0) in provider.calls and ('Pleasant Hills',0) in provider.calls
    assert report['discovery_partial'] and report['requests'] == 3
    clock.advance(hours=12)
    run(store,boundary,allocated,'2026-11-14','morning',clock=clock,opener=provider)
    assert provider.calls.count((MAIN,0)) == 1


def test_duplicate_changing_rows_update_one_identity_and_preserve_owner(store,boundary,allocated,clock):
    provider = AreaProvider(main=0,bethel=0)
    provider.rows[MAIN] = [listing('same',price=120000),listing('other'),listing('same',price=110000)]
    store.save_setting('penten_review:same',{'status':'rejected'})
    report,_ = run(store,boundary,allocated,'2026-11-13','evening',clock=clock,opener=provider)
    assert store.execute('SELECT COUNT(*) FROM properties').fetchone()[0] == 2
    assert store.get_property('same')['payload']['price'] == 110000
    assert get(store,'penten_review:same')['status'] == 'rejected'
    state = get(store,PREFIX+MAIN)
    assert state['duplicate_observations'] == state['changed_observations'] == 1
    assert len(report['new_source_ids']) == 2


def test_interruption_after_ingestion_recovers_checkpoint_without_repeating_page(store,boundary,allocated,clock,monkeypatch):
    provider = AreaProvider(main=8)
    save = store.save_setting
    fired = False
    def crash(name,value):
        nonlocal fired
        if name == PREFIX+MAIN and value.get('offset',0)>0 and not fired:
            fired = True
            raise KeyboardInterrupt('simulate process death after committed ingestion')
        return save(name,value)
    monkeypatch.setattr(store,'save_setting',crash)
    with pytest.raises(KeyboardInterrupt): run(store,boundary,allocated,'2026-11-13','evening',clock=clock,opener=provider)
    monkeypatch.setattr(store,'save_setting',save)
    report,_ = run(store,boundary,allocated,'2026-11-13','evening',clock=clock,opener=provider)
    assert provider.calls.count((MAIN,0)) == 1
    assert report['requests'] == 5
    assert any(page.get('checkpoint_recovered') for page in report['pages'])
    assert len(report['new_source_ids']) == store.execute('SELECT COUNT(*) FROM properties').fetchone()[0]


def test_legacy_offset_adopted_and_completed_other_scope_does_not_reset_it(store,boundary,allocated,clock):
    job = 'coverage:investment-20261111-two-bedroom:pittsburgh-bellevue:2026-11-12:morning'
    store.job(job,'2026-11-12');store.checkpoint(job,4,False,{},'partial')
    before = preflight(store,boundary,allocated,clock())
    assert [p['limit'] for p in before['plans']] == [5,4]
    assert get(store,PREFIX+MAIN)['job_id'] == job and get(store,PREFIX+MAIN)['offset'] == 4
    provider = AreaProvider(main=8)
    report,_ = run(store,boundary,allocated,'2026-11-13','evening',clock=clock,opener=provider)
    assert provider.calls[0] == (MAIN,4)
    assert report['partitions'][MAIN]['geographic_completeness'] == 'not_verified'


def test_small_recurring_budget_rotates_areas_across_slots(store,boundary,settings,clock):
    settings = replace(settings,per_run=1)
    provider = AreaProvider(main=8,bethel=8,pleasant=8)
    for i,slot in enumerate(('morning','evening','morning')):
        run(store,boundary,settings,clock().date().isoformat(),slot,clock=clock,opener=provider)
        clock.advance(hours=12)
    assert [p[0] for p in provider.calls] == [MAIN,'Bethel Park','Pleasant Hills']


def test_budget_exhaustion_on_day_counter_is_durable_and_does_not_skip_states(store,boundary,allocated,clock):
    store.execute('UPDATE usage SET used=6 WHERE period=?',('day:2026-11-13',))
    provider=AreaProvider()
    report,_=run(store,boundary,allocated,'2026-11-13','evening',clock=clock,opener=provider)
    assert provider.calls==[] and report['requests_consumed']==0
    assert all(p['state']=='budget_blocked' for p in report['partitions'].values())
    assert store.budget_health(allocated,clock())['used']==41


def test_transient_failure_resumes_same_cursor_in_next_slot(store,boundary,allocated,clock):
    provider=AreaProvider(main=3)
    provider.fail_main=TimeoutError('fixture timeout')
    first,_=run(store,boundary,allocated,'2026-11-13','evening',clock=clock,opener=provider)
    assert first['partitions'][MAIN]['state']=='retry_scheduled'
    clock.advance(hours=12)
    provider.fail_main=None
    second,_=run(store,boundary,allocated,'2026-11-14','morning',clock=clock,opener=provider)
    assert second['discovery_complete'] and provider.calls.count((MAIN,0))==2
    assert second['requests_consumed']==4
    assert store.budget_health(allocated,clock())['used']==48


def test_completed_morning_receipt_is_not_replayed_by_deployment(store,boundary,allocated,clock):
    clock.advance(hours=12)
    key='multi-discovery:2026-11-14:morning'
    frozen={'version':'multi-partition-v1','run_key':key,'finished_at':clock().isoformat(),
            'requests_consumed':0,'pages':[],'discovery_state':'verification_only'}
    store.save_setting(key,frozen)
    provider=AreaProvider()
    result,client=run(store,boundary,allocated,'2026-11-14','morning',clock=clock,opener=provider)
    assert result['already_executed'] and client.count==0 and provider.calls==[]
    assert get(store,key)==frozen


def test_actual_incident_ledger_allows_morning_before_expiry(store,allocated,clock):
    scope=allocation_scope('2026-11-13','evening')
    for _ in range(4):store.reserve_api(allocated,clock(),purpose='listing',scope=scope)
    clock.value=dt.datetime(2026,11,14,6,2,tzinfo=dt.timezone.utc)
    assert store.budget_health(allocated,clock())['used']==45
    assert store.budget_health(allocated,clock())['remaining']==5
    assert allowance(store,allocated,clock(),'2026-11-14','morning')==4
    clock.value=dt.datetime(2026,11,14,10,tzinfo=dt.timezone.utc)
    assert allowance(store,allocated,clock(),'2026-11-14','morning')==0


def test_expired_budget_never_opens_a_new_cursor_generation(store,boundary,allocated,clock):
    provider=AreaProvider(main=1)
    run(store,boundary,allocated,'2026-11-13','evening',clock=clock,opener=provider)
    states={slug:get(store,PREFIX+slug) for slug in (MAIN,'bethel-park','pleasant-hills')}
    clock.value=dt.datetime(2026,11,14,10,tzinfo=dt.timezone.utc)
    report,client=run(store,boundary,allocated,'2026-11-14','morning',clock=clock,opener=provider)
    assert client.count==0 and report['discovery_partial'] and not report['fresh_request_made']
    assert report['discovery_state']=='skipped_budget_exhausted'
    assert all(get(store,PREFIX+slug)==state for slug,state in states.items())


def test_expired_allocation_does_not_crash_later_evening_slot(store,boundary,allocated,clock):
    """Reproduces the November 14 20:00 production TypeError."""
    clock.value=dt.datetime(2026,11,14,18,2,tzinfo=dt.timezone.utc)
    provider=AreaProvider()
    report,client=run(store,boundary,allocated,'2026-11-14','evening',clock=clock,opener=provider)
    assert client.count==0 and provider.calls==[]
    assert report['request_limit']==0
    assert report['discovery_state']=='skipped_budget_exhausted'
    assert report['discovery_partial'] and not report['fresh_request_made']
    assert all(p['state']=='budget_blocked' for p in report['partitions'].values())


def test_scheduler_exception_records_failure_and_continues_stored_processing(store,boundary,allocated,clock,monkeypatch):
    from rentalsourcing.cloud import daily_discovery
    monkeypatch.delenv('ENABLE_REVIEW_INVENTORY_SYNC',raising=False)
    monkeypatch.delenv('ENABLE_COMMAND_CENTER_REVIEW_FEEDBACK',raising=False)
    clock.value=dt.datetime(2026,11,14,18,2,tzinfo=dt.timezone.utc)
    def broken(*args,**kwargs):raise TypeError('fixture scheduler defect')
    monkeypatch.setattr('rentalsourcing.partition_discovery.run',broken)
    result=daily_discovery(store,boundary,replace(allocated,email_enabled=False),'2026-11-14',slot='evening',clock=clock)
    assert result['scan']=='failed' and result['requests']==0
    receipt=get(store,'multi-discovery:2026-11-14:evening')
    assert receipt['discovery_state']=='failed' and receipt['requests_consumed']==0
    assert receipt['error_type']=='TypeError'
    assert store.execute('SELECT COUNT(*) FROM usage WHERE period LIKE ?',('cycle:%',)).fetchone()[0]==1


def test_cli_evidence_is_visible_when_host_logging_is_warning_only():
    import subprocess,sys
    script="""import logging
from rentalsourcing.telemetry import configure_cli,event
logging.basicConfig(level=logging.WARNING)
configure_cli()
configure_cli()
event('fixture_discovery_receipt',requests=0)
"""
    result=subprocess.run([sys.executable,'-c',script],text=True,capture_output=True,check=True)
    lines=result.stdout.strip().splitlines()
    assert len(lines)==1 and json.loads(lines[0])['event']=='fixture_discovery_receipt'


def test_morning_outbox_never_suppresses_discovery_or_creates_second_digest(store,boundary,allocated,clock,monkeypatch):
    from rentalsourcing.cloud import daily_discovery
    from rentalsourcing.digest import Digest
    monkeypatch.delenv('ENABLE_REVIEW_INVENTORY_SYNC',raising=False)
    monkeypatch.delenv('ENABLE_COMMAND_CENTER_REVIEW_FEEDBACK',raising=False)
    s=replace(allocated,email_enabled=False,email_approval_id='')
    clock.advance(hours=12)
    digest=Digest(store,boundary,s,clock)
    existing=digest.prepare_stored('2026-11-14')
    provider=AreaProvider(main=1,bethel=0,pleasant=0)
    result=daily_discovery(store,boundary,s,'2026-11-14',slot='morning',clock=clock,opener=provider)
    assert len(provider.calls)==3 and result['requests']==3
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==1
    assert store.execute('SELECT id FROM outbox').fetchone()[0]==existing


def test_account_correction_cannot_spend_mornings_reserved_four(store,allocated,clock):
    store.execute('UPDATE usage SET used=46 WHERE period=?',('cycle:2026-10-15T00:00:00+00:00',))
    assert allowance(store,allocated,clock(),'2026-11-13','evening')==0
    with pytest.raises(BudgetExceeded):
        store.reserve_api(allocated,clock(),purpose='listing',scope=allocation_scope('2026-11-13','evening'))
    assert store.count_usage('allocation:'+ID)==0


def test_zero_row_response_survives_interruption_without_repurchase(store,boundary,allocated,clock,monkeypatch):
    provider=AreaProvider(main=0,bethel=0,pleasant=0)
    save=store.save_setting
    fired=False
    def crash(name,value):
        nonlocal fired
        if name==PREFIX+MAIN and value.get('query_complete') and not fired:
            fired=True
            raise KeyboardInterrupt('simulate zero-row checkpoint interruption')
        return save(name,value)
    monkeypatch.setattr(store,'save_setting',crash)
    with pytest.raises(KeyboardInterrupt):run(store,boundary,allocated,'2026-11-13','evening',clock=clock,opener=provider)
    monkeypatch.setattr(store,'save_setting',save)
    result,_=run(store,boundary,allocated,'2026-11-13','evening',clock=clock,opener=provider)
    assert provider.calls.count((MAIN,0))==1 and result['requests_consumed']==3
    assert len(result['pages'])==3 and result['discovery_complete']


@pytest.mark.parametrize('month',[3,11])
@pytest.mark.parametrize('hour,minute,expected',[(7,49,False),(7,50,True),(8,19,True),(8,20,False),
    (19,49,False),(19,50,True),(20,19,True),(20,20,False)])
def test_watchdog_protects_both_discovery_windows_across_dst(month,hour,minute,expected):
    from rentalsourcing.schedule import ISRAEL,discovery_protected_window
    now=dt.datetime(2026,month,28,hour,minute,tzinfo=ISRAEL).astimezone(dt.timezone.utc)
    assert discovery_protected_window(now) is expected


def test_scheduled_scan_waits_for_watchdog_lock_without_duplicate_execution(store):
    if not store.pg:pytest.skip('PostgreSQL advisory-lock queue')
    import time
    other=Store(store.test_url,initialize=False)
    try:
        assert store.lock() and not other.lock()
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting=pool.submit(other.wait_for_scan_lock,2)
            deadline=time.monotonic()+1
            while time.monotonic()<deadline:
                queued=store.execute('SELECT COUNT(*) FROM pg_locks WHERE pid=? AND granted=false',(other.db.info.backend_pid,)).fetchone()[0]
                if queued:break
                time.sleep(.01)
            assert queued and not waiting.done()
            store.execute('SELECT pg_advisory_unlock(73541209)')
            assert waiting.result(timeout=3) is True
        assert not store.lock()
        assert other.execute('SHOW statement_timeout').fetchone()[0]=='30s'
    finally:other.close()
    assert store.lock()


def test_scheduled_lock_timeout_is_an_explicit_failure(store):
    if not store.pg:pytest.skip('PostgreSQL advisory-lock timeout')
    other=Store(store.test_url,initialize=False)
    try:
        assert store.lock()
        with pytest.raises(RuntimeError,match='no provider request'):
            other.wait_for_scan_lock(1)
        assert other.execute('SHOW statement_timeout').fetchone()[0]=='30s'
    finally:other.close()
