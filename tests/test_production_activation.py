import datetime as dt
from dataclasses import replace

from rentalsourcing.billing import cycle_key
from rentalsourcing.monthly_allocation import limits
from rentalsourcing.runtime_readiness import snapshot
from test_monthly_allocation import grant


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
