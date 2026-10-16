import datetime as dt
import json
from dataclasses import replace
import pytest
from rentalsourcing.collector import LIVE
from rentalsourcing.core import basic_rejection,verification
from rentalsourcing.criteria import reevaluate
from rentalsourcing.digest import Digest,ReconciliationRequired
from rentalsourcing.schedule import scheduled_day,ISRAEL
from conftest import listing,taxes,Provider,Response


def saved(store,clock,identity='eligible',zipcode='15207'):
    row=listing(identity,zipCode=zipcode,listingUrl='https://broker.example.test/listing/'+identity)
    store.save_property(identity,row,'Qualified',[],LIVE,1,clock().isoformat())
    store.save_tax(identity,taxes(identity),LIVE,clock().isoformat(),(clock()+dt.timedelta(days=7)).isoformat())
    return row


@pytest.mark.parametrize('zipcode',['15210','15203','15204','15204-1234'])
def test_all_exclusions_apply_to_both_approval_paths(boundary,zipcode):
    row=listing(zipCode=zipcode)
    assert basic_rejection(row)=='excluded_zip'
    assert verification(row,boundary)[0]=='Rejected'


def test_recheck_downgrades_previously_approved_15204_without_deleting_history(store,boundary,clock):
    saved(store,clock,'excluded','15204');saved(store,clock)
    before=store.execute('SELECT COUNT(*) FROM tax_history').fetchone()[0]
    reevaluate(store,boundary,clock())
    assert store.get_property('excluded')['state']=='Rejected'
    assert store.get_property('excluded')['reasons']==['excluded_zip']
    assert store.get_property('eligible')['state']=='Qualified'
    assert store.execute('SELECT COUNT(*) FROM tax_history').fetchone()[0]==before
    assert store.execute('SELECT COUNT(*) FROM properties').fetchone()[0]==2


def test_bootstrap_uses_approved_saved_records_with_no_scan_tasks_or_api_allowance(store,boundary,settings,clock):
    saved(store,clock);saved(store,clock,'excluded','15204')
    provider=Provider();s=replace(settings,enabled=False,approved_requests=0,email_enabled=False,email_approval_id='')
    digest=Digest(store,boundary,s,clock,provider)
    identity=digest.prepare_stored('2026-10-17')
    body=json.loads(store.execute('SELECT payload FROM outbox WHERE id=?',(identity,)).fetchone()[0])
    assert 'eligible' in body['text'] and 'excluded' not in body['text']
    assert '15204' not in body['text']
    for expected in ('180,000.00','3','1','1000','2025','1,600.00','https://broker.example.test/listing/eligible','לא בוצעה סריקה חדשה'):
        assert expected in body['text']
    assert provider.calls==[]
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    assert digest.prepare_stored('2026-10-17')==identity
    with pytest.raises(PermissionError):digest.send(identity)


def test_stale_saved_data_produces_truthful_status_email(store,boundary,settings,clock):
    saved(store,clock);clock.advance(hours=25)
    digest=Digest(store,boundary,settings,clock,Provider())
    identity=digest.prepare_stored(clock().date().isoformat())
    body=json.loads(store.execute('SELECT payload FROM outbox WHERE id=?',(identity,)).fetchone()[0])
    assert 'eligible' not in body['text']
    assert 'אין הזדמנויות חדשות מאומתות' in body['subject']
    assert 'לא בוצעה סריקה חדשה' in body['text']
    assert store.get_property('eligible')['state']=='Pending Verification'


def test_pending_preview_can_be_rebuilt_after_new_exclusion_without_duplicate_delivery(store,boundary,settings,clock):
    saved(store,clock);provider=Provider();digest=Digest(store,boundary,settings,clock,provider)
    identity=digest.prepare_stored('2026-10-17')
    p=store.get_property('eligible');p['payload']['zipCode']='15204'
    store.save_property('eligible',p['payload'],'Qualified',[],LIVE,1,clock().isoformat())
    assert digest.send(identity)=='cancelled_stale'
    assert provider.messages==[]
    assert digest.prepare_stored('2026-10-17')==identity
    assert digest.send(identity)=='sent'
    assert 'eligible' not in provider.messages[0]['text']
    assert digest.send(identity)=='already_sent'
    assert len(provider.messages)==1


