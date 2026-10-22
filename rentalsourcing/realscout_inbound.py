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
    """Retired: arbitrary/archived headers can never grant sender authenticity."""
    return {}


@dataclass(frozen=True)
class InboundPolicy:
    inbox: str
    allowed_senders: tuple[str,...]
    search_name: str = SEARCH
    alert_profile: str = 'saved_search'
    original_recipients: tuple[str,...] = ()

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


def process_received(store, data, policy, *, authorized=False, vault=None, processed_at=None, verification=None):
    """Accepts ALREADY retrieved provider data. Does not retrieve private email itself.
    The worker must bind data.id to a queued email_id before calling and mark its
    job processed only after this commit. Retries are safe via message identity.
    """
    if not authorized:raise PermissionError('Email ingestion is not enabled/authorized')
    from .realscout_auth import VerifiedEmail
    if not isinstance(verification,VerifiedEmail) or verification.email_id!=data.get('id'):
        raise PermissionError('Cryptographically verified original message required')
    if verification.sender not in {s.lower() for s in policy.allowed_senders}:
        raise PermissionError('Sender not allowlisted')
    job=store.execute('SELECT recipient_alias FROM realscout_inbound_jobs WHERE email_id=?',(data.get('id'),)).fetchone()
    if not job or job[0]!=policy.inbox.lower():raise PermissionError('Original message is not bound to the authorized receiving job')
    # Parse ONLY signature-verified raw MIME; provider HTML/text/header projections are not input.
    parsed=parse_email(verification.raw,policy.search_name,profile=policy.alert_profile)
    result=ingest(store,parsed,received_at=data['created_at'],authorized=True,vault=vault,processed_at=processed_at)
    # Queue ownership/state acknowledgement is the worker's responsibility.
    # This leaves the message commit replayable after a crash before ACK.
    store.execute('INSERT INTO realscout_auth_evidence VALUES (?,?,?,?,?,?,?) ON CONFLICT(email_id) DO NOTHING',
        (verification.email_id,verification.raw_sha256,verification.signing_domain,verification.selector,
         verification.verified_at,'aligned-full-message-dkim',parsed.message_key))
    return result
