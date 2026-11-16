import datetime as dt
from dataclasses import replace

from rentalsourcing.billing import cycle_key
from rentalsourcing.monthly_allocation import limits
from rentalsourcing.runtime_readiness import snapshot
from test_monthly_allocation import grant
import pytest


@pytest.mark.parametrize('case',['fresh','paced','budget_blocked','missing_delivery','stale_receipt'])
def test_current_scheduled_window_requires_its_own_receipt_without_erasing_historical_failure(store,clock,case):
    from rentalsourcing.reliability import scheduled_window_outcome
    from rentalsourcing.sync_runtime import incident,get
    clock.value=dt.datetime(2026,11,16,18,10,tzinfo=dt.timezone.utc)
    started=(clock()-dt.timedelta(minutes=5)).isoformat()
    historical_at=clock()-dt.timedelta(hours=12)
    incident(store,'discovery_slot_morning',{'category':'approved_provider_budget_unavailable','retryable':False},now=historical_at)
    run_key='discovery_run:2026-11-16:evening'
    store.save_setting('daily_health:2026-11-16',{'day':'2026-11-16','state':'success','stages':{}})
    store.save_setting(run_key,{'day':'2026-11-16','slot':'evening','started_at':started,'finished_at':clock().isoformat()})
    receipt={'started_at':started,'finished_at':clock().isoformat(),'discovery_state':'fresh','requests_consumed':1,
        'pages':[{'partition':'pittsburgh-bellevue','offset_before':0,'offset_after':1}],
        'received_this_run':1,'delivery_totals':{'new_pending':0,'new_qualified':0,'synced':0},'penten_receipts':{}}
    if case=='paced':receipt.update(discovery_state='scheduled_pacing_no_listing_request',requests_consumed=0,pages=[])
    if case=='budget_blocked':receipt.update(discovery_state='skipped_budget_exhausted',requests_consumed=0,pages=[])
    if case=='missing_delivery':receipt['delivery_totals']['new_pending']=1
    if case=='stale_receipt':receipt['started_at']=historical_at.isoformat()
    store.save_setting('multi-discovery:2026-11-16:evening',receipt)
    outcome=scheduled_window_outcome(store,'evening',started,clock())
    if case in {'fresh','paced'}:
        assert outcome['current_window_critical']==0 and outcome['current_window_state']==case
    else:assert outcome['current_window_critical']>0
    assert outcome['historical_open_incidents']==1
    old=get(store,'health_incident:discovery_slot_morning')
    assert old['state']=='open' and old['last_seen_at']==historical_at.isoformat()
    assert get(store,run_key)['started_at']==started
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0


@pytest.mark.parametrize('failure',[None,'provider_failed'])
def test_later_workflow_resolves_only_recovered_orchestration_incident(store,settings,boundary,clock,monkeypatch,failure):
    from rentalsourcing.cloud import daily_discovery
    from rentalsourcing.sync_runtime import incident,get
    clock.value=dt.datetime(2026,11,16,18,tzinfo=dt.timezone.utc)
    incident(store,'daily_workflow',{'category':'response_validation','retryable':False},now=clock()-dt.timedelta(days=2))
    historical={'day':'2026-11-14','slot':'evening','state':'failed','finished_at':'2026-11-14T18:05:00+00:00'}
    store.save_setting('discovery_run:2026-11-14:evening',historical)
    def completed(*a,**k):
        store.save_setting('multi-discovery:2026-11-16:evening',{'started_at':clock().isoformat(),'finished_at':clock().isoformat(),
            'discovery_state':'failed' if failure else 'fresh','requests_consumed':0 if failure else 1,'pages':[] if failure else [{'partition':'fixture'}]})
        return {'status':'processed_without_email','scan':'failed' if failure else 'fresh','error':failure}
    monkeypatch.setattr('rentalsourcing.cloud._daily_discovery',completed)
    monkeypatch.setattr('rentalsourcing.reliability.publish',lambda *a,**k:None)
    result=daily_discovery(store,boundary,grant(settings),'2026-11-16',slot='evening',clock=clock)
    assert get(store,'health_incident:daily_workflow')['state']==('open' if failure else 'resolved')
    assert get(store,'discovery_run:2026-11-14:evening')==historical
    assert result['unresolved_critical']>0 if failure else result['unresolved_critical']==0
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0


