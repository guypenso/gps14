"""Fail-closed opt-in configuration; no account changes or network on import."""
from dataclasses import dataclass,field
import json
import os
from urllib.parse import urlsplit
from .realscout_inbound import InboundPolicy


def flag(name):
    v=os.getenv(name,'false').lower()
    if v not in {'true','false'}:raise ValueError('Invalid feature flag: '+name)
    return v=='true'


@dataclass(frozen=True)
class RealScoutSettings:
    ingest_enabled: bool=False
    daily_enabled: bool=False
    worker_enabled: bool=False
    enrichment_enabled: bool=False
    status_verification_enabled: bool=False
    approval_id: str=''
    policies: tuple=()
    webhook_secret: str=field(default='',repr=False)
    receiving_key: str=field(default='',repr=False)
    link_key: str=field(default='',repr=False)
    public_url: str=''
    max_jobs: int=20
    max_attempts: int=5
    lease_seconds: int=180
    poll_seconds: int=30
    message_limit: int=2_000_000
    allow_unvalidated_saved_search: bool=False
    raw_download_hosts: tuple=()
    trusted_authserv_id: str=''
    authentication_contract_approval: str=''

    def __post_init__(self):
        if any(not isinstance(n,int) or isinstance(n,bool) or n<=0 for n in
               (self.max_jobs,self.max_attempts,self.lease_seconds,self.poll_seconds,self.message_limit)):
            raise ValueError('Invalid RealScout worker limits')
        if self.max_jobs>100 or self.max_attempts>10 or self.message_limit>2_000_000:
            raise ValueError('RealScout processing limits exceed bounded maxima')
        if self.lease_seconds<90:raise ValueError('Worker lease must exceed bounded retrieval timeout')
        if self.status_verification_enabled and not self.enrichment_enabled:raise ValueError('Status verification requires separately enabled selective enrichment')
        if self.trusted_authserv_id or self.authentication_contract_approval:
            raise ValueError('Header-trust configuration retired; use verified original raw DKIM')
        if (self.worker_enabled or (self.ingest_enabled and self.daily_enabled)) and (not self.raw_download_hosts or any(not p.original_recipients for p in self.policies)):
            raise ValueError('Raw download host and original recipient policies required before worker activation')
        if any(not isinstance(h,str) or not h or h!=h.lower() or '/' in h or ':' in h or '*' in h or '.' not in h for h in self.raw_download_hosts):
            raise ValueError('Use exact lowercase provider storage hostnames')
        if self.raw_download_hosts:
            from .realscout_auth import raw_url
            for h in self.raw_download_hosts:raw_url('https://'+h+'/validate',self.raw_download_hosts)
        enabled=self.ingest_enabled or self.daily_enabled or self.worker_enabled or self.enrichment_enabled
        if enabled and not self.approval_id:raise ValueError('RealScout processing permission approval is required')
        if (self.worker_enabled or self.enrichment_enabled) and not self.ingest_enabled:
            raise ValueError('Worker/enrichment requires authorized ingestion')
        if self.ingest_enabled:
            if not self.policies or not self.webhook_secret or not self.receiving_key or not self.link_key:
                raise ValueError('RealScout inbox policies and private receiving credentials required')
            from .realscout_store import LinkVault
            LinkVault(self.link_key)
            from svix.webhooks import Webhook
            Webhook(self.webhook_secret)
            inboxes=[p.inbox.lower() for p in self.policies]
            if len(set(inboxes))!=len(inboxes):raise ValueError('Each inbox must have one explicit profile')
            if any(p.alert_profile=='saved_search' for p in self.policies) and not self.allow_unvalidated_saved_search:
                raise ValueError('Regular saved-search sample must be validated before activation')
        if self.daily_enabled:
            p=urlsplit(self.public_url)
            if p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment:
                raise ValueError('PUBLIC_APP_URL must be the authenticated HTTPS dashboard origin/base')

    @classmethod
    def from_env(cls):
        raw=os.getenv('REALSCOUT_INBOX_PROFILES','[]')
        try:
            profiles=json.loads(raw)
            if not isinstance(profiles,list):raise ValueError
            policies=tuple(InboundPolicy(inbox=p['inbox'],allowed_senders=tuple(p['allowed_senders']),
                search_name=p.get('search_name','RentalSourcing - Pittsburgh Under $200K'),
                alert_profile=p['profile'],original_recipients=tuple(p.get('original_recipients',[]))) for p in profiles)
        except (ValueError,KeyError,TypeError):raise ValueError('Invalid REALSCOUT_INBOX_PROFILES') from None
        return cls(ingest_enabled=flag('ENABLE_REALSCOUT_INGEST'),daily_enabled=flag('ENABLE_REALSCOUT_DAILY'),
            worker_enabled=flag('ENABLE_REALSCOUT_WORKER'),enrichment_enabled=flag('ENABLE_REALSCOUT_ENRICHMENT'),
            status_verification_enabled=flag('ENABLE_REALSCOUT_STATUS_VERIFICATION'),
            approval_id=os.getenv('REALSCOUT_PROCESSING_APPROVAL_ID',''),policies=policies,
            webhook_secret=os.getenv('RESEND_WEBHOOK_SECRET',''),receiving_key=os.getenv('RESEND_RECEIVING_API_KEY',''),
            link_key=os.getenv('REALSCOUT_LINK_KEY',''),public_url=os.getenv('PUBLIC_APP_URL','').rstrip('/'),
            max_jobs=int(os.getenv('REALSCOUT_WORKER_MAX_JOBS','20')),
            max_attempts=int(os.getenv('REALSCOUT_WORKER_MAX_ATTEMPTS','5')),
            lease_seconds=int(os.getenv('REALSCOUT_WORKER_LEASE_SECONDS','180')),
            poll_seconds=int(os.getenv('REALSCOUT_WORKER_POLL_SECONDS','30')),
            raw_download_hosts=tuple(json.loads(os.getenv('REALSCOUT_RAW_DOWNLOAD_HOSTS','[]'))),
            trusted_authserv_id=os.getenv('REALSCOUT_TRUSTED_AUTHSERV_ID',''),
            authentication_contract_approval=os.getenv('REALSCOUT_AUTH_CONTRACT_APPROVAL_ID',''),
            allow_unvalidated_saved_search=flag('REALSCOUT_SAVED_SEARCH_VALIDATED'))
