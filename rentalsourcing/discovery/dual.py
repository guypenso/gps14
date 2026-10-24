"""Versioned acquisition scenarios. Pure calculations; no provider calls or policy defaults."""
from dataclasses import asdict,replace
from decimal import Decimal
import math
from .finance import Input,ALLOWED,REQUIRED,underwriting

VERSION='dual-v1'
DUAL_ALLOWED=ALLOWED|{'closing_cost_rate','loan_closing_cost','loan_to_value'}
LOAN_FIELDS={'down_payment_rate','loan_to_value','interest_rate','loan_years','loan_closing_cost'}


def validate(inputs):
    if set(inputs)-DUAL_ALLOWED:raise ValueError('Unknown dual-underwriting input')
    for name,item in inputs.items():
        if not isinstance(item,Input):raise TypeError('Every scenario number needs provenance')
        if name in {'closing_cost_rate','loan_to_value'} and item.value>1:raise ValueError('Invalid cost rate or LTV')
        if name=='loan_to_value' and item.value==0:raise ValueError('Financed LTV must be positive')
    if {'closing_cost','closing_cost_rate'}<=inputs.keys():raise ValueError('Closing amount and rate are alternative representations, not additive')
    if {'down_payment_rate','loan_to_value'}<=inputs.keys():raise ValueError('Specify down payment OR LTV, not both')
    # Reuse the maintained engine's numeric/rate/term validation.
    underwriting(1,{k:v for k,v in inputs.items() if k in ALLOWED})


def _serialized(inputs):return {k:asdict(v) for k,v in inputs.items()}
def _rounded(metrics):return {k:round(float(v),6) if v is not None else None for k,v in metrics.items()}


