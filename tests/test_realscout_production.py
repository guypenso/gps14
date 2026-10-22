"""Production integration boundaries. Every Receiving/RentCast/Resend transport is mocked."""
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import datetime as dt
from email.utils import format_datetime
import io
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit,parse_qs
from uuid import uuid4
import pytest
from cryptography.fernet import Fernet
from svix.webhooks import Webhook
from rentalsourcing.realscout_config import RealScoutSettings
from rentalsourcing.realscout_inbound import InboundPolicy,accept_notification
from rentalsourcing.realscout_worker import ReceivingClient,RetrievalFailure,run_once,claim,monitor
from rentalsourcing.realscout_store import LinkVault,ingest,decision
from rentalsourcing.realscout import parse_email,SEARCH
from rentalsourcing.realscout_enrichment import enrich
from rentalsourcing.realscout_delivery import prepare,send
from rentalsourcing.realscout_report import report
from rentalsourcing.realscout_runtime import daily
from rentalsourcing.dashboard import create_app
from rentalsourcing.storage import Store
from rentalsourcing.collector import Client
from rentalsourcing.digest import ReconciliationRequired
from conftest import Response,Provider,taxes


@pytest.fixture
def rsconfig():
    return RealScoutSettings(ingest_enabled=True,daily_enabled=True,approval_id='explicit-synthetic-only',
        policies=(InboundPolicy('alerts@example.invalid',('agent@example.invalid',)),),
        webhook_secret='whsec_'+base64.b64encode(b'only-a-synthetic-test-secret-123').decode(),
        receiving_key='synthetic-receiving-key',link_key=Fernet.generate_key().decode(),
        public_url='https://dashboard.example.invalid',allow_unvalidated_saved_search=True)


@pytest.fixture
def coreconfig(settings):return replace(settings,discovery_source='realscout')


def notification(cfg,clock,email_id=None):
    return dict(type='email.received',data=dict(email_id=email_id or str(uuid4()),
        created_at=clock().isoformat(),to=[cfg.policies[0].inbox],**{'from':cfg.policies[0].allowed_senders[0]}))


def sign(cfg,event):
    raw=json.dumps(event).encode();mid='msg_'+uuid4().hex;now=dt.datetime.now(dt.timezone.utc)
    return raw,{'svix-id':mid,'svix-timestamp':str(int(now.timestamp())),
                'svix-signature':Webhook(cfg.webhook_secret).sign(mid,now,raw.decode())}


def stage(store,cfg,clock,email_id=None):
    event=notification(cfg,clock,email_id);raw,headers=sign(cfg,event)
    accept_notification(store,raw,headers,signing_secret=cfg.webhook_secret,policy=cfg.policies[0])
    return event['data']['email_id']


def body(cfg,clock,email_id,*,street='101 Fixture St',price=180000,tax_status='Active'):
    return dict(id=email_id,created_at=clock().isoformat(),subject=SEARCH,
        to=[cfg.policies[0].inbox],authentication={'dmarc':'pass'},headers={'date':format_datetime(clock())},
        message_id='<'+email_id+'@example.invalid>',text='',
        html=f'''<html><body><h1>{SEARCH}</h1><div><p>New Listing</p><p>{street}, Pittsburgh, PA 15212</p>
        <p>${price:,}</p><p>3 Beds | 1 Bath | 1,200 Sq Ft</p><p>Listing status: {tax_status}</p><p>MLS # 1234567</p>
        <a href="https://client.realscout.com/private/SYNTHETIC_BEARER_TOKEN">View Details</a></div></body></html>''',
        **{'from':cfg.policies[0].allowed_senders[0]})


class Inbox:
    def __init__(self,messages):self.messages=messages;self.calls=[];self.failure=None
    def get(self,key):
        self.calls.append(key)
        if self.failure:raise self.failure
        return self.messages[key]


