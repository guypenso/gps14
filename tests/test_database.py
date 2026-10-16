import pytest
import psycopg
from rentalsourcing.database import validate_postgres_url, connect_postgres


@pytest.mark.parametrize('url', ['', 'postgresql:///db', 'postgresql://u:p@/db',
    'postgresql://u:p@localhost/db', 'postgresql://u:p@127.0.0.1/db',
    'postgresql://u:p@[::1]/db', 'postgresql://u:p@host:bad/db',
    'postgresql://u:p@host/db?host=localhost', 'postgresql://host/db',
    'postgresql://u:p@host/', 'sqlite:///:memory:'])
def test_reject_invalid_production_database_without_leaking_credentials(url):
    with pytest.raises(ValueError) as error:
        validate_postgres_url(url)
    assert 'u:p@' not in str(error.value)


def test_render_internal_and_external_urls():
    for host in ('dpg-existing-a', 'dpg-existing-a.virginia-postgres.render.com'):
        url=f'postgresql://user:secret@{host}/rentalsourcing?sslmode=require'
        assert validate_postgres_url(url)==url


def test_connection_recovers_with_bounded_backoff(monkeypatch, caplog):
    calls=[];delays=[];connection=object()
    def connect(*args, **kwargs):
        calls.append(kwargs)
        if len(calls)<3:raise psycopg.OperationalError('secret connection detail')
        return connection
    monkeypatch.setattr(psycopg,'connect',connect)
    monkeypatch.setattr('rentalsourcing.database.time.sleep',delays.append)
    assert connect_postgres('postgresql://u:secret@db/app') is connection
    assert delays==[1,2]
    assert all(c['connect_timeout']==10 for c in calls)
    assert 'secret' not in caplog.text


def test_connection_exhaustion_is_safe(monkeypatch):
    calls=[]
    def connect(*args, **kwargs):
        calls.append(1)
        raise psycopg.OperationalError('password=secret')
    monkeypatch.setattr(psycopg,'connect',connect)
    monkeypatch.setattr('rentalsourcing.database.time.sleep',lambda _:None)
    with pytest.raises(RuntimeError,match='after 4') as error:
        connect_postgres('postgresql://u:secret@db/app')
    assert len(calls)==4
    assert 'secret' not in str(error.value)


def test_migration_lock_contention_fails_instead_of_starting_web(monkeypatch):
    from rentalsourcing.cloud import main
    closed=[]
    class BusyStore:
        def __init__(self,url):pass
        def lock(self):return False
        def close(self):closed.append(True)
    monkeypatch.setenv('DATABASE_URL','postgresql://u:secret@db/app')
    monkeypatch.setattr('rentalsourcing.cloud.Store',BusyStore)
    with pytest.raises(RuntimeError,match='migrations did not run'):
        main(['--migrate'])
    assert closed==[True]


def test_start_script_stops_when_migrations_fail(tmp_path):
    import os
    import subprocess
    from pathlib import Path
    python=tmp_path/'python';python.write_text('#!/bin/sh\nexit 23\n');python.chmod(0o755)
    gunicorn=tmp_path/'gunicorn';gunicorn.write_text('#!/bin/sh\ntouch "$WEB_STARTED"\n');gunicorn.chmod(0o755)
    marker=tmp_path/'started'
    env=dict(os.environ,PATH=str(tmp_path)+':'+os.environ['PATH'],WEB_STARTED=str(marker))
    result=subprocess.run(['sh',str(Path(__file__).parents[1]/'scripts/start_web.sh')],env=env)
    assert result.returncode==23
    assert not marker.exists()