def test_verified_current_cycle_activation_reconciles_once_and_preserves_history(store,settings,clock):
    clock.value=dt.datetime(2026,11,16,9,tzinfo=dt.timezone.utc)
    store.execute('INSERT INTO usage VALUES (?,?)',('approval:old',27))
    s=replace(grant(settings),billing_baseline=3)
    assert sum(limits(s).values())==47
    assert all(limits(s)[kind]<=cap for kind,cap in {'listing':36,'rent':8,'targeted':4,'recovery':2}.items())
    receipt=store.reconcile_provider_baseline(s,clock())
    assert receipt['state']=='reconciled' and receipt['provider_confirmed_used']==3
    assert store.count_usage(cycle_key(s,clock()))==3
    store.reserve_api(s,clock(),purpose='rent')
    store.reconcile_provider_baseline(s,clock())
    assert store.count_usage(cycle_key(s,clock()))==4
    assert store.count_usage('approval:old')==27
    clock.value=dt.datetime(2026,12,15,6,tzinfo=dt.timezone.utc)
    assert store.reconcile_provider_baseline(s,clock())['state']=='inactive_current_cycle_grant'
    assert store.count_usage(cycle_key(s,clock()))==0


def test_retired_listing_allocation_does_not_block_a_new_verified_cycle(store,settings,boundary,clock):
    from rentalsourcing.discovery_allocation import KEY
    from rentalsourcing.partition_discovery import run,MAIN
    from test_partition_discovery import AreaProvider
    store.save_setting(KEY,{'approval_id':'rentcast-free-supplement-20261113',
        'cycle_key':'cycle:2026-10-15T00:00:00+00:00','additional_limit':9})
    store.execute('INSERT INTO usage VALUES (?,?)',('approval:rentalsourcing-remainder-20261026',27))
    clock.value=dt.datetime(2026,11,16,18,tzinfo=dt.timezone.utc)
    s=grant(settings);provider=AreaProvider(main=0,bethel=0,pleasant=0)
    receipt,_=run(store,boundary,s,'2026-11-16','evening',clock=clock,opener=provider)
    assert receipt['requests_consumed']>0 and receipt['fresh_request_made']
    assert provider.calls[0][0]==MAIN
    assert store.count_usage('approval:rentalsourcing-remainder-20261026')==27


def test_free_plan_ceiling_cannot_be_raised_by_conflicting_configuration(settings):
    s=replace(grant(settings),per_month=500,monthly_budget_requests=500,provider_included_requests=500)
    assert s.request_ceiling==50 and sum(limits(s).values())==50


def test_dashboard_snapshot_reports_the_effective_monthly_cap(store,settings,boundary,clock,monkeypatch):
    from rentalsourcing.config import Settings
    from rentalsourcing.tax_intelligence import snapshot as tax_snapshot
    clock.value=dt.datetime(2026,11,16,9,tzinfo=dt.timezone.utc)
    s=replace(grant(settings),enabled=False,approved_requests=0)
    monkeypatch.setattr(Settings,'from_env',classmethod(lambda cls:s))
    budget=tax_snapshot(store,boundary,clock())['api_budget']['dashboard_runtime']
    assert budget['approval_cap']==50 and budget['remaining']==50
    assert budget['approval_id']==s.approval_id and budget['approval_mode']=='monthly_budget'
    assert budget['approval_expires_at']==s.approval_expires_at and not budget['rentcast_enabled']


def test_dashboard_research_context_resumes_a_verified_grant_without_enabling_provider_calls(store,settings,monkeypatch):
    from rentalsourcing.config import Settings
    from rentalsourcing.billing import billing_window
    from rentalsourcing.listing_enrichment import research_context
    now=dt.datetime.now(dt.timezone.utc)
    anchor=now.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    s=replace(grant(settings),enabled=False,billing_anchor=anchor.isoformat())
    s=replace(s,approval_expires_at=billing_window(s,now)[1].isoformat(),provider_cycle_verified_at=now.isoformat())
    monkeypatch.setattr(Settings,'from_env',classmethod(lambda cls:s))
    monkeypatch.setenv('ENABLE_RENT_ESTIMATES','true')
    store.save_setting('research_budget_context',{'approval_id':'expired','remaining':0,'rent_authorized':False})
    context=research_context(store)
    assert context['approval_id']==s.approval_id and context['remaining']==50 and context['rent_authorized']
    assert not s.enabled and store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0


