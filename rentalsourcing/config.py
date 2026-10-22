"""Validated runtime settings; investment criteria remain fixed in core.py."""
from dataclasses import dataclass, asdict
import os

@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    email_enabled: bool = False
    environment: str = 'production'
    approval_id: str = ''
    approval_mode: str = 'one_time'
    approved_requests: int = 0
    per_run: int = 4
    per_day: int = 4
    per_month: int = 40
    page_size: int = 500
    max_pages: int = 20
    tax_cache_hours: int = 168
    listing_cache_minutes: int = 15
    timeout: int = 30
    retries: int = 0
    billing_day: int = 1
    billing_anchor: str = ''
    billing_baseline: int = -1
    listing_requests_per_day: int = 1
    negative_tax_cache_hours: int = 6
    email_approval_id: str = ''
    email_to: str = ''
    email_from: str = ''
    boundary_file: str = 'data/pittsburgh.geojson'
    discovery_source: str = 'rentcast'
    selective_status_verification: bool = False

    def __post_init__(self):
        if self.discovery_source not in {'rentcast','realscout'}:raise ValueError('Invalid DISCOVERY_SOURCE')
        for name in ('per_run','per_day','per_month','page_size','max_pages','tax_cache_hours','listing_cache_minutes','timeout','listing_requests_per_day','negative_tax_cache_hours'):
            if getattr(self,name) <= 0: raise ValueError(f'{name} must be positive')
        if self.approval_mode not in {'one_time','rolling_32_days','billing_cycle'}:raise ValueError('Invalid API approval mode')
        if self.approved_requests < 0 or self.page_size > 500 or self.retries not in range(3):
            raise ValueError('Invalid request limits')
        if not 1 <= self.billing_day <= 28: raise ValueError('BILLING_DAY must be 1..28 (use 28 for later billing dates conservatively)')
        if self.approval_mode=='billing_cycle':
            from .billing import billing_window
            billing_window(self)
            if self.billing_baseline < 0:raise ValueError('BILLING_USAGE_BASELINE must include current account usage before activation')
        if self.email_enabled and (self.environment != 'production' or not self.email_to or not self.email_from):
            raise ValueError('Email requires production environment and sender/recipient')

    @classmethod
    def from_env(cls):
        mapping={'discovery_source':'DISCOVERY_SOURCE','approval_mode':'API_APPROVAL_MODE','approval_id':'API_APPROVAL_ID','approved_requests':'APPROVED_API_REQUESTS',
          'per_run':'MAX_API_REQUESTS_PER_RUN','per_day':'MAX_API_REQUESTS_PER_DAY',
          'per_month':'MAX_API_REQUESTS_PER_MONTH','page_size':'LISTING_LIMIT',
          'max_pages':'MAX_LISTING_PAGES','tax_cache_hours':'TAX_CACHE_HOURS',
          'listing_cache_minutes':'LISTING_CACHE_MINUTES','timeout':'HTTP_TIMEOUT_SECONDS',
          'retries':'NETWORK_RETRIES','billing_day':'BILLING_DAY','billing_anchor':'BILLING_CYCLE_ANCHOR',
          'billing_baseline':'BILLING_USAGE_BASELINE','listing_requests_per_day':'LISTING_REQUESTS_PER_DAY',
          'negative_tax_cache_hours':'NEGATIVE_TAX_CACHE_HOURS','email_approval_id':'EMAIL_APPROVAL_ID','environment':'APP_ENV',
          'email_to':'EMAIL_TO','email_from':'EMAIL_FROM','boundary_file':'CITY_BOUNDARY_FILE'}
        defaults=cls(); values={}
        for field,env in mapping.items():
            default=getattr(defaults,field)
            values[field]=int(os.environ[env]) if isinstance(default,int) and env in os.environ else os.getenv(env,default)
        return cls(**values,enabled=os.getenv('ENABLE_RENTCAST')=='true',email_enabled=os.getenv('ENABLE_EMAIL')=='true',selective_status_verification=os.getenv('ENABLE_REALSCOUT_STATUS_VERIFICATION')=='true')

    def public(self):
        data=asdict(self)
        data.pop('email_to');data.pop('email_from')
        return data
