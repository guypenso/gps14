"""Synthetic-only RealScout adapter tests. No mailbox or provider network access."""
import base64
import copy
from dataclasses import replace
import datetime as dt
from email.message import EmailMessage
from email.parser import BytesParser
from email import policy as mail_policy
import hashlib
import json
from pathlib import Path
from uuid import uuid4
import pytest
from cryptography.fernet import Fernet
from svix.webhooks import Webhook
from rentalsourcing import realscout as parser
from rentalsourcing import realscout_store as rs
from rentalsourcing.realscout_inbound import InboundPolicy,accept_notification,process_received
from rentalsourcing.realscout_report import report,render_preview
from conftest import listing,taxes

FIXTURES=Path(__file__).parent/'fixtures/realscout'
NOW=dt.datetime(2026,10,22,5,tzinfo=dt.timezone.utc)
SINGLE='''<html><body><h1>RentalSourcing - Pittsburgh Under $200K</h1>
<div><p>New Listing</p><p>101 Fixture St<br>Pittsburgh, PA 15212</p>
<p>$175,000</p><p>3 Beds | 1.5 Baths | 1,200 Sq Ft</p>
<a href="https://client.realscout.com/private/SYNTHETIC_TOKEN">View Details</a></div></body></html>'''


def message(html=SINGLE,*,text='Synthetic fixture',identity='one',date='Thu, 22 Oct 2026 04:00:00 +0000',subject=parser.SEARCH):
    m=EmailMessage();m['From']='alerts@example.invalid';m['To']='inbox@example.invalid'
    m['Subject']=subject;m['Message-ID']=f'<synthetic-{identity}@example.invalid>'
    if date:m['Date']=date
    m.set_content(text)
    if html is not None:m.add_alternative(html,subtype='html')
    return m.as_bytes()


@pytest.fixture
def vault():return rs.LinkVault(Fernet.generate_key())


@pytest.fixture
def inbox(store):
    rs.migrate(store);return store


def import_one(inbox,vault,raw=None):
    parsed=parser.parse_email(raw or message())
    rs.ingest(inbox,parsed,received_at=NOW.isoformat(),authorized=True,vault=vault)
    return parsed.cards[0].candidate_id


def link(inbox,cid,now=NOW,**overrides):
    p=listing('cached-property',formattedAddress='101 Fixture St, Pittsburgh, PA 15212',**overrides)
    inbox.save_property(p['id'],p,'Pending Verification',[],rs.LIVE,1,now.isoformat())
    rs.link_cached_identity(inbox,cid,p['id'],now.isoformat())
    return p['id']


@pytest.mark.parametrize('name',['multipart','plain'])
def test_multiple_cards_fields_and_private_link_extraction(name):
    result=parser.parse_email((FIXTURES/(name+'.eml')).read_bytes())
    assert len(result.cards)==4 and not result.issues
    first=result.cards[0]
    assert (first.street,first.price,first.bedrooms,first.bathrooms,first.square_footage)==('101 Fixture St',175000,3,1.5,1200)
    assert first.detail_url.endswith('SYNTHETIC_PRIVATE_0')
    assert 'SYNTHETIC_PRIVATE_0' not in json.dumps(result.public())
    assert 'SYNTHETIC_PRIVATE_0' not in repr(first)


def test_mime_alternatives_not_double_counted():
    result=parser.parse_email((FIXTURES/'multipart.eml').read_bytes())
    assert len({c.candidate_id for c in result.cards})==4


def test_missing_fields_are_not_zero_or_borrowed_from_next_card():
    html=SINGLE.replace('$175,000','Unknown').replace('3 Beds','Beds unknown')
    result=parser.parse_email(message(html))
    assert result.cards[0].price is None and result.cards[0].bedrooms is None
    assert 'missing_or_ambiguous_price' in result.cards[0].issues


def test_multiple_prices_are_quarantined_as_unknown():
    c=parser.parse_email(message(SINGLE.replace('$175,000','$175,000 Previously $180,000'))).cards[0]
    assert c.price is None


def test_nested_layout_does_not_mix_two_properties():
    first=SINGLE.split('<div>')[1].split('</body>')[0]
    other=first.replace('101 Fixture St','202 Sample Ave').replace('$175,000','$199,000')
    result=parser.parse_email(message('<html><body><table><tr><td>'+first+'</td><td>'+other+'</td></tr></table></body></html>'))
    assert len(result.cards)==2
    assert {c.price for c in result.cards}=={175000,199000}