def seed(store,cfg,clock,*,street='101 Fixture St',price=180000):
    eid=stage(store,cfg,clock);mail=Inbox({eid:body(cfg,clock,eid,street=street,price=price)})
    assert run_once(store,cfg,client=mail,clock=clock)['processed']==1
    cid=store.execute('SELECT id FROM realscout_candidates WHERE address_key LIKE ?',('%'+street.split()[0]+'%',)).fetchone()[0]
    return cid,mail


class Records:
    def __init__(self,amount=1500):self.calls=[];self.amount=amount
    def open(self,request,timeout=None):
        self.calls.append(request)
        parts=urlsplit(request.full_url)
        assert parts.path=='/v1/properties','No bulk sale listing or AVM requests allowed'
        params=parse_qs(parts.query);assert params['limit']==['1']
        address=params['address'][0];record=dict(id='record-'+address.split()[0],formattedAddress=address,
            latitude=40.45,longitude=-80,propertyTaxes={'2025':{'year':2025,'total':self.amount}})
        return Response([record])


@pytest.fixture
def app(store,boundary,clock,rsconfig,monkeypatch):
    monkeypatch.setenv('DASHBOARD_PASSWORD','synthetic-dashboard-password-long')
    monkeypatch.setattr(RealScoutSettings,'from_env',classmethod(lambda cls:rsconfig))
    return create_app(store.test_url,testing=True,boundary=boundary,clock=clock)


def auth():return {'Authorization':'Basic '+base64.b64encode(b'owner:synthetic-dashboard-password-long').decode()}


def test_http_webhook_signed_and_duplicate_only_one_queue_entry(app,store,clock,rsconfig):
    raw,headers=sign(rsconfig,notification(rsconfig,clock));c=app.test_client()
    assert c.post('/webhooks/resend/inbound',data=raw,headers=headers).json['status']=='queued'
    assert c.post('/webhooks/resend/inbound',data=raw,headers=headers).json['status']=='duplicate'
    assert store.execute('SELECT COUNT(*) FROM realscout_inbound_jobs').fetchone()[0]==1
    assert store.execute('SELECT COUNT(*) FROM realscout_messages').fetchone()[0]==0


def test_http_reject_unsigned_tampered_wrong_sender_and_oversized(app,store,clock,rsconfig):
    raw,headers=sign(rsconfig,notification(rsconfig,clock));c=app.test_client()
    assert c.post('/webhooks/resend/inbound',data=raw).status_code==401
    assert c.post('/webhooks/resend/inbound',data=raw+b' ',headers={**headers,**auth()}).status_code==401
    event=notification(rsconfig,clock);event['data']['from']='attacker@example.invalid';raw,h=sign(rsconfig,event)
    assert c.post('/webhooks/resend/inbound',data=raw,headers=h).status_code==403
    assert c.post('/webhooks/resend/inbound',data=b'x'*131073).status_code==413
    assert store.execute('SELECT COUNT(*) FROM realscout_inbound_jobs').fetchone()[0]==0


def test_webhook_does_not_ack_failed_durable_write(app,clock,rsconfig,monkeypatch):
    def fail(*a,**k):raise RuntimeError('PRIVATE BODY SHOULD NEVER APPEAR')
    monkeypatch.setattr('rentalsourcing.realscout_web.accept_notification',fail)
    raw,h=sign(rsconfig,notification(rsconfig,clock))
    result=app.test_client().post('/webhooks/resend/inbound',data=raw,headers=h)
    assert result.status_code==503 and b'PRIVATE' not in result.data


def test_dashboard_and_detail_keep_basic_auth_and_private_links(app,store,clock,rsconfig):
    cid,_=seed(store,rsconfig,clock);c=app.test_client()
    for path in ['/api/realscout/status','/realscout/detail/'+cid,'/api/dashboard']:
        assert c.get(path).status_code==401
    response=c.get('/realscout/detail/'+cid,headers=auth(),follow_redirects=False)
    assert response.status_code==302 and response.headers['Location'].endswith('SYNTHETIC_BEARER_TOKEN')
    assert response.headers['Referrer-Policy']=='no-referrer'
    assert c.get('/api/realscout/status',headers=auth()).status_code==200


