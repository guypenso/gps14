"""All new behavior opt-in. Secrets are env references, never stored source configuration."""
from dataclasses import dataclass,field
import json,os
from .models import Source
from .finance import Input,ALLOWED,underwriting


def flag(name):
    raw=os.getenv(name,'false').lower()
    if raw not in {'true','false'}:raise ValueError('Invalid feature flag '+name)
    return raw=='true'


@dataclass(frozen=True)
class DiscoverySettings:
    enabled:bool=False
    daily_enabled:bool=False
    export_webhook_enabled:bool=False
    approval_id:str=''
    sources:tuple=()
    assumptions:dict=field(default_factory=dict)
    financing:str|None=None
    rentcast_discovery:bool=False
    rentcast_enrichment:bool=False
    rentcast_status:bool=False
    rentcast_interval_days:int=7
    max_candidates:int=3
    reverify_limit:int=3
    def __post_init__(self):
        if self.daily_enabled or self.export_webhook_enabled:
            if not self.enabled:raise ValueError('Unified discovery must be enabled explicitly')
        if self.enabled and not self.approval_id:raise PermissionError('Unified processing permission required')
        if not 1<=self.rentcast_interval_days<=31 or not 0<=self.max_candidates<=20 or not 0<=self.reverify_limit<=20:raise ValueError('Bounded discovery settings required')
        if self.financing not in (None,'cash','loan'):raise ValueError('Unknown financing mode')
        if set(self.assumptions)-ALLOWED or any(not isinstance(v,Input) for v in self.assumptions.values()):raise ValueError('Invalid underwriting configuration')
        underwriting(1,self.assumptions,financing=self.financing)  # Validate units/rates before startup.
        transports={'mls':{'reso','trestle','csv_file','csv_webhook'},'rentcast':{'rentcast'},'realscout':{'realscout'},'public_records':{'wprdc'}}
        for profile in self.sources:
            if profile.get('transport') not in transports[profile['source'].kind]:raise ValueError('Source kind/transport mismatch')
            if profile['transport']=='csv_file' and not all(profile.get(k) for k in ('export_path','manifest_path')):raise ValueError('CSV file and snapshot manifest required')
            if profile['transport']=='csv_webhook' and not profile.get('secret_env'):raise ValueError('CSV signing-secret environment reference required')
        if len({p['source'].name for p in self.sources})!=len(self.sources):raise ValueError('Duplicate source name')
    @classmethod
    def from_env(cls):
        sources=[]
        for item in json.loads(os.getenv('UNIFIED_SOURCES_JSON','[]')):
            item=dict(item);contract=Source(**item.pop('source'))
            if set(item)-{'transport','base_url','token_url','expected_system','max_pages','secret_env','export_path','manifest_path','bearer_env'}:raise ValueError('Unknown source configuration field')
            sources.append(dict(item,source=contract))
        assumption_data=json.loads(os.getenv('UNIFIED_ASSUMPTIONS_JSON','{}'));assumptions={}
        for name,value in assumption_data.items():assumptions[name]=Input(**value)
        return cls(enabled=flag('ENABLE_UNIFIED_DISCOVERY'),daily_enabled=flag('ENABLE_UNIFIED_DAILY'),
            export_webhook_enabled=flag('ENABLE_MLS_EXPORT_WEBHOOK'),approval_id=os.getenv('UNIFIED_PROCESSING_APPROVAL_ID',''),
            sources=tuple(sources),assumptions=assumptions,financing=os.getenv('UNIFIED_FINANCING_MODE') or None,
            rentcast_discovery=flag('ENABLE_UNIFIED_RENTCAST_DISCOVERY'),rentcast_enrichment=flag('ENABLE_UNIFIED_RENTCAST_ENRICHMENT'),
            rentcast_status=flag('ENABLE_UNIFIED_STATUS_VERIFICATION'),rentcast_interval_days=int(os.getenv('UNIFIED_RENTCAST_INTERVAL_DAYS','7')),
            max_candidates=int(os.getenv('UNIFIED_MAX_ENRICHMENT_CANDIDATES','3')),reverify_limit=int(os.getenv('UNIFIED_MLS_REVERIFY_LIMIT','3')))
