"""All new behavior opt-in. Secrets are env references, never stored source configuration."""
from dataclasses import dataclass,field
import json,os
from .models import Source
from .finance import Input,ALLOWED,underwriting
from .dual import validate as validate_dual,LOAN_FIELDS,merge_loan_profiles
from .ranking import validate_weights


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
    report_mode:str="mvp"
    dual_enabled:bool=False
    financing_scenarios:dict=field(default_factory=dict)
    property_inputs:dict=field(default_factory=dict)
    property_financing_scenarios:dict=field(default_factory=dict)
    ranking_weights:dict=field(default_factory=lambda:{'annual_return':.5,'monthly_cash_flow':.5})
    ranking_approval_id:str=''
    primary_financing_scenario:str='base'
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
        if self.report_mode not in {'mvp','advanced','legacy'}:raise ValueError('Unknown report mode')
        if self.report_mode=='advanced' and not self.dual_enabled:raise ValueError('Advanced reporting requires explicit dual underwriting flag')
        if self.financing not in (None,'cash','loan'):raise ValueError('Unknown financing mode')
        validate_dual(self.assumptions)
        validate_weights(self.ranking_weights)
        if len(self.financing_scenarios)>10:raise ValueError('At most10 named financing profiles')
        if self.financing_scenarios and self.primary_financing_scenario not in self.financing_scenarios:raise ValueError('Choose an explicit primary financing profile')
        import re
        for name,values in self.financing_scenarios.items():
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',name) or set(values)-LOAN_FIELDS:raise ValueError('Invalid financing profile')
            validate_dual({**self.assumptions,**values})
        for identity,values in self.property_inputs.items():
            if not isinstance(identity,str) or not identity.startswith('property:'):raise ValueError('Property assumptions require exact canonical entity ID')
            if set(values)&LOAN_FIELDS:raise ValueError('Use named property financing profiles for loan terms')
            validate_dual({**self.assumptions,**values})
        property_quotes=[]
        for identity,quotes in self.property_financing_scenarios.items():
            if not isinstance(identity,str) or not identity.startswith('property:'):raise ValueError('Property loan quotes require canonical entity ID')
            for values in merge_loan_profiles(self.financing_scenarios,quotes).values():validate_dual({**self.assumptions,**self.property_inputs.get(identity,{}),**values})
            property_quotes.extend(quotes.values())
        all_inputs=[self.assumptions,*self.financing_scenarios.values(),*self.property_inputs.values(),*property_quotes]
        if any(v.kind=='hypothetical' and not v.approval_id for group in all_inputs for v in group.values()) and not (self.sources and all(p['source'].synthetic for p in self.sources)):
            raise PermissionError('Hypothetical live-source scenarios need explicit owner review approval; no production defaults')
        transports={'mls':{'reso','trestle','csv_file','csv_webhook'},'rentcast':{'rentcast'},'realscout':{'realscout'},'public_records':{'wprdc'}}
        for profile in self.sources:
            if profile.get('transport') not in transports[profile['source'].kind]:raise ValueError('Source kind/transport mismatch')
            if profile['transport']=='csv_file' and not all(profile.get(k) for k in ('export_path','manifest_path')):raise ValueError('CSV file and snapshot manifest required')
            if profile['transport']=='csv_webhook' and not profile.get('secret_env'):raise ValueError('CSV signing-secret environment reference required')
        if len({p['source'].name for p in self.sources})!=len(self.sources):raise ValueError('Duplicate source name')
    def loans_for(self,identity):return merge_loan_profiles(self.financing_scenarios,self.property_financing_scenarios.get(identity,{}))
    @classmethod
    def from_env(cls):
        sources=[]
        for item in json.loads(os.getenv('UNIFIED_SOURCES_JSON','[]')):
            item=dict(item);contract=Source(**item.pop('source'))
            if set(item)-{'transport','base_url','token_url','expected_system','max_pages','secret_env','export_path','manifest_path','bearer_env'}:raise ValueError('Unknown source configuration field')
            sources.append(dict(item,source=contract))
        assumption_data=json.loads(os.getenv('UNIFIED_ASSUMPTIONS_JSON','{}'));assumptions={}
        for name,value in assumption_data.items():assumptions[name]=Input(**value)
        loans={name:{k:Input(**v) for k,v in values.items()} for name,values in json.loads(os.getenv('UNIFIED_FINANCING_SCENARIOS_JSON','{}')).items()}
        property_inputs={identity:{k:Input(**v) for k,v in values.items()} for identity,values in json.loads(os.getenv('UNIFIED_PROPERTY_INPUTS_JSON','{}')).items()}
        property_loans={identity:{name:{k:Input(**v) for k,v in values.items()} for name,values in profiles.items()} for identity,profiles in json.loads(os.getenv('UNIFIED_PROPERTY_FINANCING_JSON','{}')).items()}
        return cls(report_mode=os.getenv('UNIFIED_REPORT_MODE','mvp'),property_financing_scenarios=property_loans,dual_enabled=flag('ENABLE_DUAL_UNDERWRITING') if 'ENABLE_DUAL_UNDERWRITING' in os.environ else False,financing_scenarios=loans,property_inputs=property_inputs,ranking_weights=json.loads(os.getenv('UNIFIED_RANKING_WEIGHTS_JSON','{"annual_return":0.5,"monthly_cash_flow":0.5}')),ranking_approval_id=os.getenv('UNIFIED_RANKING_APPROVAL_ID',''),primary_financing_scenario=os.getenv('UNIFIED_PRIMARY_FINANCING_SCENARIO','base'),enabled=flag('ENABLE_UNIFIED_DISCOVERY'),daily_enabled=flag('ENABLE_UNIFIED_DAILY'),
            export_webhook_enabled=flag('ENABLE_MLS_EXPORT_WEBHOOK'),approval_id=os.getenv('UNIFIED_PROCESSING_APPROVAL_ID',''),
            sources=tuple(sources),assumptions=assumptions,financing=os.getenv('UNIFIED_FINANCING_MODE') or None,
            rentcast_discovery=flag('ENABLE_UNIFIED_RENTCAST_DISCOVERY'),rentcast_enrichment=flag('ENABLE_UNIFIED_RENTCAST_ENRICHMENT'),
            rentcast_status=flag('ENABLE_UNIFIED_STATUS_VERIFICATION'),rentcast_interval_days=int(os.getenv('UNIFIED_RENTCAST_INTERVAL_DAYS','7')),
            max_candidates=int(os.getenv('UNIFIED_MAX_ENRICHMENT_CANDIDATES','3')),reverify_limit=int(os.getenv('UNIFIED_MLS_REVERIFY_LIMIT','3')))