def test_responsive_duplicates_deduplicated():
    card=SINGLE[SINGLE.index('<div>'):SINGLE.index('</body>')]
    result=parser.parse_email(message('<html><body>'+card+card+'</body></html>'))
    assert len(result.cards)==1


def test_conflicting_same_address_cards_not_silently_merged():
    card=SINGLE[SINGLE.index('<div>'):SINGLE.index('</body>')]
    result=parser.parse_email(message('<html><body>'+card+card.replace('$175,000','$180,000')+'</body></html>'))
    assert result.cards==[] and 'conflicting_duplicate_cards' in result.issues


def test_unit_identity_and_suffix_normalization():
    a=parser.address_key('202 Sample Avenue Apt 2','Pittsburgh','PA','15207')
    assert a==parser.address_key('202 Sample Ave #2','Pittsburgh','PA','15207-1234')
    assert a!=parser.address_key('202 Sample Ave #3','Pittsburgh','PA','15207')


@pytest.mark.parametrize('url',['javascript:alert(1)','http://realscout.com/a','https://user:password@realscout.com/a','https://realscout.com:444/a'])
def test_unsafe_links_not_accepted(url):assert parser.safe_url(url) is None


def test_unknown_template_and_wrong_search_do_not_become_empty_success():
    result=parser.parse_email(message('<p>Unknown template</p>'))
    assert 'no_parseable_cards_template_review_required' in result.issues
    with pytest.raises(ValueError):parser.parse_email(message('<p>Other search</p>',subject='Other search'))


def test_no_remote_resources_or_scripts_are_executed(monkeypatch):
    def forbidden(*a,**k):raise AssertionError('No real network')
    monkeypatch.setattr('socket.create_connection',forbidden)
    c=parser.parse_email(message(SINGLE.replace('<body>','<body><script>fetch("https://bad.invalid")</script><img src="https://bad.invalid/pixel">'))).cards
    assert len(c)==1


def test_attached_originals_are_not_parsed_as_body():
    m=EmailMessage();m['Subject']=parser.SEARCH;m['Date']='Thu, 22 Oct 2026 04:00:00 +0000';m.set_content('No cards here')
    m.add_attachment(BytesParser(policy=mail_policy.default).parsebytes(message()))
    assert not parser.parse_email(m.as_bytes()).cards


def test_size_and_depth_bounded():
    with pytest.raises(ValueError):parser.parse_email(b'a'*(parser.MAX_BYTES+1))
    with pytest.raises(ValueError):parser.parse_email(message('<div>'*130+'x'+'</div>'*130))


def test_authorization_and_vault_required(inbox,vault):
    p=parser.parse_email(message())
    with pytest.raises(PermissionError):rs.ingest(inbox,p,received_at=NOW.isoformat())
    with pytest.raises(ValueError):rs.ingest(inbox,p,received_at=NOW.isoformat(),authorized=True)
    assert inbox.execute('SELECT COUNT(*) FROM realscout_messages').fetchone()[0]==0


def test_message_and_candidate_dedup(inbox,vault):
    p=parser.parse_email(message())
    assert not rs.ingest(inbox,p,received_at=NOW.isoformat(),authorized=True,vault=vault)['duplicate_message']
    assert rs.ingest(inbox,p,received_at=NOW.isoformat(),authorized=True,vault=vault)['duplicate_message']
    p2=parser.parse_email(message(identity='two',date='Thu, 22 Oct 2026 04:30:00 +0000'))
    result=rs.ingest(inbox,p2,received_at=NOW.isoformat(),authorized=True,vault=vault)
    assert result['candidates'][0]['discovery']=='previously_seen'
    assert inbox.execute('SELECT COUNT(*) FROM realscout_candidates').fetchone()[0]==1
    assert inbox.execute('SELECT COUNT(*) FROM realscout_observations').fetchone()[0]==2


