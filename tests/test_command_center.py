"""Only synthetic records and mock HTTP; never access either production system."""
import hashlib
import hmac
import json
from dataclasses import replace
import pytest
from conftest import Provider, listing, Response
from rentalsourcing.cloud import daily_discovery
from rentalsourcing.command_center import configuration, flush
from rentalsourcing.digest import Digest
from rentalsourcing.engine import Engine
from rentalsourcing.collector import Client


def generate(store,boundary,settings,clock,provider,day=None):
    day=day or clock().date().isoformat()
    stats=Engine(store,boundary,Client(store,settings,clock,provider,lambda _:None),settings,clock).run('daily:'+day,day,first_page_only=True)
    digest=Digest(store,boundary,settings,clock,provider)
    return digest,digest.prepare('daily:'+day,day,stats)


def captured(store,identity):
    return json.loads(store.execute('SELECT payload FROM command_center_reports WHERE outbox_id=?',(identity,)).fetchone()[0])


class Receiver:
    def __init__(self,fail=False):self.requests=[];self.fail=fail
    def open(self,request,timeout):
        self.requests.append(request)
        assert timeout==5
        if self.fail:raise TimeoutError('secret or message content must not be logged')
        return Response({'accepted':True,'report_id':json.loads(request.data)['report_id']})


def enable(monkeypatch):
    monkeypatch.setenv('ENABLE_COMMAND_CENTER_SYNC','true')
    monkeypatch.setenv('COMMAND_CENTER_SYNC_URL','https://command-center.example.test/api/rentalsourcing/sync')
    monkeypatch.setenv('COMMAND_CENTER_SYNC_SECRET','synthetic-signing-secret-32-characters')
    monkeypatch.setenv('COMMAND_CENTER_SERVICE_TOKEN','synthetic-sites-token')


def test_exact_daily_selection_pending_and_no_raw_or_excluded_records(store,boundary,settings,clock):
    rows=[listing('verified'),listing('pending',listingUrl='https://listing.example.test/pending'),listing('excluded',zipCode='15211'),listing('expensive',price=210000)]
    provider=Provider(rows);provider.records.pop('pending')
    digest,identity=generate(store,boundary,replace(settings,page_size=100),clock,provider)
    report=captured(store,identity)
    assert {p['source_id'] for p in report['opportunities']}=={'verified','pending'}
    pending=next(p for p in report['opportunities'] if p['source_id']=='pending')
    assert pending['qualification']==pending['tax_status']=='pending_verification'
    assert pending['annual_tax'] is pending['tax_year'] is None
    assert pending['listing_url']=='https://listing.example.test/pending'
    verified=next(p for p in report['opportunities'] if p['source_id']=='verified')
    assert verified['annual_tax']==1500 and verified['tax_year']==2025
    envelope=json.loads(store.execute('SELECT payload FROM outbox WHERE id=?',(identity,)).fetchone()[0])
    assert set(envelope)=={'from','to','subject','text','html'}
    assert verified['address'] in envelope['text'] and 'Pending Verification' in envelope['text']
    assert all(k not in json.dumps(report) for k in ['fixture-resend-not-real','investor@example.test'])


def test_frozen_snapshot_and_price_reduction(store,boundary,settings,clock):
    p=Provider();digest,identity=generate(store,boundary,settings,clock,p)
    first=captured(store,identity)
    assert digest.send(identity)=='sent'
    clock.advance(days=1);p.rows=[listing(price=170000)]
    digest,second=generate(store,boundary,settings,clock,p)
    updated=captured(store,second)['opportunities'][0]
    assert updated['section']=='price_reduction' and updated['previous_price']==180000
    assert updated['discovered_at']==first['opportunities'][0]['discovered_at']
    assert captured(store,identity)==first


def test_disabled_default_and_only_accepted_daily_messages_export(store,boundary,settings,clock,monkeypatch):
    monkeypatch.delenv('ENABLE_COMMAND_CENTER_SYNC',raising=False)
    digest,identity=generate(store,boundary,settings,clock,Provider())
    receiver=Receiver()
    assert flush(store,clock(),receiver)==0 and not receiver.requests
    enable(monkeypatch)
    assert flush(store,clock(),receiver)==0
    store.execute("UPDATE outbox SET state='uncertain' WHERE id=?",(identity,))
    assert flush(store,clock(),receiver)==0
    store.execute("UPDATE outbox SET state='sent' WHERE id=?",(identity,))
    assert flush(store,clock(),receiver)==1
    request=receiver.requests[0];headers={k.lower():v for k,v in request.header_items()}
    timestamp=headers['x-rentalsourcing-timestamp']
    expected=hmac.new(b'synthetic-signing-secret-32-characters',timestamp.encode()+b'.'+request.data,hashlib.sha256).hexdigest()
    assert headers['x-rentalsourcing-signature']=='v1='+expected
    assert headers['oai-sites-authorization']=='Bearer synthetic-sites-token'
    assert flush(store,clock(),receiver)==0 and len(receiver.requests)==1