def test_worker_fetches_only_verified_ids_and_duplicate_is_noop(store,clock,rsconfig):
    cid,mail=seed(store,rsconfig,clock)
    assert len(mail.calls)==1
    assert run_once(store,rsconfig,client=mail,clock=clock)['processed']==0
    assert len(mail.calls)==1 and store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0


def test_worker_transient_retry_backoff_and_recovery(store,clock,rsconfig):
    eid=stage(store,rsconfig,clock);mail=Inbox({eid:body(rsconfig,clock,eid)})
    mail.failure=RetrievalFailure('receiving_http_429')
    assert run_once(store,rsconfig,client=mail,clock=clock)['retried']==1
    assert run_once(store,rsconfig,client=mail,clock=clock)['retried']==0
    clock.advance(seconds=31);mail.failure=None
    assert run_once(store,rsconfig,client=mail,clock=clock)['processed']==1
    assert store.execute('SELECT attempts,state FROM realscout_inbound_jobs').fetchone()==(2,'processed')


def test_worker_exhaustion_and_bad_auth_quarantine_without_content_logs(store,clock,rsconfig,caplog):
    eid=stage(store,rsconfig,clock);data=body(rsconfig,clock,eid);data['authentication']={}
    data['subject']='PRIVATE SUBJECT';mail=Inbox({eid:data})
    with caplog.at_level('INFO'):
        assert run_once(store,rsconfig,client=mail,clock=clock)['quarantined']==1
    assert 'PRIVATE SUBJECT' not in caplog.text and 'SYNTHETIC_BEARER_TOKEN' not in caplog.text
    assert run_once(store,rsconfig,client=mail,clock=clock)['processed']==0
    assert len(mail.calls)==1


def test_expired_lease_after_restart_is_recovered(store,clock,rsconfig):
    eid=stage(store,rsconfig,clock);claim(store,rsconfig,clock())
    clock.advance(seconds=rsconfig.lease_seconds+1)
    mail=Inbox({eid:body(rsconfig,clock,eid)})
    assert run_once(store,rsconfig,client=mail,clock=clock)['processed']==1
    assert store.execute('SELECT attempts FROM realscout_inbound_jobs').fetchone()[0]==2


def test_partial_commit_before_ack_is_idempotently_recovered(store,clock,rsconfig):
    from rentalsourcing.realscout_inbound import process_received
    eid=stage(store,rsconfig,clock);item=claim(store,rsconfig,clock());data=body(rsconfig,clock,eid)
    process_received(store,data,rsconfig.policies[0],authorized=True,vault=LinkVault(rsconfig.link_key),processed_at=clock().isoformat())
    clock.advance(seconds=rsconfig.lease_seconds+1)
    assert run_once(store,rsconfig,client=Inbox({eid:data}),clock=clock)['processed']==1
    assert store.execute('SELECT COUNT(*) FROM realscout_candidates').fetchone()[0]==1
    assert store.execute('SELECT COUNT(*) FROM realscout_observations').fetchone()[0]==1


def test_receiving_client_fixed_url_id_binding_and_no_redirect_secret_leak():
    eid=str(uuid4())
    class Opener:
        def open(self,r,timeout=None):
            assert r.full_url=='https://api.resend.com/emails/receiving/'+eid
            assert r.get_header('Authorization')=='Bearer synthetic-key'
            return Response({'id':str(uuid4())})
    with pytest.raises(RetrievalFailure):ReceivingClient('synthetic-key',opener=Opener()).get(eid)
    with pytest.raises(ValueError):ReceivingClient('synthetic-key').get('https://attacker.invalid')