def test_reused_message_id_different_facts_rolls_back(inbox,vault):
    import_one(inbox,vault)
    with pytest.raises(ValueError):rs.ingest(inbox,parser.parse_email(message(SINGLE.replace('$175,000','$170,000'))),received_at=NOW.isoformat(),authorized=True,vault=vault)
    assert inbox.execute('SELECT COUNT(*) FROM realscout_observations').fetchone()[0]==1


def test_price_change_version_and_review_history(inbox,vault):
    cid=import_one(inbox,vault);rs.mark_reviewed(inbox,cid,NOW.isoformat())
    p=parser.parse_email(message(SINGLE.replace('$175,000','$170,000'),identity='reduced',date='Thu, 22 Oct 2026 04:30:00 +0000'))
    result=rs.ingest(inbox,p,received_at=NOW.isoformat(),authorized=True,vault=vault)
    assert result['candidates'][0]['discovery']=='price_reduction'
    r=report(inbox,type('B',(),{'contains':lambda s,r:True})(),NOW)['rows'][0]
    assert r['previously_reviewed'] and not r['review_current']


def test_out_of_order_mail_cannot_revert_price(inbox,vault):
    cid=import_one(inbox,vault)
    old=parser.parse_email(message(SINGLE.replace('$175,000','$180,000'),identity='old',date='Wed, 21 Oct 2026 04:00:00 +0000'))
    result=rs.ingest(inbox,old,received_at=NOW.isoformat(),authorized=True,vault=vault)
    assert result['candidates'][0]['discovery']=='historical_observation'
    assert json.loads(inbox.execute('SELECT payload FROM realscout_candidates WHERE id=?',(cid,)).fetchone()[0])['price']==175000


def test_private_links_encrypted_and_report_redacted(inbox,vault,boundary):
    cid=import_one(inbox,vault)
    payload,cipher=inbox.execute('SELECT payload,detail_ciphertext FROM realscout_candidates WHERE id=?',(cid,)).fetchone()
    assert 'SYNTHETIC_TOKEN' not in payload+cipher
    assert vault.decrypt(cipher).endswith('SYNTHETIC_TOKEN')
    preview=render_preview(report(inbox,boundary,NOW))
    assert 'SYNTHETIC_TOKEN' not in json.dumps(preview)


def test_additive_schema_and_import_preserve_legacy_data(inbox,vault):
    inbox.execute('INSERT INTO usage VALUES (?,?)',('approval:keep',4))
    inbox.execute('INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?)',('keep','daily:keep','uncertain','{}','[]',None,NOW.isoformat(),NOW.isoformat()))
    inbox.save_property('legacy',listing('legacy'),'Qualified',[],rs.LIVE,3,NOW.isoformat())
    rs.migrate(inbox);import_one(inbox,vault)
    assert inbox.count_usage('approval:keep')==4
    assert inbox.get_property('legacy')['revision']==3
    assert inbox.execute('SELECT state FROM outbox WHERE id=?',('keep',)).fetchone()[0]=='uncertain'


def test_original_criteria_reject_excluded_zip_and_small_property(inbox,vault,boundary):
    p=parser.parse_email((FIXTURES/'multipart.eml').read_bytes())
    rs.ingest(inbox,p,received_at=NOW.isoformat(),authorized=True,vault=vault)
    states=[rs.decision(inbox,c.candidate_id,boundary,NOW) for c in p.cards]
    assert states[2]['state']=='Rejected' and 'excluded_zip' in states[2]['reasons']
    assert states[3]['state']=='Rejected' and 'bedrooms' in states[3]['reasons']
    assert states[0]['state']=='Pending Verification'


def test_email_new_label_is_not_active_or_city_boundary_proof(inbox,vault,boundary):
    cid=import_one(inbox,vault)
    result=rs.decision(inbox,cid,boundary,NOW)
    assert result['state']=='Pending Verification' and {'status','coordinates'}<=set(result['reasons'])


def test_verified_cached_identity_and_tax_reuse_no_requests(inbox,vault,boundary,provider):
    cid=import_one(inbox,vault);pid=link(inbox,cid)
    inbox.save_tax(pid,taxes(pid),rs.LIVE,NOW.isoformat(),(NOW+dt.timedelta(days=7)).isoformat())
    assert rs.decision(inbox,cid,boundary,NOW)['state']=='Qualified'
    assert provider.calls==[]


