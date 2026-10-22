"""Real RSA crypto over fictional MIME. DNS, downloads, Resend and mail sends are mocked."""
import base64
from dataclasses import replace
import datetime as dt
from email import policy
from email.message import EmailMessage
from email.utils import format_datetime
import hashlib
import io
import json
from unittest.mock import patch
from uuid import uuid4
import dkim
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from rentalsourcing.realscout_auth import RawDkimVerifier,DkimDNS,AuthFailure,CRITICAL,raw_url
from rentalsourcing.realscout_config import RealScoutSettings
from rentalsourcing.realscout_inbound import InboundPolicy,process_received,accept_notification
from rentalsourcing.realscout_worker import run_once
from rentalsourcing.realscout_store import LinkVault
from cryptography.fernet import Fernet
from svix.webhooks import Webhook


@pytest.fixture(scope='module')
def rsa_keys():
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    private=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.TraditionalOpenSSL,serialization.NoEncryption())
    public=key.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)
    return private,b'v=DKIM1; k=rsa; p='+base64.b64encode(public)


@pytest.fixture
def source():
    return InboundPolicy('exclusive@example.invalid',('agent@sender.example',),'RentalSourcing - Pittsburgh Under $200K',
        'saved_search',('owner@example.invalid',))


def make_message(clock,keys,*,signer=b'sender.example',signed=None,length=False,price=175000,identity=None,algorithm=b'rsa-sha256'):
    m=EmailMessage();m['From']='Agent <agent@sender.example>';m['To']='owner@example.invalid'
    m['Date']=format_datetime(clock());m['Subject']='RentalSourcing - Pittsburgh Under $200K'
    m['Message-ID']=identity or '<'+str(uuid4())+'@sender.example>'
    m.set_content(f'''RentalSourcing - Pittsburgh Under $200K
101 Fixture St, Pittsburgh, PA 15212
${price:,}
3 Beds | 1 Bath | 1,200 Sq Ft
Listing status: Active
View Details: https://client.realscout.com/private/SYNTHETIC
''')
    raw=m.as_bytes(policy=policy.SMTP)
    with patch('dkim.time.time',return_value=clock().timestamp()):
        sig=dkim.sign(raw,b'fixture',signer,keys[0],canonicalize=(b'relaxed',b'relaxed'),
            include_headers=[h.encode() for h in (signed or list(CRITICAL)+['content-transfer-encoding'])],length=length,signature_algorithm=algorithm)
    data=dict(id=str(uuid4()),message_id=str(m['Message-ID']),created_at=clock().isoformat(),
        to=['exclusive@example.invalid'],raw=dict(download_url='https://storage.example.invalid/original?SYNTHETIC_TOKEN=1',
        expires_at=(clock()+dt.timedelta(hours=1)).isoformat()),**{'from':str(m['From'])})
    return sig+raw,data


class BytesResponse(io.BytesIO):
    status=200;headers={}


class RawTransport:
    def __init__(self,raw,status=200):self.raw=raw;self.requests=[];self.status=status
    def open(self,req,timeout=None):
        self.requests.append(req)
        assert req.get_header('Authorization') is None
        assert req.get_header('Cookie') is None
        response=BytesResponse(self.raw);response.status=self.status;return response


def verifier(store,clock,keys,raw=None,**kwargs):
    def dns(name,timeout=5):
        assert name==b'fixture._domainkey.sender.example.'
        return keys[1]
    return RawDkimVerifier(store,('storage.example.invalid',),clock=clock,dnsfunc=dns,
        opener=RawTransport(raw) if raw is not None else None,**kwargs)


def cfg(source):
    return RealScoutSettings(ingest_enabled=True,worker_enabled=True,approval_id='synthetic-authorization',policies=(source,),
        raw_download_hosts=('storage.example.invalid',),webhook_secret='whsec_'+base64.b64encode(b'synthetic-webhook-secret-12345678').decode(),
        receiving_key='synthetic-only',link_key=Fernet.generate_key().decode(),allow_unvalidated_saved_search=True)


def queue(store,config,clock,data):
    event=dict(type='email.received',data=dict(email_id=data['id'],created_at=data['created_at'],to=[config.policies[0].inbox],**{'from':data['from']}))
    raw=json.dumps(event).encode();mid='msg_'+uuid4().hex;now=dt.datetime.now(dt.timezone.utc)
    headers={'svix-id':mid,'svix-timestamp':str(int(now.timestamp())),'svix-signature':Webhook(config.webhook_secret).sign(mid,now,raw.decode())}
    accept_notification(store,raw,headers,signing_secret=config.webhook_secret,policy=config.policies[0])
    return raw,headers