def acquisition(price,inputs,*,strategy,loan_inputs=None):
    """Transaction costs exclude annual operating expenses/escrow; fees are paid in cash.
    Shared closing cost + renovation are counted once. Loan costs only affect financed cash.
    """
    if strategy not in {'cash','financed'}:raise ValueError('Unknown acquisition strategy')
    validate(inputs);loan_inputs=loan_inputs or {}
    if set(loan_inputs)-LOAN_FIELDS:raise ValueError('Only financing terms/costs belong in a loan profile')
    merged={**inputs,**loan_inputs};validate(merged)
    base={k:v for k,v in inputs.items() if k in ALLOWED and k not in LOAN_FIELDS}
    if 'closing_cost_rate' in inputs and isinstance(price,(int,float)) and not isinstance(price,bool) and math.isfinite(price) and price>0:
        item=inputs['closing_cost_rate'];base['closing_cost']=replace(item,value=float(Decimal(str(price))*Decimal(str(item.value))))
    legacy=underwriting(price,base,financing='cash')
    missing=sorted(set(legacy['missing'])|{name for name in REQUIRED if name not in base});m=dict(legacy['metrics'])
    m.update(purchase_price=price if isinstance(price,(int,float)) and not isinstance(price,bool) and math.isfinite(price) and price>0 else None,
             closing_cost=base['closing_cost'].value if 'closing_cost' in base else None,
             renovation_cost=base['renovation_budget'].value if 'renovation_budget' in base else None,
             monthly_gross_rent=None,annual_vacancy_loss=None,annual_gross_rent=None,annual_effective_rent=None,monthly_effective_rent=None,
             annual_operating_expenses=None,annual_capex_reserve=None,annual_after_reserve_cash_flow=None,
             total_property_capital=None,total_initial_cash=None,annual_net_cash_flow=None,
             down_payment=None,loan_principal=None,loan_to_value=None,interest_rate=None,loan_years=None,
             loan_closing_cost=None,dscr=None,annual_debt_service=None)
    if 'monthly_rent' in base:
        gross=Decimal(str(base['monthly_rent'].value))*12;m.update(annual_gross_rent=gross,monthly_gross_rent=base['monthly_rent'].value)
        if 'vacancy_rate' in base:
            collected=gross*(1-Decimal(str(base['vacancy_rate'].value)))
            m.update(annual_effective_rent=collected,monthly_effective_rent=collected/12,annual_vacancy_loss=gross-collected)
            if m.get('noi_annual') is not None:m['annual_operating_expenses']=collected-Decimal(str(m['noi_annual']))
        if 'capex_rate' in base:m['annual_capex_reserve']=gross*Decimal(str(base['capex_rate'].value))
    if m.get('noi_annual') is not None and m['annual_capex_reserve'] is not None:
        m['annual_after_reserve_cash_flow']=Decimal(str(m['noi_annual']))-m['annual_capex_reserve']
    if m['purchase_price'] is not None and m['closing_cost'] is not None and m['renovation_cost'] is not None:
        m['total_property_capital']=sum(Decimal(str(m[k])) for k in ('purchase_price','closing_cost','renovation_cost'))
    risks=[]
    if strategy=='cash':
        m['total_initial_cash']=m['total_property_capital'];m['annual_net_cash_flow']=m['annual_after_reserve_cash_flow'];m['annual_debt_service']=0
    else:
        # Never borrow old all-cash debt0/CoC/cash-flow values into an incomplete loan.
        for name in ('debt_service_monthly','cash_flow_monthly','cash_on_cash_pct'):m[name]=None
        missing += [name for name in ('interest_rate','loan_years','loan_closing_cost') if name not in merged]
        if 'down_payment_rate' not in merged and 'loan_to_value' not in merged:missing.append('down_payment_rate_or_loan_to_value')
        if m['purchase_price'] is not None and ('down_payment_rate' in merged or 'loan_to_value' in merged):
            ltv=Decimal(str(merged['loan_to_value'].value)) if 'loan_to_value' in merged else 1-Decimal(str(merged['down_payment_rate'].value))
            principal=Decimal(str(price))*ltv;down=Decimal(str(price))-principal
            m.update(loan_to_value=ltv,loan_principal=principal,down_payment=down)
            if down==0:risks.append('zero_down_payment')
            if 'loan_closing_cost' in merged:
                m['loan_closing_cost']=merged['loan_closing_cost'].value
                if m['closing_cost'] is not None and m['renovation_cost'] is not None:
                    m['total_initial_cash']=down+sum(Decimal(str(m[k])) for k in ('closing_cost','renovation_cost','loan_closing_cost'))
            if {'interest_rate','loan_years'}<=merged.keys():
                rate=Decimal(str(merged['interest_rate'].value))/12;months=int(merged['loan_years'].value)*12
                debt=principal/months if rate==0 else principal*rate/(1-(1+rate)**(-months))
                m.update(interest_rate=merged['interest_rate'].value,loan_years=merged['loan_years'].value,debt_service_monthly=debt,annual_debt_service=12*debt)
                if m.get('noi_annual') is not None and debt>0:m['dscr']=Decimal(str(m['noi_annual']))/(12*debt)
                if m['annual_after_reserve_cash_flow'] is not None:
                    annual=m['annual_after_reserve_cash_flow']-12*debt;m.update(annual_net_cash_flow=annual,cash_flow_monthly=annual/12)
                    if m['total_initial_cash'] is not None and m['total_initial_cash']>0:m['cash_on_cash_pct']=100*annual/m['total_initial_cash']
        if m['total_initial_cash']==0:missing.append('positive_initial_cash_investment_for_cash_on_cash')
    if m.get('cash_flow_monthly') is not None and m['cash_flow_monthly']<0:risks.append('negative_modeled_cash_flow')
    if m.get('dscr') is not None and m['dscr']<1:risks.append('modeled_noi_below_debt_service')
    used=merged if strategy=='financed' else {k:v for k,v in merged.items() if k not in LOAN_FIELDS}
    hypothetical=[k for k,v in used.items() if v.kind=='hypothetical']
    return dict(strategy=strategy,complete=not missing,missing=sorted(set(missing)),data_completeness_pct=max(0,round(100*(11+(4 if strategy=='financed' else 0)-len(set(missing)))/(15 if strategy=='financed' else 11),2)),metrics=_rounded(m),inputs=_serialized(used),
        assumptions=[k for k,v in used.items() if v.kind=='assumption'],estimates=[k for k,v in used.items() if v.kind=='third_party_estimate'],
        hypothetical=hypothetical,provisional=bool(missing or hypothetical),risks=risks,
        annual_return_metric='net_yield_pct' if strategy=='cash' else 'cash_on_cash_pct',
        definitions={'noi':'Effective rent minus operating expenses; excludes CapEx and debt',
                     'net_yield':'After-reserve unlevered income / purchase + shared closing + renovation; excludes loan fees',
                     'dscr':'Annual NOI / annual scheduled principal-and-interest debt service; before CapEx',
                     'cash_on_cash':'Annual after-reserve after-debt cash flow / initial cash, including loan closing costs'})


def merge_loan_profiles(profiles,overrides):
    """Explicit property quotes override the named shared scenario, never another strategy."""
    result={name:dict(values) for name,values in (profiles or {'base':{}}).items()}
    if set(overrides)-set(result):raise ValueError('Property quote references an unknown financing profile')
    for name,values in overrides.items():
        if set(values)-LOAN_FIELDS:raise ValueError('Invalid property financing quote')
        validate(values)
        if 'loan_to_value' in values:result[name].pop('down_payment_rate',None)
        if 'down_payment_rate' in values:result[name].pop('loan_to_value',None)
        result[name].update(values);validate(result[name])
    return result