def test_uncertain_retry_cannot_send_newly_excluded_property(store,boundary,settings,clock):
    saved(store,clock);provider=Provider();digest=Digest(store,boundary,settings,clock,provider)
    identity=digest.prepare_stored('2026-10-17');provider.failures=[TimeoutError()]
    with pytest.raises(TimeoutError):digest.send(identity)
    p=store.get_property('eligible');p['payload']['zipCode']='15204'
    store.save_property('eligible',p['payload'],'Qualified',[],LIVE,1,clock().isoformat())
    with pytest.raises(ReconciliationRequired):digest.send(identity)
    assert len(provider.calls)==1


def test_test_email_does_not_consume_first_property_digest(store,boundary,settings,clock):
    saved(store,clock);provider=Provider();digest=Digest(store,boundary,settings,clock,provider)
    identity=digest.prepare_test()
    assert digest.send(identity)=='sent'
    assert digest.send(identity)=='already_sent'
    assert store.execute('SELECT COUNT(*) FROM deal_events').fetchone()[0]==0
    first=digest.prepare_stored('2026-10-17')
    body=json.loads(store.execute('SELECT payload FROM outbox WHERE id=?',(first,)).fetchone()[0])
    assert 'eligible' in body['text']
    assert len(provider.messages)==1


def test_sender_and_recipient_are_bound_to_frozen_message(store,boundary,settings,clock):
    provider=Provider();identity=Digest(store,boundary,settings,clock,provider).prepare_test()
    changed=Digest(store,boundary,replace(settings,email_to='changed@example.test'),clock,provider)
    with pytest.raises(PermissionError,match='sender/recipient'):changed.send(identity)
    assert provider.calls==[]


def test_delivery_confirmation_requires_resend_delivered_event(store,boundary,settings,clock):
    provider=Provider();digest=Digest(store,boundary,settings,clock,provider)
    identity=digest.prepare_test();digest.send(identity)
    class Status:
        def __init__(self,status):self.status=status
        def open(self,request,timeout=None):
            assert request.get_method()=='GET'
            return Response({'id':'fixture-message-id','last_event':self.status})
    digest.opener=Status('sent')
    assert digest.delivery_status(identity)['delivered'] is False
    digest.opener=Status('delivered')
    assert digest.delivery_status(identity)['delivered'] is True


def test_first_target_and_both_israel_offsets():
    target=dt.datetime(2026,10,17,8,tzinfo=ISRAEL)
    assert target.astimezone(dt.timezone.utc)==dt.datetime(2026,10,17,5,tzinfo=dt.timezone.utc)
    assert scheduled_day(target)=='2026-10-17'
    assert scheduled_day(target+dt.timedelta(hours=1)) is None
    winter=dt.datetime(2026,12,1,8,tzinfo=ISRAEL)
    assert winter.astimezone(dt.timezone.utc).hour==6


def test_concurrent_send_has_only_one_provider_call(store,boundary,settings,clock):
    if not store.pg:pytest.skip('Concurrent sessions need PostgreSQL')
    from test_postgres import simultaneous
    provider=Provider();identity=Digest(store,boundary,settings,clock,provider).prepare_test()
    results=simultaneous(store,lambda other:Digest(other,boundary,settings,clock,provider).send(identity))
    assert results.count('sent')==1 and results.count('already_sent')==7
    assert len(provider.messages)==1


def test_scheduled_email_never_constructs_rentcast_client_even_if_collection_enabled(store,boundary,settings,clock,monkeypatch):
    from rentalsourcing import cloud
    saved(store,clock)
    provider=Provider()
    s=replace(settings,enabled=True,approved_requests=0)
    class Handle:
        def __getattr__(self,name):return getattr(store,name)
        def close(self):pass
    class Frozen(dt.datetime):
        @classmethod
        def now(cls,tz=None):return dt.datetime(2026,10,17,5,tzinfo=dt.timezone.utc)
    monkeypatch.setattr(cloud.dt,'datetime',Frozen)
    monkeypatch.setattr(cloud.Settings,'from_env',classmethod(lambda cls:s))
    monkeypatch.setattr(cloud,'Store',lambda url:Handle())
    monkeypatch.setattr(cloud,'load_boundary',lambda path:boundary)
    def forbidden(*args,**kwargs):raise AssertionError('Stored email must never create RentCast client')
    monkeypatch.setattr(cloud,'Client',forbidden)
    monkeypatch.setattr(cloud,'Digest',lambda db,b,s:Digest(db,b,s,lambda:Frozen.now(),provider))
    monkeypatch.setenv('DATABASE_URL','postgresql://u:p@render-db/db')
    cloud.main(['--scheduled-email']);cloud.main(['--scheduled-email'])
    assert len(provider.messages)==1
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    assert store.execute('SELECT day FROM scheduled_days').fetchone()[0]=='israel:2026-10-17'