def test_exhausted_listing_purpose_is_distinguished_from_pacing(store,settings,boundary,clock):
    from rentalsourcing.monthly_allocation import period
    from rentalsourcing.partition_discovery import run
    s=grant(settings)
    clock.value=dt.datetime(2026,12,14,6,tzinfo=dt.timezone.utc)
    store.execute('INSERT INTO usage VALUES (?,?)',(cycle_key(s,clock()),36))
    store.execute('INSERT INTO usage VALUES (?,?)',(period(s,clock(),'listing'),36))
    class NoWire:
        def open(self,*a,**k):raise AssertionError('No listing allowance remains')
    receipt,_=run(store,boundary,s,'2026-12-14','morning',clock=clock,opener=NoWire())
    assert receipt['requests_consumed']==0
    assert receipt['budget_reason']=='listing_purpose_allocation_exhausted'
    assert receipt['remaining_budget']==14


def test_runtime_readiness_only_reads_persisted_evidence(store,settings,clock):
    clock.value=dt.datetime(2026,11,16,9,tzinfo=dt.timezone.utc)
    s=grant(settings)
    store.save_setting('multi-discovery:2026-11-16:morning',{'finished_at':clock().isoformat(),'requests_consumed':0})
    before=store.execute('SELECT name,payload FROM settings ORDER BY name').fetchall()
    r=snapshot(store,s,clock())
    assert r['read_only'] and r['provider_calls']==r['email_sends']==0
    assert r['budget']['remaining']==50 and r['monthly_allocation']['active']
    assert r['next_slots'][0]['at_israel']=='2026-11-16T20:00:00+02:00'
    assert r['next_slots'][0]['listing_limit_upper_bound']>0
    assert store.execute('SELECT name,payload FROM settings ORDER BY name').fetchall()==before
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0


def test_paced_slot_is_explicit_without_an_exhaustion_incident(store):
    from rentalsourcing.reliability import audit_scheduled_slots
    now=dt.datetime(2026,11,16,6,41,tzinfo=dt.timezone.utc)
    store.save_setting('discovery_run:2026-11-16:morning',{'finished_at':now.isoformat()})
    store.save_setting('multi-discovery:2026-11-16:morning',{'finished_at':now.isoformat(),
        'discovery_state':'scheduled_pacing_no_listing_request','requests_consumed':0,'pages':[]})
    audit=audit_scheduled_slots(store,now)['morning']
    assert audit['state']=='paced' and audit['reason']=='monthly_listing_allocation_reserved_for_future_slots'
    row=store.execute('SELECT payload FROM settings WHERE name=?',('health_incident:discovery_slot_morning',)).fetchone()
    assert row is None or '"open"' not in row[0]


def test_free_listing_recovery_replays_exact_payload_and_never_spends_provider_budget(store,clock,monkeypatch):
    import hashlib,json
    from rentalsourcing.reliability import recover_listing_operations
    from rentalsourcing.sync_runtime import get
    config=('https://fixture.test/api/rentalsourcing/review-sync','s'*32,'fixture-token')
    monkeypatch.setattr('rentalsourcing.reliability.review_configuration',lambda:config)
    identity='stale-free-refresh';body=json.dumps({'action':'refresh','operation_id':identity},separators=(',',':')).encode()
    name='sync_operation:offmarket_refresh:'+hashlib.sha256(identity.encode()).hexdigest()
    store.save_setting(name,{'id':identity,'component':'offmarket_refresh','state':'pending',
        'digest':hashlib.sha256(body).hexdigest(),'endpoint':'/api/rentalsourcing/offmarket-refresh',
        'last_attempt_at':(clock()-dt.timedelta(hours=1)).isoformat(),'attempts':1})
    class Response:
        status=200
        def read(self,n):return b'{"state":"success","counts":{"received":0}}'
        def __enter__(self):return self
        def __exit__(self,*a):pass
    class FreeReceiver:
        calls=0
        def open(self,request,timeout):
            self.calls+=1;assert request.full_url.endswith('/offmarket-refresh');assert request.data==body
            return Response()
    receiver=FreeReceiver();result=recover_listing_operations(store,clock(),receiver)
    assert result['recovered']==1 and receiver.calls==1 and get(store,name)['state']=='success'
    assert recover_listing_operations(store,clock(),receiver)['recovered']==0 and receiver.calls==1
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0