def test_selective_record_lookup_qualifies_with_exact_original_tax_rule(store,boundary,clock,rsconfig,coreconfig):
    cid,_=seed(store,rsconfig,clock);provider=Records();cfg=replace(rsconfig,enrichment_enabled=True)
    client=Client(store,coreconfig,clock,provider,lambda _:None)
    result=enrich(store,boundary,coreconfig,cfg,clock=clock,client=client)
    assert result['requests']==1 and len(provider.calls)==1
    d=decision(store,cid,boundary,clock());assert d['state']=='Qualified' and d['tax']['adjusted']==1600
    r=report(store,boundary,clock())['rows'][0]
    assert r['score']==20 and r['price_per_sqft']==150 and r['gross_yield'] is None
    assert r['listing_id']=='1234567'
    assert store.execute('SELECT COUNT(*) FROM properties').fetchone()[0]==0
    assert enrich(store,boundary,coreconfig,cfg,clock=clock,client=Client(store,coreconfig,clock,provider,lambda _:None))['requests']==0
    assert len(provider.calls)==1


@pytest.mark.parametrize('amount,state',[(1899.99,'Qualified'),(1900,'Rejected')])
def test_enrichment_tax_boundary(store,boundary,clock,rsconfig,coreconfig,amount,state):
    cid,_=seed(store,rsconfig,clock);cfg=replace(rsconfig,enrichment_enabled=True)
    enrich(store,boundary,coreconfig,cfg,clock=clock,client=Client(store,coreconfig,clock,Records(amount),lambda _:None))
    assert decision(store,cid,boundary,clock())['state']==state


def test_enrichment_no_approval_never_goes_on_wire_and_bulk_always_blocked(store,boundary,clock,rsconfig,coreconfig):
    seed(store,rsconfig,clock);cfg=replace(rsconfig,enrichment_enabled=True);provider=Records()
    limited=replace(coreconfig,approved_requests=0)
    assert enrich(store,boundary,limited,cfg,clock=clock,client=Client(store,limited,clock,provider,lambda _:None))['requests']==0
    assert provider.calls==[]
    with pytest.raises(PermissionError):Client(store,coreconfig,clock,provider).get('/listings/sale',{})
    with pytest.raises(ValueError):Client(store,coreconfig,clock,provider).get('/properties',{'city':'Pittsburgh','limit':500})


def test_shared_outbox_daily_dedup_and_auth_links_no_raw_tokens(store,boundary,clock,rsconfig,coreconfig):
    seed(store,rsconfig,clock);provider=Provider()
    oid=prepare(store,boundary,coreconfig,rsconfig,clock().date().isoformat(),clock())
    frozen=json.loads(store.execute('SELECT payload FROM outbox WHERE id=?',(oid,)).fetchone()[0])
    assert 'SYNTHETIC_BEARER_TOKEN' not in json.dumps(frozen)
    assert 'https://dashboard.example.invalid/realscout/detail/' in frozen['text']
    assert send(store,boundary,coreconfig,rsconfig,oid,clock=clock,opener=provider)=='sent'
    assert prepare(store,boundary,coreconfig,rsconfig,clock().date().isoformat(),clock())==oid
    assert send(store,boundary,coreconfig,rsconfig,oid,clock=clock,opener=provider)=='already_sent'
    assert len(provider.messages)==1


def test_legacy_daily_send_prevents_second_source_report(store,boundary,clock,rsconfig,coreconfig):
    day=clock().date().isoformat()
    store.execute('INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?)',('legacy','daily:'+day,'sent','{}','[]','id',clock().isoformat(),clock().isoformat()))
    assert prepare(store,boundary,coreconfig,rsconfig,day,clock())=='legacy'
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==1


def test_failed_delivery_preserves_uncertain_body_and_key(store,boundary,clock,rsconfig,coreconfig):
    seed(store,rsconfig,clock);provider=Provider();provider.failures=[TimeoutError('simulated')]
    oid=prepare(store,boundary,coreconfig,rsconfig,clock().date().isoformat(),clock())
    original=store.execute('SELECT payload FROM outbox WHERE id=?',(oid,)).fetchone()[0]
    with pytest.raises(TimeoutError):send(store,boundary,coreconfig,rsconfig,oid,clock=clock,opener=provider)
    assert store.execute('SELECT state,payload FROM outbox WHERE id=?',(oid,)).fetchone()==('uncertain',original)
    clock.advance(hours=24)
    with pytest.raises(ReconciliationRequired):send(store,boundary,coreconfig,rsconfig,oid,clock=clock,opener=provider)
    assert len(provider.calls)==1


