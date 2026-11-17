import datetime as dt
import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rentalsourcing.collector import LIVE
from rentalsourcing.dashboard import create_app
from rentalsourcing.diagnostics import funnel
from conftest import listing


def test_legacy_cache_selects_only_latest_listing_without_loading_historical_payloads(store,boundary,clock,monkeypatch):
    for i in range(6):
        when=(clock()-dt.timedelta(days=6-i)).isoformat()
        store.execute('INSERT INTO response_cache VALUES (?,?,?,?)',(f'page-{i}',json.dumps({'data':[listing(f'old-{i}',description='source evidence '*3000)],'fetched_at':when,'total':1}),LIVE,when))
    store.execute('INSERT INTO response_cache VALUES (?,?,?,?)',('new-tax',json.dumps({'data':{'id':'tax'},'fetched_at':clock().isoformat()}),LIVE,clock().isoformat()))
    store.execute('INSERT INTO response_cache VALUES (?,?,?,?)',('fixture',json.dumps({'data':[listing()],'fetched_at':clock().isoformat()}),'fixture',clock().isoformat()))
    original=store.execute
    def bounded(sql,args=()):
        assert not (sql.startswith('SELECT payload,source FROM response_cache') and 'WHERE' not in sql)
        return original(sql,args)
    monkeypatch.setattr(store,'execute',bounded)
    before=store.execute('SELECT name,payload FROM settings ORDER BY name').fetchall()
    latest=store.latest_listing_response()
    assert latest['data'][0]['id']=='old-5'
    assert funnel(store,boundary,clock())['latest_discovery']['received']==1
    assert store.execute('SELECT name,payload FROM settings ORDER BY name').fetchall()==before
    store.bootstrap_listing_cache_index()
    assert store.latest_listing_response()==latest


def test_listing_cache_index_preserves_newer_evidence_and_ignores_other_provider_records(store,clock):
    old=(clock()-dt.timedelta(days=1)).isoformat();new=clock().isoformat()
    store.cache_put('new',{'data':[listing('new')],'fetched_at':new},LIVE,new)
    store.cache_put('old',{'data':[listing('old')],'fetched_at':old},LIVE,new)
    store.cache_put('rent',{'data':{'rent':1600},'fetched_at':new},LIVE,new)
    assert store.latest_listing_response()['data'][0]['id']=='new'
    assert store.execute('SELECT COUNT(*) FROM response_cache').fetchone()[0]==3
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0


def test_health_probe_remains_available_while_large_reads_are_at_capacity(store,boundary,clock,monkeypatch):
    password='bounded-fixture-password-20261117';monkeypatch.setenv('DASHBOARD_PASSWORD',password)
    headers={'Authorization':'Basic '+base64.b64encode(('owner:'+password).encode()).decode()}
    app=create_app(store.test_url,testing=True,clock=clock,boundary=boundary)
    barrier=threading.Barrier(3);release=threading.Event()
    def large(*args):
        barrier.wait(timeout=5);assert release.wait(5)
        return {'fixture':True}
    monkeypatch.setattr('rentalsourcing.dashboard.snapshot',large)
    def request_large():
        with app.test_client() as client:return client.get('/api/dashboard',headers=headers).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(request_large);second=pool.submit(request_large)
        barrier.wait(timeout=5)
        try:
            with app.test_client() as client:
                assert client.get('/healthz').status_code==200
                busy=client.get('/api/dashboard',headers=headers)
                assert busy.status_code==429 and busy.headers['Retry-After']=='2'
                assert client.get('/api/dashboard').status_code==401
        finally:release.set()
        assert first.result()==second.result()==200
    monkeypatch.setattr('rentalsourcing.dashboard.snapshot',lambda *args:{'fixture':True})
    with app.test_client() as client:assert client.get('/api/dashboard',headers=headers).status_code==200


def test_existing_service_uses_one_threaded_process_without_duplicate_background_owners():
    from rentalsourcing import gunicorn_config
    assert gunicorn_config.workers==1 and gunicorn_config.worker_class=='gthread'
    assert gunicorn_config.threads>=4
    startup=(Path(__file__).parents[1]/'scripts/start_web.sh').read_text()
    assert '--workers 2' not in startup and '--timeout 60' in startup


def test_readiness_database_failure_is_one_short_attempt(monkeypatch):
    import psycopg
    import pytest
    from rentalsourcing.database import probe_postgres
    calls=[]
    def unavailable(*args,**kwargs):
        calls.append(kwargs);raise psycopg.OperationalError('fixture unavailable')
    monkeypatch.setattr(psycopg,'connect',unavailable)
    with pytest.raises(psycopg.OperationalError):probe_postgres('postgresql://fixture:fixture@db.example.test/app')
    assert len(calls)==1 and calls[0]['connect_timeout']==2
    assert 'statement_timeout=1000' in calls[0]['options']