@pytest.mark.parametrize('amount,state',[(1899.99,'Qualified'),(1900,'Rejected'),(1900.01,'Rejected')])
def test_strict_original_tax_rule(inbox,vault,boundary,amount,state):
    cid=import_one(inbox,vault);pid=link(inbox,cid)
    inbox.save_tax(pid,taxes(pid,amount),rs.LIVE,NOW.isoformat(),(NOW+dt.timedelta(days=7)).isoformat())
    assert rs.decision(inbox,cid,boundary,NOW)['state']==state


def test_invalid_latest_tax_does_not_use_older_good_year(inbox,vault,boundary):
    cid=import_one(inbox,vault);pid=link(inbox,cid)
    t={'id':pid,'propertyTaxes':{'2024':{'year':2024,'total':1000},'2025':{'year':2025,'total':None}}}
    inbox.save_tax(pid,t,rs.LIVE,NOW.isoformat(),(NOW+dt.timedelta(hours=6)).isoformat())
    assert rs.decision(inbox,cid,boundary,NOW)['reasons']==['invalid_latest_tax_amount']


def test_stale_cached_listing_does_not_qualify_from_new_email(inbox,vault,boundary):
    cid=import_one(inbox,vault);link(inbox,cid,now=NOW-dt.timedelta(days=2))
    assert 'status' in rs.decision(inbox,cid,boundary,NOW)['reasons']


def test_ambiguous_cached_identity_does_not_link(inbox,vault):
    cid=import_one(inbox,vault);p=listing('a',formattedAddress='101 Fixture St, Pittsburgh, PA 15212')
    for pid in ['a','b']:inbox.save_property(pid,{**p,'id':pid},'Pending Verification',[],rs.LIVE,1,NOW.isoformat())
    with pytest.raises(ValueError):rs.link_cached_identity(inbox,cid,'a',NOW.isoformat())


def test_enrichment_plan_respects_existing_approval_and_makes_no_calls(inbox,vault,boundary,settings,provider):
    cid=import_one(inbox,vault);pid=link(inbox,cid)
    exhausted=replace(settings,approved_requests=0)
    plan=rs.enrichment_plan(inbox,boundary,exhausted,NOW)
    assert plan['planned']==[] and plan['max_attempts']==0
    plan=rs.enrichment_plan(inbox,boundary,settings,NOW)
    assert plan['planned'][0]['property_id']==pid and not plan['executes_requests']
    assert provider.calls==[] and inbox.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0


def test_daily_report_distinguishes_discovery_review_financial_and_alert_coverage(inbox,vault,boundary):
    cid=import_one(inbox,vault);link(inbox,cid);rs.mark_reviewed(inbox,cid,NOW.isoformat())
    r=report(inbox,boundary,NOW)
    assert r['counts']['new_listings']==1 and r['counts']['previously_reviewed']==1
    assert r['counts']['awaiting_financial_verification']==1
    assert r['coverage']=='alert_stream_only' and r['fresh_inventory_scan_performed'] is False
    body=render_preview(r)
    assert 'not a complete MLS' in body['text'] and 'PREVIEW' in body['subject']
    assert inbox.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0


def event_for(policy):
    return {'type':'email.received','data':{'email_id':str(uuid4()),'to':[policy.inbox],
            'created_at':dt.datetime.now(dt.timezone.utc).isoformat()}}


def signed(event,secret):
    raw=json.dumps(event).encode();event_id='msg_synthetic_'+uuid4().hex
    now=dt.datetime.now(dt.timezone.utc)
    headers={'svix-id':event_id,'svix-timestamp':str(int(now.timestamp())),
             'svix-signature':Webhook(secret).sign(event_id,now,raw.decode())}
    return raw,headers


@pytest.fixture
def inbound_policy():return InboundPolicy('inbox@example.invalid',('alerts@example.invalid',))


def test_signed_notification_replay_dedup_without_body_access(inbox,inbound_policy):
    secret='whsec_'+base64.b64encode(b'synthetic-secret-that-is-not-real').decode()
    event=event_for(inbound_policy);raw,headers=signed(event,secret)
    assert accept_notification(inbox,raw,headers,signing_secret=secret,policy=inbound_policy)['queued']
    assert accept_notification(inbox,raw,headers,signing_secret=secret,policy=inbound_policy)['duplicate']
    assert inbox.execute('SELECT COUNT(*) FROM realscout_inbound_jobs').fetchone()[0]==1


