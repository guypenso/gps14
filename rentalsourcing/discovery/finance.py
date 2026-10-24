"""Transparent underwriting with explicit provenance and NO default cost/rent assumptions."""
from dataclasses import dataclass
from decimal import Decimal,InvalidOperation
import math

REQUIRED=('monthly_rent','annual_tax','annual_insurance','hoa_monthly','vacancy_rate','maintenance_rate',
          'capex_rate','management_rate','closing_cost','renovation_budget')
RATES={'vacancy_rate','maintenance_rate','capex_rate','management_rate','down_payment_rate','interest_rate'}


@dataclass(frozen=True)
class Input:
    value:float
    kind:str
    source:str
    approval_id:str=''
    def __post_init__(self):
        if self.kind not in {'verified_source','third_party_estimate','assumption'} or not self.source:raise ValueError('Financial provenance required')
        if self.kind=='assumption' and not self.approval_id:raise PermissionError('Investment assumptions need explicit approval')
        if isinstance(self.value,bool) or not isinstance(self.value,(int,float)) or not math.isfinite(self.value) or self.value<0:raise ValueError('Invalid underwriting value')


def underwriting(price,inputs,*,financing=None):
    if price is None or isinstance(price,bool) or not isinstance(price,(int,float)) or not math.isfinite(price) or price<=0:return {'complete':False,'missing':['price'],'metrics':{}}
    for name,item in inputs.items():
        if not isinstance(item,Input):raise TypeError('Every underwriting number needs provenance')
        if name in RATES and item.value>=1:raise ValueError('Rates must be fractions below1')
    missing=[name for name in REQUIRED if name not in inputs]
    values={name:Decimal(str(item.value)) for name,item in inputs.items()}
    metrics={'gross_yield_pct':float(12*values['monthly_rent']/Decimal(str(price))*100) if 'monthly_rent' in values else None,
             'net_yield_pct':None,'noi_annual':None,'cash_flow_monthly':None,'cash_on_cash_pct':None,'debt_service_monthly':None}
    financial_missing=missing.copy()
    operating={'monthly_rent','annual_tax','annual_insurance','hoa_monthly','vacancy_rate','maintenance_rate','management_rate'}
    if operating<=values.keys():
        gross=12*values['monthly_rent'];collected=gross*(1-values['vacancy_rate'])
        noi=collected-values['annual_tax']-values['annual_insurance']-12*values['hoa_monthly']-gross*values['maintenance_rate']-collected*values['management_rate']
        metrics['noi_annual']=float(noi)
        if 'capex_rate' in values:
            after_reserves=noi-gross*values['capex_rate'];metrics['cash_flow_before_debt_monthly']=float(after_reserves/12)
            if {'closing_cost','renovation_budget'}<=values.keys():
                total=Decimal(str(price))+values['closing_cost']+values['renovation_budget']
                metrics['net_yield_pct']=float(100*after_reserves/total)
                if financing=='cash':debt=Decimal(0);invested=total
                elif financing=='loan' and {'down_payment_rate','interest_rate','loan_years'}<=values.keys():
                    years=values['loan_years']
                    if years<=0 or years>100 or years!=int(years):raise ValueError('Invalid loan term')
                    loan=Decimal(str(price))*(1-values['down_payment_rate']);rate=values['interest_rate']/12;months=years*12
                    debt=loan/months if rate==0 else loan*rate/(1-(1+rate)**(-int(months)))
                    invested=Decimal(str(price))*values['down_payment_rate']+values['closing_cost']+values['renovation_budget']
                else:
                    debt=None;invested=None
                    if financing=='loan':financial_missing.append('financing_mode_or_terms')
                if debt is not None:
                    metrics['debt_service_monthly']=float(debt);metrics['cash_flow_monthly']=float(after_reserves/12-debt)
                    metrics['cash_on_cash_pct']=float(100*(after_reserves-12*debt)/invested) if invested else None
    return dict(complete=not financial_missing,missing=sorted(set(financial_missing)),metrics={k:round(v,6) if v is not None else None for k,v in metrics.items()},
        inputs={k:dict(value=v.value,kind=v.kind,source=v.source,approval_id=v.approval_id) for k,v in inputs.items()},
        score=metrics['net_yield_pct'],score_units='Annual unlevered after-reserve yield percentage points; no ROI pass threshold invented',
        assumptions=[k for k,v in inputs.items() if v.kind=='assumption'],estimates=[k for k,v in inputs.items() if v.kind=='third_party_estimate'])


def scenarios(price,inputs,changes,*,financing=None):
    """Only explicitly supplied, approved changes; no hidden rent/cost perturbations."""
    return {name:underwriting(price,{**inputs,**delta},financing=financing) for name,delta in changes.items()}