def compare_acquisition(price,inputs,loan_scenarios=None):
    loans=loan_scenarios or {'base':{}}
    return dict(version=VERSION,cash=acquisition(price,inputs,strategy='cash'),financed={name:acquisition(price,inputs,strategy='financed',loan_inputs=terms) for name,terms in loans.items()})


def sensitivities(price,inputs,loan_scenarios=None):
    """Named hypothetical shocks; never become the base inputs or qualification policy."""
    results={};loans=loan_scenarios or {'base':{}}
    changes=[('rent_minus10','monthly_rent','multiply',.9),('rent_minus20','monthly_rent','multiply',.8),
             ('vacancy_plus5pp','vacancy_rate','add',.05),('maintenance_high','maintenance_rate','floor',.12),
             ('capex_high','capex_rate','floor',.10),('insurance_plus25','annual_insurance','multiply',1.25),
             ('renovation_plus25','renovation_budget','multiply',1.25)]
    for name,key,operation,value in changes:
        if key not in inputs:results[name]={'applies_to':'both','missing':[key],'hypothetical':True};continue
        old=inputs[key].value;new=old*value if operation=='multiply' else old+value if operation=='add' else max(value,old*1.25)
        delta={**inputs,key:Input(new,'hypothetical','Named stress test '+name)}
        try:results[name]={'applies_to':'both','hypothetical':True,'comparison':compare_acquisition(price,delta,loans)}
        except ValueError:results[name]={'applies_to':'both','hypothetical':True,'missing':['stress_value_outside_model_domain']}
    if {'maintenance_rate','capex_rate'}<=inputs.keys():
        delta={**inputs,**{key:Input(max(limit,inputs[key].value*1.25),'hypothetical','Combined maintenance/CapEx high case') for key,limit in [('maintenance_rate',.12),('capex_rate',.10)]}}
        try:results['maintenance_and_capex_high']={'applies_to':'both','hypothetical':True,'comparison':compare_acquisition(price,delta,loans)}
        except ValueError:results['maintenance_and_capex_high']={'applies_to':'both','hypothetical':True,'missing':['stress_value_outside_model_domain']}
    for closing_rate in (.02,.03,.05):
        # Review alternatives replace the base closing representation; never add it twice.
        delta={k:v for k,v in inputs.items() if k not in {'closing_cost','closing_cost_rate'}}
        delta['closing_cost_rate']=Input(closing_rate,'hypothetical','Proposed closing-cost review case')
        results[f'closing_cost_{int(closing_rate*100)}pct']={'applies_to':'both','hypothetical':True,'comparison':compare_acquisition(price,delta,loans)}
    for points in (1,2):
        changed={};missing=[]
        for name,terms in loans.items():
            rate=terms.get('interest_rate') or inputs.get('interest_rate')
            if rate is None:missing.append(name);continue
            stress={**terms,'interest_rate':Input(rate.value+points/100,'hypothetical',f'Rate +{points} percentage points')}
            try:changed[name]=acquisition(price,inputs,strategy='financed',loan_inputs=stress)
            except ValueError:missing.append(name)
        results[f'interest_plus{points}pp']={'applies_to':'financed_only','hypothetical':True,'financed':changed,'missing_profiles':missing}
    # Store base provenance once; each stress records only its changes and results.
    # This keeps daily PostgreSQL snapshots bounded without losing reproducibility.
    baseline=compare_acquisition(price,inputs,loans)
    def compact(scenario,base,reference):
        return {**{k:scenario[k] for k in ('strategy','complete','missing','metrics','provisional','risks','data_completeness_pct')},
                'baseline_reference':reference,'hypothetical':True,
                'changed_inputs':{k:v for k,v in scenario['inputs'].items() if base['inputs'].get(k)!=v},
                'removed_inputs':sorted(set(base['inputs'])-set(scenario['inputs']))}
    for result in results.values():
        if 'comparison' in result:
            comparison=result['comparison']
            comparison['cash']=compact(comparison['cash'],baseline['cash'],'acquisition.cash')
            comparison['financed']={name:compact(value,baseline['financed'][name],'acquisition.financed.'+name) for name,value in comparison['financed'].items()}
        elif 'financed' in result:
            result['financed']={name:compact(value,baseline['financed'][name],'acquisition.financed.'+name) for name,value in result['financed'].items()}
    return results