def test_price_change_cancels_unattempted_frozen_report(store,boundary,clock,rsconfig,coreconfig):
    seed(store,rsconfig,clock);oid=prepare(store,boundary,coreconfig,rsconfig,clock().date().isoformat(),clock())
    clock.advance(minutes=1);eid=stage(store,rsconfig,clock)
    run_once(store,rsconfig,client=Inbox({eid:body(rsconfig,clock,eid,price=170000)}),clock=clock)
    provider=Provider()
    assert send(store,boundary,coreconfig,rsconfig,oid,clock=clock,opener=provider)=='cancelled_stale'
    assert provider.calls==[]
    assert report(store,boundary,clock())['counts']['price_changes']==1


def test_stale_qualified_evidence_cannot_send_as_qualified(store,boundary,clock,rsconfig,coreconfig):
    seed(store,rsconfig,clock);cfg=replace(rsconfig,enrichment_enabled=True)
    enrich(store,boundary,coreconfig,cfg,clock=clock,client=Client(store,coreconfig,clock,Records(),lambda _:None))
    oid=prepare(store,boundary,coreconfig,cfg,clock().date().isoformat(),clock())
    clock.advance(hours=25);provider=Provider()
    assert send(store,boundary,coreconfig,cfg,oid,clock=clock,opener=provider)=='cancelled_stale'
    assert provider.calls==[]


def test_report_current_health_not_old_operations(store,boundary,clock,rsconfig,coreconfig):
    store.operation('old','OLD RESEND REJECTION ALREADY FIXED')
    oid=prepare(store,boundary,coreconfig,rsconfig,clock().date().isoformat(),clock())
    payload=store.execute('SELECT payload FROM outbox WHERE id=?',(oid,)).fetchone()[0]
    assert 'OLD RESEND' not in payload and 'No new alerts were ingested' in payload


def test_end_to_end_mocked_intake_enrichment_ranked_daily_delivery(store,boundary,clock,rsconfig,coreconfig):
    eid=stage(store,rsconfig,clock);receiving=Inbox({eid:body(rsconfig,clock,eid)})
    cfg=replace(rsconfig,enrichment_enabled=True);provider=Provider();record_provider=Records()
    result=daily(store,boundary,coreconfig,clock().date().isoformat(),rs_settings=cfg,clock=clock,
        receiving_client=receiving,rentcast_client=Client(store,coreconfig,clock,record_provider,lambda _:None),opener=provider)
    assert result['status']=='sent' and result['queue']['processed']==1 and result['enrichment']['requests']==1
    assert len(provider.messages)==1 and 'Score: 20.0' in provider.messages[0]['text']
    assert 'SYNTHETIC_BEARER_TOKEN' not in json.dumps(provider.messages[0])


def test_pg_concurrent_workers_claim_each_message_once(store,clock,rsconfig):
    if not store.pg:pytest.skip('Cross-session lease test requires disposable PostgreSQL')
    messages={}
    for _ in range(6):
        eid=stage(store,rsconfig,clock);messages[eid]=body(rsconfig,clock,eid)
    receiving=Inbox(messages)
    def task(_):
        s=Store(store.test_url,initialize=False)
        try:return run_once(s,rsconfig,client=receiving,clock=clock)
        finally:s.close()
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(task,range(4)))
    assert len(receiving.calls)==6 and len(set(receiving.calls))==6
    assert store.execute("SELECT COUNT(*) FROM realscout_inbound_jobs WHERE state='processed'").fetchone()[0]==6


def test_configuration_fails_closed_without_permission_or_validated_regular_sample(rsconfig):
    assert not RealScoutSettings().ingest_enabled
    with pytest.raises(ValueError):replace(rsconfig,approval_id='')
    with pytest.raises(ValueError):replace(rsconfig,allow_unvalidated_saved_search=False)
    with pytest.raises(ValueError):replace(rsconfig,public_url='http://not-secure.invalid')


