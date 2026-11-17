import datetime as dt
import hashlib
import json
import pytest

from rentalsourcing.sync_runtime import DEADLINE,SyncFailure,get,request_json
from test_sync_reliability import CONFIG,Wire,error


def test_local_expired_slice_does_not_create_wire_attempt_or_upstream_incident(store,clock,monkeypatch):
    wire=Wire([{'accepted':True}])
    monkeypatch.setattr('rentalsourcing.sync_runtime.time.monotonic',lambda:20)
    token=DEADLINE.set(19)
    try:
        with pytest.raises(SyncFailure,match='recovery_time_budget'):
            request_json(store,'listing_pre_report','not-dispatched',CONFIG,b'{}',opener=wire,now=clock())
    finally:DEADLINE.reset(token)
    assert wire.calls==[]
    assert store.execute('SELECT COUNT(*) FROM settings WHERE name LIKE ?',('sync_operation:%',)).fetchone()[0]==0
    assert store.execute('SELECT COUNT(*) FROM settings WHERE name LIKE ?',('health_incident:%',)).fetchone()[0]==0


def test_long_lived_successful_poll_does_not_inherit_lifetime_failure_backoff(store,clock):
    wire=Wire([{'accepted':True}])
    request_json(store,'health_roundtrip','poll',CONFIG,b'{}',opener=wire,now=clock(),poll=True)
    key='sync_operation:health_roundtrip:'+hashlib.sha256(b'poll').hexdigest()
    store.save_setting(key,{**get(store,key),'attempts':3665})
    wire=Wire([error(429)])
    # Use a non-rate-limited failure so the Retry-After fixture is not involved.
    wire.responses=[TimeoutError(),TimeoutError(),TimeoutError()]
    with pytest.raises(SyncFailure):
        request_json(store,'health_roundtrip','poll',CONFIG,b'{}',opener=wire,now=clock(),poll=True,sleeper=lambda _:None)
    ledger=get(store,key)
    assert ledger['attempts']==3668 and ledger['consecutive_failures']==3
    retry=dt.datetime.fromisoformat(ledger['next_attempt_at'])
    assert 8 <= (retry-clock()).total_seconds() < 10
    clock.advance(minutes=1)
    request_json(store,'health_roundtrip','poll',CONFIG,b'{}',opener=Wire([{'accepted':True}]),now=clock(),poll=True)
    assert get(store,key)['consecutive_failures']==0


def test_retry_cooldown_starts_after_wire_timeout_not_before_it(store,clock,monkeypatch):
    ticks=[0]
    monkeypatch.setattr('rentalsourcing.sync_runtime.time.monotonic',lambda:ticks[0])
    class SlowWire:
        def open(self,*args,**kwargs):ticks[0]+=20;raise TimeoutError()
    with pytest.raises(SyncFailure):
        request_json(store,'property_sync','slow',CONFIG,b'{}',opener=SlowWire(),now=clock(),sleeper=lambda _:None)
    key='sync_operation:property_sync:'+hashlib.sha256(b'slow').hexdigest()
    assert dt.datetime.fromisoformat(get(store,key)['next_attempt_at'])==clock()+dt.timedelta(seconds=68)


def test_health_aggregates_history_in_database_without_loading_success_payloads(store,clock,monkeypatch):
    from rentalsourcing.reliability import snapshot
    for n in range(25):store.save_setting('sync_operation:history:'+str(n),{'state':'success','receipt':{'accepted':True}})
    for n in range(23):store.save_setting('sync_operation:retry:'+str(n),{'state':'pending','id':str(n)})
    for n in range(4):store.save_setting('price-observation:'+str(n),{'synced_at':None if n<2 else clock().isoformat()})
    original=store.execute
    def bounded(sql,args=()):
        if sql.startswith('SELECT payload FROM settings') and args in [('sync_operation:%',),('price-observation:%',)]:
            assert 'LIMIT 20' in sql
        return original(sql,args)
    monkeypatch.setattr(store,'execute',bounded)
    report=snapshot(store,clock())
    assert report['counts']['pending_operations']==23 and len(report['pending_operation_details'])==20
    assert report['counts']['pending_price_observations']==2


def test_all_incident_and_recovery_readers_leave_success_history_in_database(store,clock,boundary,monkeypatch):
    from rentalsourcing.execution_evidence import snapshot
    from rentalsourcing.reliability import reconcile_completed_enrichment,reconcile_completed_source_refreshes,recover_listing_operations
    from rentalsourcing.sync_runtime import operation_rows
    for n in range(30):store.save_setting('sync_operation:history:'+str(n),{'state':'success','receipt':{'audit':'historic evidence '*10000}})
    store.save_setting('sync_operation:pending:fixture',{'state':'pending','component':'fixture','id':'fixture'})
    original=store.execute
    def bounded(sql,args=()):
        projection=sql.split('FROM')[0]
        if 'payload' in projection and 'LIKE' in sql and any(isinstance(a,str) and a.startswith('sync_operation:') and a.endswith('%') for a in args):
            assert "IN (" in sql and 'state' in sql
        return original(sql,args)
    monkeypatch.setattr(store,'execute',bounded)
    assert len(operation_rows(store))==1
    assert snapshot(store,clock(),boundary)['operations']['pending_count']==1
    reconcile_completed_enrichment(store,clock())
    reconcile_completed_source_refreshes(store,clock())
    monkeypatch.setattr('rentalsourcing.reliability.review_configuration',lambda:CONFIG)
    assert recover_listing_operations(store,clock())['recovered']==0
    assert store.execute('SELECT COUNT(*) FROM settings WHERE name LIKE ?',('sync_operation:history:%',)).fetchone()[0]==30


def test_pending_price_delivery_prevents_successful_scheduled_window_claim(store,clock,monkeypatch):
    from rentalsourcing.reliability import scheduled_window_outcome
    monkeypatch.setattr('rentalsourcing.reliability.audit_scheduled_slots',lambda *a,**k:{'evening':{'state':'paced'}})
    counts=dict.fromkeys(('pending_reports','pending_operations','dead_letters','pending_inventory_jobs'),0)
    counts['pending_price_observations']=1
    monkeypatch.setattr('rentalsourcing.reliability.snapshot',lambda *a,**k:{'counts':counts,'incidents':[]})
    result=scheduled_window_outcome(store,'evening',clock().isoformat(),clock())
    assert result['current_window_critical']==result['unresolved_critical']==1