class Metadata:
    def __init__(self,data):self.data=data
    def get(self,identity):assert identity==self.data['id'];return self.data


def test_signed_original_worker_end_to_end_ignores_projected_html_and_forged_auth_headers(store,clock,rsa_keys,source):
    raw,data=make_message(clock,rsa_keys);settings=cfg(source);queue(store,settings,clock,data)
    data.update(html='FAKE $1 PRICE',authentication={'dmarc':'pass'},headers={'authentication-results':'forged; dmarc=pass'})
    result=run_once(store,settings,client=Metadata(data),verifier=verifier(store,clock,rsa_keys,raw),clock=clock)
    assert result['processed']==1
    payload=store.execute('SELECT payload FROM realscout_candidates').fetchone()[0]
    assert json.loads(payload)['price']==175000
    evidence=store.execute('SELECT raw_sha256,method FROM realscout_auth_evidence').fetchone()
    assert evidence==(hashlib.sha256(raw).hexdigest(),'aligned-full-message-dkim')
    assert 'SYNTHETIC_TOKEN' not in payload and 'DKIM-Signature' not in payload


@pytest.mark.parametrize('attack',['body','from','subject','date','message-id','duplicate-from','duplicate-subject','cte','unsigned-key','partial-body','wrong-domain','missing-signature','unsigned-date'])
def test_spoofing_or_signed_content_tampering_rejected(store,clock,rsa_keys,source,attack):
    raw,data=make_message(clock,rsa_keys,length=attack=='partial-body',
        signer=b'attacker.example' if attack=='wrong-domain' else b'sender.example',
        signed=[h for h in list(CRITICAL)+['content-transfer-encoding'] if h!='date'] if attack=='unsigned-date' else None)
    if attack=='body':raw=raw.replace(b'$175,000',b'$100,000')
    elif attack=='from':raw=raw.replace(b'agent@sender.example',b'attacker@sender.example')
    elif attack=='subject':raw=raw.replace(b'Subject: RentalSourcing',b'Subject: Forged RentalSourcing')
    elif attack=='date':raw=raw.replace(b'Date: ',b'Date: Fri, 23 Oct 2026 10:00:00 +0000 (',1)
    elif attack=='message-id':raw=raw.replace(data['message_id'].encode(),b'<attacker@sender.example>')
    elif attack=='duplicate-from':raw=b'From: agent@sender.example\r\n'+raw
    elif attack=='duplicate-subject':raw=b'Subject: forged\r\n'+raw
    elif attack=='cte':raw=raw.replace(b'Content-Transfer-Encoding: 7bit',b'Content-Transfer-Encoding: base64')
    elif attack=='missing-signature':raw=raw[raw.index(b'From:'):]
    if attack=='unsigned-key':
        bad=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        fake=b'v=DKIM1; p='+base64.b64encode(bad.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo))
        v=RawDkimVerifier(store,(),clock=clock,dnsfunc=lambda *a,**k:fake)
    else:v=verifier(store,clock,rsa_keys)
    data.update(authentication={'dmarc':'pass'},headers={'Authentication-Results':'mx.receiver; spf=pass; dkim=pass; dmarc=pass header.from=sender.example'})
    with pytest.raises(AuthFailure):v.verify_bytes(raw,data,source)


def test_signed_but_wrong_original_recipient_or_provider_binding_is_rejected(store,clock,rsa_keys,source):
    raw,data=make_message(clock,rsa_keys);v=verifier(store,clock,rsa_keys)
    with pytest.raises(AuthFailure):v.verify_bytes(raw,data,replace(source,original_recipients=('not-owner@example.invalid',)))
    with pytest.raises(AuthFailure):v.verify_bytes(raw,{**data,'message_id':'<wrong@example.invalid>'},source)
    with pytest.raises(AuthFailure):v.verify_bytes(raw,{**data,'from':'other@sender.example'},source)


def test_wrong_receiving_alias_cannot_use_received_for_header_to_gain_authority(store,clock,rsa_keys,source):
    raw,data=make_message(clock,rsa_keys);settings=cfg(source);queue(store,settings,clock,data)
    proof=verifier(store,clock,rsa_keys).verify_bytes(raw,data,source)
    store.execute('UPDATE realscout_inbound_jobs SET recipient_alias=?',('unauthorized@example.invalid',))
    data['received_for']=[source.inbox]
    with pytest.raises(PermissionError):process_received(store,data,source,authorized=True,vault=LinkVault(settings.link_key),verification=proof)
    assert store.execute('SELECT COUNT(*) FROM realscout_candidates').fetchone()[0]==0