def test_webhook_tamper_expiry_and_wrong_recipient_rejected(inbox,inbound_policy):
    secret='whsec_'+base64.b64encode(b'synthetic-secret-that-is-not-real').decode()
    event=event_for(inbound_policy);raw,headers=signed(event,secret)
    with pytest.raises(Exception):accept_notification(inbox,raw+b' ',headers,signing_secret=secret,policy=inbound_policy)
    old=dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=1)
    headers['svix-timestamp']=str(int(old.timestamp()));headers['svix-signature']=Webhook(secret).sign(headers['svix-id'],old,raw.decode())
    with pytest.raises(Exception):accept_notification(inbox,raw,headers,signing_secret=secret,policy=inbound_policy)
    event['data']['to']=['other@example.invalid'];raw,headers=signed(event,secret)
    with pytest.raises(PermissionError):accept_notification(inbox,raw,headers,signing_secret=secret,policy=inbound_policy)
    assert inbox.execute('SELECT COUNT(*) FROM realscout_inbound_jobs').fetchone()[0]==0


def test_already_retrieved_body_requires_authorization_authentication_and_queue(inbox,vault,inbound_policy):
    data=dict(id=str(uuid4()),created_at=NOW.isoformat(),subject=parser.SEARCH,
              html=SINGLE,text='',headers={'date':'Thu, 22 Oct 2026 04:00:00 +0000'},
              message_id='<authorized-synthetic@example.invalid>',to=[inbound_policy.inbox],
              authentication={'dmarc':'pass'},**{'from':'alerts@example.invalid'})
    with pytest.raises(PermissionError):process_received(inbox,data,inbound_policy,vault=vault)
    with pytest.raises(PermissionError):process_received(inbox,data,inbound_policy,authorized=True,vault=vault)
    inbox.execute('INSERT INTO realscout_inbound_jobs (email_id,event_id,received_at,state,attempts) VALUES (?,?,?,?,0)',(data['id'],'synthetic-event',NOW.isoformat(),'pending'))
    with pytest.raises(PermissionError):process_received(inbox,{**data,'authentication':{}},inbound_policy,authorized=True,vault=vault)
    from realscout_auth_fixtures import FixtureVerifier
    inbox.execute('UPDATE realscout_inbound_jobs SET recipient_alias=? WHERE email_id=?',(inbound_policy.inbox,data['id']))
    result=process_received(inbox,data,inbound_policy,authorized=True,vault=vault,verification=FixtureVerifier().verify(data,inbound_policy))
    assert len(result['candidates'])==1
    assert inbox.execute('SELECT COUNT(*) FROM realscout_messages').fetchone()[0]==1


def test_demo_marked_cached_evidence_cannot_qualify_email_candidate(inbox,vault,boundary):
    cid=import_one(inbox,vault);link(inbox,cid,demo=True)
    result=rs.decision(inbox,cid,boundary,NOW)
    assert result['state']=='Pending Verification' and result['reasons']==['non_live_listing_source']


def test_parse_issues_quarantine_instead_of_partial_empty_ingest(inbox,vault):
    p=parser.parse_email(message('<div>Unsupported template</div>'))
    with pytest.raises(ValueError):rs.ingest(inbox,p,received_at=NOW.isoformat(),authorized=True,vault=vault)
    assert inbox.execute('SELECT COUNT(*) FROM realscout_messages').fetchone()[0]==0


def test_delayed_old_email_is_not_refreshed_by_arrival_time(inbox,vault,boundary):
    cid=import_one(inbox,vault,message(date='Tue, 20 Oct 2026 04:00:00 +0000'))
    assert 'stale_email_observation' in rs.decision(inbox,cid,boundary,NOW)['reasons']


def test_future_or_missing_message_date_cannot_refresh_candidate(inbox,vault):
    for date in ['Fri, 23 Oct 2026 04:00:00 +0000',None]:
        with pytest.raises(ValueError):rs.ingest(inbox,parser.parse_email(message(date=date)),received_at=NOW.isoformat(),authorized=True,vault=vault)
    assert inbox.execute('SELECT COUNT(*) FROM realscout_candidates').fetchone()[0]==0