def test_optional_status_lookup_is_per_property_and_shares_run_cap(store,boundary,clock,rsconfig,coreconfig):
    from rentalsourcing.realscout_enrichment import verify_active_status
    eid=stage(store,rsconfig,clock);data=body(rsconfig,clock,eid,tax_status='For Sale')
    run_once(store,rsconfig,client=Inbox({eid:data}),clock=clock)
    cid=store.execute('SELECT id FROM realscout_candidates').fetchone()[0]
    class Selective(Records):
        def open(self,req,timeout=None):
            if urlsplit(req.full_url).path=='/v1/listings/sale':
                self.calls.append(req);q=parse_qs(urlsplit(req.full_url).query)
                assert set(q)=={'address','limit','status'} and q['limit']==['1']
                return Response([dict(id='sale-1',formattedAddress=q['address'][0],status='Active',price=180000,bedrooms=3,bathrooms=1,squareFootage=1200)])
            return super().open(req,timeout)
    cfg=replace(rsconfig,enrichment_enabled=True,status_verification_enabled=True)
    settings=replace(coreconfig,selective_status_verification=True,per_run=2)
    provider=Selective();client=Client(store,settings,clock,provider,lambda _:None)
    enrich(store,boundary,settings,cfg,clock=clock,client=client)
    assert decision(store,cid,boundary,clock())['state']=='Pending Verification'
    verify_active_status(store,boundary,settings,cfg,clock=clock,client=client)
    assert len(provider.calls)==2 and client.count==2
    assert decision(store,cid,boundary,clock())['state']=='Qualified'
    # One-request cap cannot spend a second request for status confirmation.
    assert store.count_usage('approval:'+settings.approval_id)==2


def test_status_record_price_over_ceiling_rejects_despite_cheaper_email(store,boundary,clock,rsconfig,coreconfig):
    cid,_=seed(store,rsconfig,clock)
    record=dict(id='status-record',formattedAddress='101 Fixture St, Pittsburgh, PA 15212',status='Active',
                price=250000,bedrooms=3,bathrooms=1,squareFootage=1200)
    store.execute('INSERT INTO realscout_status_evidence VALUES (?,?,?,?,?)',
                  (cid,json.dumps(record),'rentcast-live',clock().isoformat(),(clock()+dt.timedelta(hours=24)).isoformat()))
    result=decision(store,cid,boundary,clock())
    assert result['state']=='Rejected' and 'current_listing_price' in result['reasons']


def test_crash_cached_property_response_recovers_at_zero_remaining_allowance(store,boundary,clock,rsconfig,coreconfig):
    cid,_=seed(store,rsconfig,clock);provider=Records()
    one=replace(coreconfig,approved_requests=1)
    client=Client(store,one,clock,provider,lambda _:None)
    client.get('/properties',{'address':'101 Fixture St, Pittsburgh, PA 15212','limit':1})
    assert store.execute('SELECT COUNT(*) FROM realscout_evidence').fetchone()[0]==0
    assert store.budget_health(one,clock())['remaining']==0
    # Resume through the real daily path: consume durable response cache, never repeat wire call.
    cfg=replace(rsconfig,enrichment_enabled=True)
    result=daily(store,boundary,replace(one,email_enabled=False),clock().date().isoformat(),rs_settings=cfg,clock=clock,
                 receiving_client=Inbox({}),rentcast_client=Client(store,one,clock,provider,lambda _:None))
    assert result['enrichment']['requests']==0 and len(provider.calls)==1
    assert decision(store,cid,boundary,clock())['state']=='Qualified'