def test_replayed_signed_message_and_duplicate_webhook_do_not_create_fresh_observation(store,clock,rsa_keys,source):
    raw,data=make_message(clock,rsa_keys);settings=cfg(source);event,headers=queue(store,settings,clock,data)
    assert accept_notification(store,event,headers,signing_secret=settings.webhook_secret,policy=source)['duplicate']
    def run():return run_once(store,settings,clock=clock,client=Metadata(data),verifier=verifier(store,clock,rsa_keys,raw))
    assert run()['processed']==1
    original=store.execute('SELECT first_seen,last_seen,version FROM realscout_candidates').fetchone()
    clock.advance(hours=1);data.update(id=str(uuid4()),created_at=clock().isoformat());data['raw']['expires_at']=(clock()+dt.timedelta(hours=1)).isoformat();queue(store,settings,clock,data)
    assert run()['processed']==1
    assert store.execute('SELECT first_seen,last_seen,version FROM realscout_candidates').fetchone()==original
    assert store.execute('SELECT COUNT(*) FROM realscout_messages').fetchone()[0]==1
    assert store.execute('SELECT COUNT(*) FROM realscout_observations').fetchone()[0]==1
    clock.advance(days=8)
    with pytest.raises(AuthFailure):verifier(store,clock,rsa_keys).verify_bytes(raw,data,source)


@pytest.mark.parametrize('url',['http://storage.example.invalid/raw','https://127.0.0.1/raw','https://storage.example.invalid.attacker.com/raw','https://storage.example.invalid:444/raw','https://user:pass@storage.example.invalid/raw','https://storage.example.invalid/raw#fragment','https://attacker.invalid/raw'])
def test_raw_url_ssrf_allowlist_is_exact_and_https_only(url):
    with pytest.raises(AuthFailure):raw_url(url,('storage.example.invalid',))


def test_missing_raw_no_header_based_fallback_and_download_size_or_redirect_rejected(store,clock,rsa_keys,source):
    raw,data=make_message(clock,rsa_keys)
    for altered in [{**data,'raw':None},{**data,'raw':{}},{**data,'raw':{'download_url':'https://storage.example.invalid/raw'}}]:
        with pytest.raises(AuthFailure):verifier(store,clock,rsa_keys,raw).verify(altered,source)
    for body,status in [(b'x'*2_000_001,200),(raw,302)]:
        v=RawDkimVerifier(store,('storage.example.invalid',),clock=clock,opener=RawTransport(body,status))
        with pytest.raises(AuthFailure):v.verify(data,source)


def test_forged_json_authentication_cannot_cross_processing_boundary(store,clock,source):
    data={'id':str(uuid4()),'authentication':{'dmarc':'pass','dkim':'pass'},'headers':{'Authentication-Results':'mx.example; dmarc=pass'}}
    with pytest.raises(PermissionError):process_received(store,data,source,authorized=True)


def test_dns_https_txt_key_cache_and_cname_chain(store,clock,rsa_keys):
    class DNS:
        calls=0
        def open(self,request,timeout=None):
            from urllib.parse import urlsplit,parse_qs
            p=urlsplit(request.full_url);assert p.hostname=='cloudflare-dns.com';q=parse_qs(p.query)
            import dns.message,dns.rrset
            query=dns.message.from_wire(base64.urlsafe_b64decode(q['dns'][0]+'='*((-len(q['dns'][0]))%4)))
            name=query.question[0].name.to_text().rstrip('.')
            self.calls+=1
            if name.startswith('fixture.'):
                answer=[dict(name=name,type=5,TTL=120,data='key.dkim.sender.example.')]
            else:answer=[dict(name=name,type=16,TTL=300,data=' '.join('"'+rsa_keys[1].decode()[i:i+200]+'"' for i in range(0,len(rsa_keys[1]),200)))]
            response=dns.message.make_response(query)
            for r in answer:response.answer.append(dns.rrset.from_text(r['name']+'.',r['TTL'],'IN','CNAME' if r['type']==5 else 'TXT',r['data']))
            return BytesResponse(response.to_wire())
    transport=DNS();resolver=DkimDNS(store,opener=transport,clock=clock)
    assert resolver(b'fixture._domainkey.sender.example.')==rsa_keys[1]
    calls=transport.calls
    assert resolver(b'fixture._domainkey.sender.example.')==rsa_keys[1] and transport.calls==calls
    clock.advance(seconds=121)
    assert resolver(b'fixture._domainkey.sender.example.')==rsa_keys[1] and transport.calls>calls