def test_sync_failure_survives_restart_and_cannot_interfere_with_email(store,boundary,settings,clock,monkeypatch):
    digest,identity=generate(store,boundary,settings,clock,Provider())
    import rentalsourcing.command_center as integration
    monkeypatch.setattr(integration,'flush',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('synthetic failure')))
    assert digest.send(identity)=='sent'
    assert store.execute('SELECT state FROM outbox WHERE id=?',(identity,)).fetchone()[0]=='sent'
    enable(monkeypatch);receiver=Receiver(fail=True)
    assert flush(store,clock(),receiver)==0
    from rentalsourcing.storage import Store
    reopened=Store(store.test_url,initialize=False)
    try:
        receiver.fail=False
        assert flush(reopened,clock(),receiver)==1
        assert len(receiver.requests)==2 and receiver.requests[0].data==receiver.requests[1].data
        row=reopened.execute('SELECT attempts,error_type,synced_at FROM command_center_reports WHERE outbox_id=?',(identity,)).fetchone()
        assert row[0]==2 and row[1] is None and row[2]
    finally:reopened.close()
    assert digest.send(identity)=='already_sent'


def test_missing_or_invalid_configuration_fails_closed(monkeypatch):
    enable(monkeypatch)
    assert configuration()
    for url in ['http://example.test/api/rentalsourcing/sync','https://user:password@example.test/api/rentalsourcing/sync','https://example.test/wrong']:
        monkeypatch.setenv('COMMAND_CENTER_SYNC_URL',url)
        with pytest.raises(ValueError):configuration()


def test_stored_email_snapshot_and_preview_not_exportable(store,boundary,settings,clock):
    provider=Provider();stats=Engine(store,boundary,Client(store,settings,clock,provider,lambda _:None),settings,clock).run('fixture',clock().date().isoformat())
    digest=Digest(store,boundary,settings,clock,provider)
    preview=digest.prepare('fixture','2026-10-15',stats,preview=True)
    assert store.execute('SELECT payload FROM command_center_reports WHERE outbox_id=?',(preview,)).fetchone() is None
    identity=digest.prepare_stored(clock().date().isoformat())
    assert captured(store,identity)['discovery_state']=='stored'
    assert len(captured(store,identity)['opportunities'])==1


def test_legacy_sent_emails_are_never_reconstructed(store,boundary,settings,clock):
    store.execute('INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?)',('legacy','daily:2026-10-14','sent','{}','[]','old',clock().isoformat(),clock().isoformat()))
    assert Digest(store,boundary,settings,clock,Provider()).send('legacy')=='already_sent'
    assert store.execute('SELECT COUNT(*) FROM command_center_reports').fetchone()[0]==0


def test_shared_contract_fixture_matches_actual_digest_snapshot(store,boundary,settings,clock):
    from pathlib import Path
    rows=[listing('contract-a',formattedAddress='100 Fixture Avenue, Pittsburgh, PA 15212',addressLine1='100 Fixture Avenue',listingUrl='https://listings.example.test/a'),
          listing('contract-b',formattedAddress='200 Fixture Street, Pittsburgh, PA 15212',addressLine1='200 Fixture Street')]
    provider=Provider(rows);provider.records.pop('contract-b')
    s=replace(settings,approval_id='fixture-sync-only',approved_requests=30,per_run=4,per_day=4,per_month=40,page_size=100)
    result=daily_discovery(store,boundary,s,clock().date().isoformat(),clock=clock,opener=provider)
    assert result['status']=='sent'
    identity=result['outbox_id']
    expected=json.loads((Path(__file__).parent/'fixtures'/'command-center-report.json').read_text())
    assert captured(store,identity)==expected


def test_delivery_backlog_is_bounded_and_cancellation_does_not_export(store,boundary,settings,clock,monkeypatch):
    from rentalsourcing.command_center import snapshot
    enable(monkeypatch)
    for day in range(1,6):
        identity='synthetic-'+str(day)
        store.execute('INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?)',(identity,'daily:2026-10-0'+str(day),'sent' if day<5 else 'cancelled','{}','[]','mock-accepted',None,clock().isoformat()))
        snapshot(store,identity,'2026-10-0'+str(day),[],{},clock())
    receiver=Receiver()
    assert flush(store,clock(),receiver)==3 and len(receiver.requests)==3
    assert flush(store,clock(),receiver)==1 and len(receiver.requests)==4
    assert flush(store,clock(),receiver)==0