def test_repeat_daily_after_accepted_send_does_not_retrieve_or_enrich(store,boundary,clock,rsconfig,coreconfig):
    cid,mail=seed(store,rsconfig,clock);provider=Provider()
    kwargs=dict(rs_settings=rsconfig,clock=clock,receiving_client=mail,opener=provider)
    assert daily(store,boundary,coreconfig,clock().date().isoformat(),**kwargs)['status']=='sent'
    before=len(mail.calls)
    assert daily(store,boundary,coreconfig,clock().date().isoformat(),**kwargs)['status']=='already_sent'
    assert len(mail.calls)==before and len(provider.calls)==1


@pytest.mark.parametrize('header,expected',[
    ('receiver.example.invalid; dkim=pass; dmarc=pass header.from=example.invalid',True),
    ('receiver.example.invalid;\r\n dmarc=pass header.from=example.invalid',True),
    ('attacker.invalid; dmarc=pass header.from=example.invalid',False),
    ('receiver.example.invalid; dmarc=fail header.from=example.invalid',False),
    ('receiver.example.invalid; dmarc=pass header.from=other.invalid',False),
    ('receiver.example.invalid; dmarc=pass',False),
    ('receiver.example.invalid; dmarc=pass header.from=example.invalid; dmarc=fail',False),
    ('receiver.example.invalid; dmarc=pass header.from=example.invalid\nAuthentication-Results: spoof',False),
])
def test_receiving_authentication_contract_is_explicit_and_aligned(header,expected):
    from rentalsourcing.realscout_inbound import trusted_authentication
    data={'from':'agent@example.invalid','headers':{'Authentication-Results':header}}
    assert bool(trusted_authentication(data,'receiver.example.invalid'))==expected
    assert not trusted_authentication(data,'')


def test_receiving_transport_never_accepts_undocumented_authentication_object():
    eid=str(uuid4())
    class Opener:
        def open(self,request,timeout=None):
            return Response({'id':eid,'from':'agent@example.invalid','authentication':{'dmarc':'pass'},
                'headers':{'Authentication-Results':'receiver.example.invalid; dmarc=pass header.from=example.invalid'}})
    assert ReceivingClient('synthetic',opener=Opener()).get(eid)['authentication']=={}
    assert ReceivingClient('synthetic',opener=Opener(),trusted_authserv_id='receiver.example.invalid').get(eid)['authentication']=={'dmarc':'pass'}


def test_worker_activation_requires_reviewed_provider_auth_contract(rsconfig):
    with pytest.raises(ValueError):replace(rsconfig,worker_enabled=True)
    assert replace(rsconfig,worker_enabled=True,trusted_authserv_id='receiver.example.invalid',
                   authentication_contract_approval='synthetic-only').worker_enabled


def test_unlinked_financial_plan_is_bounded_and_does_not_crash(store,boundary,clock,rsconfig,coreconfig):
    from rentalsourcing.realscout_store import enrichment_plan
    seed(store,rsconfig,clock)
    plan=enrichment_plan(store,boundary,coreconfig,clock())
    assert plan['executes_requests'] is False
    # Missing coordinates still requires verification; no invented provider ID.
    assert all(p['property_id'] is None for p in plan['planned'])


def test_first_alert_labeled_previous_price_is_both_new_and_price_change(store,boundary,clock,rsconfig):
    eid=stage(store,rsconfig,clock);data=body(rsconfig,clock,eid)
    data['html']=data['html'].replace('$180,000','Current price: $180,000 Previous price: $195,000')
    assert run_once(store,rsconfig,client=Inbox({eid:data}),clock=clock)['processed']==1
    row=report(store,boundary,clock())['rows'][0]
    assert row['new_to_system'] and row['price_changed'] and row['previous_price']==195000
    assert row['price_change_amount']==-15000


def test_private_candidate_report_is_authenticated_and_has_no_source_bearer_url(app,store,clock,rsconfig):
    seed(store,rsconfig,clock);c=app.test_client()
    assert c.get('/api/realscout/report').status_code==401
    response=c.get('/api/realscout/report',headers=auth())
    assert response.status_code==200 and response.json['counts']['total']==1
    assert 'SYNTHETIC_BEARER_TOKEN' not in response.get_data(as_text=True)