def test_dns_failure_retries_without_trusting_forged_headers(store,clock,rsa_keys,source):
    raw,data=make_message(clock,rsa_keys);config=cfg(source);queue(store,config,clock,data)
    data['headers']={'Authentication-Results':'forged; dkim=pass; dmarc=pass'}
    def unavailable(*a,**k):raise AuthFailure('dkim_dns_unavailable',True)
    v=RawDkimVerifier(store,('storage.example.invalid',),clock=clock,opener=RawTransport(raw),dnsfunc=unavailable)
    result=run_once(store,config,clock=clock,client=Metadata(data),verifier=v)
    assert result['retried']==1 and store.execute('SELECT COUNT(*) FROM realscout_candidates').fetchone()[0]==0


def test_lease_expiring_during_crypto_download_does_not_commit(store,clock,rsa_keys,source):
    raw,data=make_message(clock,rsa_keys);config=cfg(source);queue(store,config,clock,data)
    real=verifier(store,clock,rsa_keys)
    class Slow:
        def verify(self,data,policy):
            proof=real.verify_bytes(raw,data,policy);clock.advance(seconds=config.lease_seconds+1);return proof
    assert run_once(store,config,clock=clock,client=Metadata(data),verifier=Slow())['retried']==1
    assert store.execute('SELECT COUNT(*) FROM realscout_candidates').fetchone()[0]==0


def test_dns_response_must_match_query_and_malformed_packet_never_becomes_a_key(store,clock):
    class Bogus:
        def open(self,request,timeout=None):return BytesResponse(b'Authentication-Results: dkim=pass')
    with pytest.raises(AuthFailure):DkimDNS(store,opener=Bogus(),clock=clock)(b'fixture._domainkey.sender.example.')
    assert store.execute('SELECT COUNT(*) FROM realscout_dkim_keys').fetchone()[0]==0


def test_forwarder_added_authentication_headers_neither_grant_nor_remove_valid_dkim(store,clock,rsa_keys,source):
    raw,data=make_message(clock,rsa_keys)
    forwarded=(b'Received: from forwarder.example by receiving.example; Fri, 16 Oct 2026 12:00:00 +0000\r\n'
               b'Authentication-Results: attacker.example; dkim=fail; spf=pass; dmarc=pass\r\n'+raw)
    assert verifier(store,clock,rsa_keys).verify_bytes(forwarded,data,source).sender=='agent@sender.example'


def test_weak_rsa_key_cannot_authenticate_even_with_exact_sender_alignment(store,clock,source):
    private=rsa.generate_private_key(public_exponent=65537,key_size=1024)
    keys=(private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.TraditionalOpenSSL,serialization.NoEncryption()),
          b'v=DKIM1; p='+base64.b64encode(private.public_key().public_bytes(serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)))
    raw,data=make_message(clock,keys)
    with pytest.raises(AuthFailure):verifier(store,clock,keys).verify_bytes(raw,data,source)


def test_missing_raw_is_quarantined_without_content_or_url_leak(store,clock,rsa_keys,source,caplog):
    raw,data=make_message(clock,rsa_keys);config=replace(cfg(source),max_attempts=1);data['raw']=None;queue(store,config,clock,data)
    result=run_once(store,config,clock=clock,client=Metadata(data),verifier=verifier(store,clock,rsa_keys,raw))
    assert result['quarantined']==1
    assert store.execute('SELECT error_category FROM realscout_inbound_jobs').fetchone()[0]=='original_raw_message_unavailable'
    assert '$175,000' not in caplog.text and 'SYNTHETIC_TOKEN' not in caplog.text


def test_raw_body_delivery_delay_retries_then_verifies_one_observation(store,clock,rsa_keys,source):
    raw,data=make_message(clock,rsa_keys);available=data['raw'];data['raw']=None
    config=cfg(source);queue(store,config,clock,data)
    kwargs=dict(clock=clock,client=Metadata(data),verifier=verifier(store,clock,rsa_keys,raw))
    assert run_once(store,config,**kwargs)['retried']==1
    clock.advance(seconds=31);data['raw']=available
    assert run_once(store,config,**kwargs)['processed']==1
    assert store.execute('SELECT COUNT(*) FROM realscout_observations').fetchone()[0]==1


def test_committed_crypto_evidence_recovers_ack_without_private_refetch(store,clock,rsa_keys,source):
    raw,data=make_message(clock,rsa_keys);config=cfg(source);queue(store,config,clock,data)
    proof=verifier(store,clock,rsa_keys).verify_bytes(raw,data,source)
    process_received(store,data,source,authorized=True,vault=LinkVault(config.link_key),verification=proof)
    original=store.execute('SELECT first_seen,last_seen,version FROM realscout_candidates').fetchone()
    class NeverFetch:
        def get(self,*a):raise AssertionError('Committed authenticated messages must not be retrieved again')
    assert run_once(store,config,clock=clock,client=NeverFetch())['processed']==1
    assert store.execute('SELECT first_seen,last_seen,version FROM realscout_candidates').fetchone()==original
