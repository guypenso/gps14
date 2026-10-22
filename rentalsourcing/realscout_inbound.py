"""Signed notification boundary and independently authenticated email processing."""
from dataclasses import dataclass
import datetime as dt
from email.message import EmailMessage
from email.utils import format_datetime, parseaddr
import json
import re
from uuid import UUID
from .realscout import parse_email, SEARCH, MAX_BYTES
from .realscout_store import ingest, instant


def trusted_authentication(data, authserv_id):
    """Fail closed unless an explicitly approved receiver owns this header namespace.

    The configured authserv-id is NOT inferred from an email. Operators must first
    verify that Resend prepends/sanitizes this Authentication-Results namespace.
    Archived, sender-supplied and ARC headers never establish trust on their own.
    No assumption is made that the Receiving API exposes a top-level DMARC field.
    """
    if not authserv_id:return {}
    headers=data.get('headers')
    if not isinstance(headers,dict):return {}
    values=[v for k,v in headers.items() if k.lower()=='authentication-results']
    if len(values)!=1 or not isinstance(values[0],str):return {}
    value=re.sub(r'\r?\n[ \t]+',' ',values[0])
    if '\n' in value or '\r' in value:return {}
    parts=value.split(';')
    if parts[0].strip().lower()!=authserv_id.lower():return {}
    results=[p.strip() for p in parts[1:] if re.match(r'\s*dmarc\s*=',p,re.I)]
    if len(results)!=1 or not re.match(r'dmarc\s*=\s*pass\b',results[0],re.I):return {}
    domains=re.findall(r'\bheader\.from\s*=\s*([A-Za-z0-9.-]+)',results[0],re.I)
    sender=parseaddr(data.get('from',''))[1].lower()
    if len(domains)!=1 or '@' not in sender or domains[0].lower()!=sender.rsplit('@',1)[1]:return {}
    return {'dmarc':'pass'}


@dataclass(frozen=True)
class InboundPolicy:
    inbox: str
    allowed_senders: tuple[str,...]
    search_name: str = SEARCH
    alert_profile: str = 'saved_search'

    def __post_init__(self):
        if not self.inbox or not self.allowed_senders:raise ValueError('Exact inbox and sender allowlist required')
        if any('@' not in sender or '*' in sender for sender in self.allowed_senders):
            raise ValueError('Use exact sender addresses, not wildcard domains')
        if self.alert_profile not in {'saved_search','exclusive_alert'}:raise ValueError('Unknown alert profile')


def accept_notification(store, raw, headers, *, signing_secret, policy):
    if len(raw)>MAX_BYTES:raise ValueError('Webhook too large')
    from svix.webhooks import Webhook
    Webhook(signing_secret).verify(raw,headers)  # Raw bytes; library checks signature and replay time window.
    event=json.loads(raw)
    if event.get('type')!='email.received':raise ValueError('Unsupported webhook event')
    data=event['data'];email_id=str(UUID(data['email_id']))
    recipients=[parseaddr(v)[1].lower() for v in data.get('to',[])]
    if policy.inbox.lower() not in recipients:raise PermissionError('Unexpected receiving address')
    h={k.lower():v for k,v in headers.items()};event_id=h.get('svix-id')
    if not event_id:raise ValueError('Missing event ID')
    received=instant(data['created_at']).isoformat()
    with store.transaction():
        existing=store.execute('SELECT email_id FROM realscout_inbound_jobs WHERE event_id=?',(event_id,)).fetchone()
        if existing and existing[0]!=email_id:raise ValueError('Webhook identity collision')
        created=store.execute('INSERT INTO realscout_inbound_jobs (email_id,event_id,received_at,state,attempts,profile,recipient_alias,updated_at) VALUES (?,?,?,?,0,?,?,?) ON CONFLICT(email_id) DO NOTHING RETURNING email_id',
                              (email_id,event_id,received,'pending',policy.alert_profile,policy.inbox.lower(),received)).fetchone()
    return dict(queued=bool(created),duplicate=not bool(created),email_id=email_id)


def process_received(store, data, policy, *, authorized=False, vault=None, processed_at=None):
    """Accepts ALREADY retrieved provider data. Does not retrieve private email itself.
    The worker must bind data.id to a queued email_id before calling and mark its
    job processed only after this commit. Retries are safe via message identity.
    """
    if not authorized:raise PermissionError('Email ingestion is not enabled/authorized')
    sender=parseaddr(data.get('from',''))[1].lower()
    if sender not in {s.lower() for s in policy.allowed_senders}:raise PermissionError('Sender not allowlisted')
    if data.get('authentication',{}).get('dmarc')!='pass':
        raise PermissionError('Email authentication unverified; quarantine, including unsupported forwarding')
    recipients=[parseaddr(v)[1].lower() for v in data.get('to',[])]+[parseaddr(v)[1].lower() for v in data.get('received_for',[])]
    if policy.inbox.lower() not in recipients:raise PermissionError('Unexpected receiving address')
    job=store.execute('SELECT state FROM realscout_inbound_jobs WHERE email_id=?',(data.get('id'),)).fetchone()
    if not job:raise PermissionError('Email body is not bound to a verified queued notification')
    headers={k.lower():v for k,v in data.get('headers',{}).items()}
    msg=EmailMessage();msg['From']=data['from'];msg['Subject']=data.get('subject','')
    msg['Message-ID']=data.get('message_id') or '<resend-received-'+str(UUID(data['id']))+'>'
    if headers.get('date'):msg['Date']=headers['date']
    msg.set_content(data.get('text') or '')
    html=data.get('html')
    if html:
        if not isinstance(html,str) or html.startswith('data:'):
            raise ValueError('Unsupported HTML format; provider-specific decoding required')
        msg.add_alternative(html,subtype='html')
    parsed=parse_email(msg.as_bytes(),policy.search_name,profile=policy.alert_profile)
    result=ingest(store,parsed,received_at=data['created_at'],authorized=True,vault=vault,processed_at=processed_at)
    # Queue ownership/state acknowledgement is the worker's responsibility.
    # This leaves the message commit replayable after a crash before ACK.
    return result