@pytest.mark.parametrize('result',['timeout','rate_limited'])
def test_slow_or_busy_listing_recovery_keeps_health_budget_and_retryable_evidence(store,clock,monkeypatch,result):
    import hashlib,json
    from rentalsourcing.reliability import recover_listing_operations
    from rentalsourcing.sync_runtime import DEADLINE,get,request_json
    from conftest import Response
    config=('https://fixture.test/api/rentalsourcing/review-sync','s'*32,'fixture-token')
    monkeypatch.setattr('rentalsourcing.reliability.review_configuration',lambda:config)
    tick=[0.0]
    monkeypatch.setattr('rentalsourcing.reliability.time.monotonic',lambda:tick[0])
    for identity in ('old-a','old-b'):
        body=json.dumps({'action':'refresh','operation_id':identity},separators=(',',':')).encode()
        name='sync_operation:offmarket_refresh:'+hashlib.sha256(identity.encode()).hexdigest()
        store.save_setting(name,{'id':identity,'component':'offmarket_refresh','state':'pending','attempts':1,
            'digest':hashlib.sha256(body).hexdigest(),'endpoint':'/api/rentalsourcing/offmarket-refresh',
            'last_attempt_at':(clock()-dt.timedelta(hours=1)).isoformat()})
    class Wire:
        calls=[]
        def open(self,request,timeout):
            self.calls.append(request.full_url)
            if request.full_url.endswith('/offmarket-refresh'):
                assert timeout<=40
                if result=='timeout':
                    tick[0]=41.0
                    raise TimeoutError()
                return Response({'state':'rate_limited'})
            return Response({'accepted':True})
    wire=Wire();parent=DEADLINE.set(120.0)
    try:
        receipt=recover_listing_operations(store,clock(),wire)
        assert receipt['recovered']==0 and DEADLINE.get()==120.0
        assert request_json(store,'health_delivery','health-after-slow-source',config,b'{}',opener=wire,now=clock())['accepted']
    finally:DEADLINE.reset(parent)
    a=get(store,'sync_operation:offmarket_refresh:'+hashlib.sha256(b'old-a').hexdigest())
    b=get(store,'sync_operation:offmarket_refresh:'+hashlib.sha256(b'old-b').hexdigest())
    assert a['state']=='pending' and a['next_attempt_at'] and b['attempts']==1
    assert wire.calls.count('https://fixture.test/api/rentalsourcing/offmarket-refresh')==1
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0


@pytest.mark.parametrize('gap',[None,'partial_source','pending_disposition','missing_destination','missing_email_check'])
def test_obsolete_free_refresh_reconciliation_requires_later_complete_source_and_delivery_proof(store,clock,gap):
    import hashlib,json
    from rentalsourcing.reliability import reconcile_completed_source_refreshes
    from rentalsourcing.sync_runtime import get
    source_at=(clock()-dt.timedelta(hours=1)).isoformat()
    source={'attempted_at':source_at,'state':'success','result':{'state':'success','partial':False,
        'counts':{'received':26,'duplicates':26},'email':{'authorized_accounts':1,'results':[{'state':'success','more':False}]},
        'realscout_audit':{'counts':{'observed':111,'delivered_new':2,'preserved_owner_decision':52,
            'rejected_hard_criterion':54,'pending_additional_evidence':3,'pending_retry':0}}}}
    destination={'at':clock().isoformat(),'result':{'current_inventory':{'missing':0,'rejected_at_destination':0,
        'tax_status_mismatch':0,'pending_tax_status_mismatch':0}}}
    if gap=='partial_source':source['result']['partial']=True
    if gap=='pending_disposition':source['result']['realscout_audit']['counts']['pending_retry']=1
    if gap=='missing_destination':destination['result']['current_inventory']['missing']=1
    if gap=='missing_email_check':source['result']['email']['results']=[]
    store.save_setting('offmarket_scheduler',source);store.save_setting('health_last_audit',destination)
    names={}
    original_error={'category':'network','retryable':True}
    for identity,created in (('older',clock()-dt.timedelta(hours=2)),('newer',clock())):
        body=json.dumps({'action':'refresh','operation_id':identity},separators=(',',':')).encode()
        name='sync_operation:offmarket_refresh:'+hashlib.sha256(identity.encode()).hexdigest();names[identity]=name
        store.save_setting(name,{'id':identity,'component':'offmarket_refresh','state':'pending','attempts':3,
            'created_at':created.isoformat(),'last_attempt_at':clock().isoformat(),'error':original_error,
            'endpoint':'/api/rentalsourcing/offmarket-refresh','digest':hashlib.sha256(body).hexdigest()})
    result=reconcile_completed_source_refreshes(store,clock())
    older=get(store,names['older'])
    assert result['reconciled']==(0 if gap else 1)
    assert older['state']==('pending' if gap else 'reconciled')
    assert older['error']==original_error and older['attempts']==3 and not older.get('receipt')
    if not gap:assert older['original_execution_confirmed'] is False and older['reconciliation_evidence']['source_attempted_at']==source_at
    assert get(store,names['newer'])['state']=='pending'
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0
