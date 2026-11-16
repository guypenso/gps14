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


def test_free_plan_ceiling_cannot_be_raised_by_conflicting_configuration(settings):
    s=replace(grant(settings),per_month=500,monthly_budget_requests=500,provider_included_requests=500)
    assert s.request_ceiling==50 and sum(limits(s).values())==50


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
