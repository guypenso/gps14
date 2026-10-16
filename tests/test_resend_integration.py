import io
import json
import urllib.error
from dataclasses import replace
import pytest
from rentalsourcing.digest import Digest,ReconciliationRequired
from rentalsourcing.resend_api import failure,request
from conftest import Provider


def error(payload,headers=None):
    data=json.dumps(payload).encode() if isinstance(payload,dict) else payload
    return urllib.error.HTTPError('https://api.resend.com/emails',403,'Forbidden',headers or {},io.BytesIO(data))


def test_resend_wire_contract_matches_official_api():
    body=json.dumps({'from':'RentalSourcing <onboarding@resend.dev>','to':['registered@example.test'],'subject':'בדיקה','text':'טקסט'})
    req=request(body,'secret-key','test-identity')
    assert req.full_url=='https://api.resend.com/emails' and req.get_method()=='POST'
    headers={k.lower():v for k,v in req.header_items()}
    assert headers['authorization']=='Bearer secret-key'
    assert headers['content-type']=='application/json'
    assert headers['accept']=='application/json'
    assert headers['user-agent']=='RentalSourcing/1.0'
    assert headers['idempotency-key']=='test-identity'
    assert json.loads(req.data)==json.loads(body)


@pytest.mark.parametrize('message,category',[
    ('You can only send testing emails to your own email address (private@example.test).','onboarding_recipient_restriction'),
    ('The example.test domain is not verified. Please, add and verify your domain.','sender_domain_not_verified')])
def test_documented_403_categories_without_persisting_raw_private_response(message,category):
    info=failure(error({'name':'validation_error','message':message,'api_key':'secret-key'}))
    assert info['category']==category and info['confirmed_rejection']
    assert 'private@example.test' not in json.dumps(info)
    assert 'secret-key' not in json.dumps(info)


def test_unknown_403_cannot_be_misclassified_as_bad_key_or_definite_rejection():
    info=failure(error(b'<html>403 edge denial secret-key</html>'))
    assert info['category']=='unclassified_http_error'
    assert not info['confirmed_rejection']
    assert info['response_kind']=='non_json'
    assert 'secret-key' not in json.dumps(info)


def test_secret_echo_in_request_id_is_removed(monkeypatch):
    monkeypatch.setenv('RESEND_API_KEY','re_secret')
    info=failure(error({'name':'restricted_api_key','message':'not active'},{'x-request-id':'re_secret'}))
    assert info['request_id']==''


def test_rejected_test_can_be_replaced_without_sending_or_deleting_history(store,boundary,settings,clock):
    provider=Provider();digest=Digest(store,boundary,settings,clock,provider)
    old=digest.prepare_test()
    provider.failures=[error({'name':'validation_error','message':'You can only send testing emails to your own email address (registered@example.test).'})]
    with pytest.raises(urllib.error.HTTPError):digest.send(old)
    assert store.execute('SELECT state FROM outbox WHERE id=?',(old,)).fetchone()[0]=='failed'
    new=digest.replace_rejected_test(old)
    assert new!=old
    assert digest.replace_rejected_test(old)==new
    assert store.execute('SELECT state FROM outbox WHERE id=?',(old,)).fetchone()[0]=='failed'
    assert store.execute('SELECT state FROM outbox WHERE id=?',(new,)).fetchone()[0]=='pending'
    assert len(provider.calls)==1 and provider.messages==[]
    assert digest.send(new)=='sent'
    assert digest.send(new)=='already_sent'
    assert len(provider.messages)==1


def test_unknown_403_stays_uncertain_and_cannot_be_rearmed(store,boundary,settings,clock):
    provider=Provider();digest=Digest(store,boundary,settings,clock,provider);old=digest.prepare_test()
    provider.failures=[error(b'edge forbidden')]
    with pytest.raises(urllib.error.HTTPError):digest.send(old)
    assert store.execute('SELECT state FROM outbox WHERE id=?',(old,)).fetchone()[0]=='uncertain'
    with pytest.raises(ReconciliationRequired):digest.replace_rejected_test(old)
    assert len(provider.calls)==1


def test_later_rejection_cannot_erase_prior_uncertain_attempt(store,boundary,settings,clock):
    provider=Provider();digest=Digest(store,boundary,settings,clock,provider);old=digest.prepare_test()
    provider.failures=[TimeoutError()]
    with pytest.raises(TimeoutError):digest.send(old)
    provider.failures=[error({'name':'validation_error','message':'The domain is not verified'})]
    with pytest.raises(urllib.error.HTTPError):digest.send(old)
    assert store.execute('SELECT state FROM outbox WHERE id=?',(old,)).fetchone()[0]=='uncertain'
    with pytest.raises(ReconciliationRequired):digest.replace_rejected_test(old)


def test_legacy_failed_record_needs_evidence_before_replacement(store,boundary,settings,clock):
    digest=Digest(store,boundary,settings,clock,Provider());old=digest.prepare_test()
    store.execute("UPDATE outbox SET state='failed' WHERE id=?",(old,))
    with pytest.raises(ReconciliationRequired):digest.replace_rejected_test(old)


def test_safe_inspection_reads_frozen_request_and_never_networks_or_exposes_secrets(store,boundary,settings,clock,monkeypatch):
    monkeypatch.setenv('RESEND_API_KEY','re_private')
    s=replace(settings,email_from='RentalSourcing <onboarding@resend.dev>')
    provider=Provider();digest=Digest(store,boundary,s,clock,provider);identity=digest.prepare_test()
    evidence=digest.inspect_request(identity)
    assert evidence['onboarding_sender'] and evidence['envelope_matches_config']
    assert evidence['recipient_count']==1
    assert evidence['endpoint']=='https://api.resend.com/emails'
    assert evidence['method']=='POST'
    assert 're_private' not in json.dumps(evidence)
    assert settings.email_to not in json.dumps(evidence)
    assert provider.calls==[]
