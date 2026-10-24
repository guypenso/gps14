"""Independent formula and policy regressions. All inputs/properties are fictional."""
from dataclasses import replace
import datetime as dt
import json,math
import pytest
from rentalsourcing.discovery.finance import Input
from rentalsourcing.discovery.dual import acquisition,compare_acquisition,sensitivities
from rentalsourcing.discovery.ranking import rank,percentile
from rentalsourcing.discovery.models import Source,normalize
from rentalsourcing.discovery.repository import ingest
from rentalsourcing.discovery.evaluate import evaluate
from rentalsourcing.discovery.config import DiscoverySettings
from rentalsourcing.discovery.report import snapshot,prepare,render,validate_frozen
from rentalsourcing.digest import Digest,ReconciliationRequired
from test_discovery import NOW,record,unified
from conftest import Provider


def source_input(value):return Input(value,'verified_source','fictional property-specific source')
def assumption(value):return Input(value,'assumption','explicit fictional scenario','FICTIONAL-ONLY')
def inputs():
    return {**{k:source_input(v) for k,v in dict(monthly_rent=1500,annual_tax=1500,annual_insurance=1200,hoa_monthly=0,renovation_budget=10000).items()},
            **{k:assumption(v) for k,v in dict(vacancy_rate=.05,maintenance_rate=.08,capex_rate=.05,management_rate=.10,closing_cost=4500).items()}}
def terms(**changes):
    return {k:assumption(v) for k,v in dict(dict(loan_to_value=.75,interest_rate=.06,loan_years=30,loan_closing_cost=3000),**changes).items()}


def test_cash_and_loan_independent_cash_accounting_and_consistent_noi():
    result=compare_acquisition(150000,inputs(),{'base':terms()});cash=result['cash']['metrics'];loan=result['financed']['base']['metrics']
    assert cash['annual_gross_rent']==18000 and cash['annual_effective_rent']==17100
    assert cash['annual_operating_expenses']==5850 and cash['noi_annual']==11250 and loan['noi_annual']==11250
    assert cash['annual_capex_reserve']==900 and cash['annual_after_reserve_cash_flow']==10350
    assert cash['total_initial_cash']==164500 and cash['cash_flow_monthly']==862.5
    assert cash['net_yield_pct']==pytest.approx(100*10350/164500,abs=1e-6)
    assert cash['cash_on_cash_pct']==cash['net_yield_pct']==loan['net_yield_pct']
    assert loan['down_payment']==37500 and loan['loan_principal']==112500 and loan['total_initial_cash']==55000
    assert loan['debt_service_monthly']==pytest.approx(674.49434,abs=1e-5)
    assert loan['cash_flow_monthly']==pytest.approx(188.00566,abs=1e-5)
    assert loan['cash_on_cash_pct']==pytest.approx(100*loan['annual_net_cash_flow']/55000,abs=1e-6)
    assert loan['dscr']==pytest.approx(11250/loan['annual_debt_service'],abs=1e-6)
    assert result['cash']['complete'] and result['financed']['base']['complete']


def test_loan_fees_count_once_in_equity_never_noi_or_unlevered_yield():
    a=acquisition(150000,inputs(),strategy='financed',loan_inputs=terms())
    b=acquisition(150000,inputs(),strategy='financed',loan_inputs=terms(loan_closing_cost=6000))
    for key in ['noi_annual','cash_flow_monthly','net_yield_pct','dscr']:assert a['metrics'][key]==b['metrics'][key]
    assert b['metrics']['total_initial_cash']-a['metrics']['total_initial_cash']==3000
    assert b['metrics']['cash_on_cash_pct']<a['metrics']['cash_on_cash_pct']


@pytest.mark.parametrize('rate',[.02,.03,.05])
def test_closing_rate_is_per_property_once_not_also_an_amount(rate):
    data=inputs();data.pop('closing_cost');data['closing_cost_rate']=assumption(rate)
    cash=acquisition(180000,data,strategy='cash');assert cash['metrics']['closing_cost']==180000*rate
    with pytest.raises(ValueError):acquisition(180000,{**data,'closing_cost':assumption(5400)},strategy='cash')


@pytest.mark.parametrize('ltv',[-.1,0,1.01])
def test_invalid_ltv_constraints(ltv):
    with pytest.raises(ValueError):acquisition(150000,inputs(),strategy='financed',loan_inputs=terms(loan_to_value=ltv))


def test_alternative_downpayment_not_double_representation_and_zero_interest_amortization():
    loan=terms(interest_rate=0);loan.pop('loan_to_value');loan['down_payment_rate']=assumption(.25)
    result=acquisition(150000,inputs(),strategy='financed',loan_inputs=loan)
    assert result['metrics']['debt_service_monthly']==312.5
    with pytest.raises(ValueError):acquisition(150000,inputs(),strategy='financed',loan_inputs={**loan,'loan_to_value':assumption(.75)})


def test_missing_loan_cost_or_reno_is_never_zero_cash_scenario_stays_independent():
    loan=terms();loan.pop('loan_closing_cost');result=compare_acquisition(150000,inputs(),{'base':loan})
    assert result['cash']['complete'] and not result['financed']['base']['complete']
    assert 'loan_closing_cost' in result['financed']['base']['missing'] and result['financed']['base']['metrics']['total_initial_cash'] is None
    missing=inputs();missing.pop('renovation_budget');result=compare_acquisition(150000,missing)
    assert not result['cash']['complete'] and result['cash']['metrics']['total_initial_cash'] is None
    assert result['financed']['base']['metrics']['cash_flow_monthly'] is None
    assert set(result['financed']['base']['missing'])>={'interest_rate','loan_years','loan_closing_cost','down_payment_rate_or_loan_to_value'}


def test_zero_equity_not_a_fabricated_infinite_return_and_negative_income_not_rejected():
    data=inputs();data.update(closing_cost=assumption(0),renovation_budget=source_input(0),monthly_rent=source_input(100))
    result=acquisition(150000,data,strategy='financed',loan_inputs=terms(loan_to_value=1,loan_closing_cost=0))
    assert not result['complete'] and result['metrics']['cash_on_cash_pct'] is None
    assert result['metrics']['cash_flow_monthly']<0 and 'negative_modeled_cash_flow' in result['risks']
    with pytest.raises(ValueError):acquisition(150000,{**data,'annual_maintenance':assumption(500)},strategy='cash')


def property_row(identity,cash_return,cash_flow,loan_return,loan_flow,*,complete=True):
    def sc(ret,flow,strategy):return dict(complete=complete,provisional=False,metrics=dict(net_yield_pct=ret if strategy=='cash' else 3,cash_on_cash_pct=ret,cash_flow_monthly=flow,total_initial_cash=10000))
    return dict(id=identity,address=identity,status='Qualified',original_screening='Qualified',conflicts={},acquisition={'cash':sc(cash_return,cash_flow,'cash'),'financed':{'base':sc(loan_return,loan_flow,'loan')}})


def test_balanced_percentiles_ties_singleton_outliers_and_separate_leverage_cohorts():
    rows=[property_row('a',4,1000,20,100),property_row('b',8,500,10,500),property_row('c',6,750,15,300)]
    result=rank(rows);assert {r['score'] for r in result['cash']}=={50}
    assert result['cash'][0]['annual_return_metric']=='net_yield_pct'
    assert result['financed']['base'][0]['annual_return_metric']=='cash_on_cash_pct'
    assert percentile(5,[5])==percentile(5,[5,5,5])==50
    assert percentile(1,[1,1,1000000])==25 and percentile(1000000,[1,1,1000000])==100
    assert rank([property_row('x',1,1,1,1,complete=False)])['cash']==[]
    with pytest.raises(ValueError):rank(rows,weights={'annual_return':1,'monthly_cash_flow':1})
    negative=rank([property_row('negative',-3,-100,-4,-200)])
    assert negative['cash'][0]['score']==50 and negative['cash'][0]['annual_return']==-3


def test_named_sensitivity_shocks_do_not_mutate_base_or_invent_missing_data():
    data=inputs();loans={'base':terms()};before=json.dumps({k:v.value for k,v in data.items()},sort_keys=True)
    result=sensitivities(150000,data,loans)
    base=compare_acquisition(150000,data,loans)
    for name in ['rent_minus10','rent_minus20','vacancy_plus5pp','maintenance_high','capex_high','insurance_plus25','renovation_plus25']:
        assert result[name]['hypothetical'] and result[name]['comparison']['cash']['metrics']['net_yield_pct']<base['cash']['metrics']['net_yield_pct']
    assert result['interest_plus1pp']['applies_to']=='financed_only' and 'comparison' not in result['interest_plus1pp']
    assert result['interest_plus2pp']['financed']['base']['metrics']['cash_flow_monthly']<result['interest_plus1pp']['financed']['base']['metrics']['cash_flow_monthly']
    assert json.dumps({k:v.value for k,v in data.items()},sort_keys=True)==before
    assert sensitivities(150000,{}, {})['rent_minus10']['missing']==['monthly_rent']


def setup_property(store,*,tax=1500,zip='15212'):
    source=Source('fixture_mls','mls','FICTIONAL-ONLY','fictional cohort',True,True)
    item=normalize(record(TaxAnnualAmount=tax,UnparsedAddress='101 Fixture St, Pittsburgh, PA '+zip,EstimatedMonthlyRent=1500),source,observed_at=NOW.isoformat())
    ingest(store,item,NOW,authorized=True);return source,item.entity_id


@pytest.mark.parametrize('tax,expected',[(1899.99,'Qualified'),(1900,'Rejected')])
def test_dual_tax_boundary_and_property_specific_evidence(store,boundary,tax,expected):
    source,eid=setup_property(store,tax=tax)
    data=inputs();specific={k:data.pop(k) for k in ['annual_insurance','renovation_budget']}
    result=evaluate(store,eid,boundary,NOW,assumptions=data,dual_mode=True,loan_scenarios={'base':terms()},property_inputs=specific)
    assert result['status']==expected and result['investment_score'] is None
    assert result['acquisition']['cash']['metrics']['noi_annual']==result['acquisition']['financed']['base']['metrics']['noi_annual']
    missing=evaluate(store,eid,boundary,NOW,assumptions=data,dual_mode=True)
    if tax<1900:assert missing['status']=='Pending Verification'


def test_config_requires_property_keys_approved_assumptions_and_named_primary_profile():
    with pytest.raises(PermissionError):DiscoverySettings(assumptions={'vacancy_rate':Input(.05,'hypothetical','review only')})
    with pytest.raises(ValueError):DiscoverySettings(financing_scenarios={'alternate':terms()})
    with pytest.raises(ValueError):DiscoverySettings(property_inputs={'unmatched address':inputs()})
    with pytest.raises(ValueError):DiscoverySettings(financing_scenarios={'base':{'annual_insurance':source_input(1200)}})


def test_dual_report_persistence_rendering_and_frozen_financing_change(store,boundary,settings):
    source,eid=setup_property(store);data=inputs();specific={k:data.pop(k) for k in ['annual_insurance','renovation_budget']}
    cfg=DiscoverySettings(enabled=True,daily_enabled=True,approval_id='FICTIONAL-ONLY',sources=({'source':source,'transport':'reso'},),assumptions=data,property_inputs={eid:specific},financing_scenarios={'base':terms()})
    s=unified(settings);oid=prepare(store,boundary,s,cfg,'2026-10-24',NOW)
    report=json.loads(store.execute('SELECT snapshot FROM discovery_reports WHERE outbox_id=?',(oid,)).fetchone()[0])
    assert report['underwriting_version']=='dual-v1' and report['properties'][0]['status']=='Qualified'
    body=render(report);assert all(x in body['html'] for x in ['ALL-CASH','FINANCED','Top Cash Opportunities','Top Financed Opportunities','DSCR','LTV','Hypothetical'])
    assert validate_frozen(store,boundary,oid,NOW,cfg)
    changed=replace(cfg,financing_scenarios={'base':terms(interest_rate=.07)})
    assert not validate_frozen(store,boundary,oid,NOW,changed)
    provider=Provider();assert Digest(store,boundary,s,lambda:NOW,provider,discovery_settings=cfg).send(oid)=='sent'
    assert Digest(store,boundary,s,lambda:NOW,provider,discovery_settings=cfg).send(oid)=='already_sent' and len(provider.messages)==1


def test_missing_hoa_cannot_be_approved_by_an_implicit_zero_or_missing_loan(store,boundary):
    source=Source('fixture_mls','mls','FICTIONAL-ONLY','fictional scope',True,True)
    raw=record(EstimatedMonthlyRent=1500);raw.pop('AssociationFee')
    item=normalize(raw,source,observed_at=NOW.isoformat());ingest(store,item,NOW,authorized=True)
    result=evaluate(store,item.entity_id,boundary,NOW,assumptions=inputs(),dual_mode=True,loan_scenarios={'base':terms()})
    assert result['status']=='Pending Verification' and 'verified_hoa_required' in result['reasons']


def test_closing_review_cases_replace_amount_and_combined_reserves_are_distinct():
    data=inputs();results=sensitivities(150000,data,{'base':terms()})
    assert [results[f'closing_cost_{v}pct']['comparison']['cash']['metrics']['closing_cost'] for v in (2,3,5)]==[3000,4500,7500]
    assert results['maintenance_and_capex_high']['comparison']['cash']['metrics']['cash_flow_monthly']<results['maintenance_high']['comparison']['cash']['metrics']['cash_flow_monthly']


def test_missing_price_does_not_imply_other_unknowns_are_complete():
    result=acquisition(None,{},strategy='cash')
    assert not result['complete'] and {'price','annual_tax','monthly_rent','renovation_budget'}<=set(result['missing'])
    assert result['data_completeness_pct']==0


def test_conflicting_rent_remains_pending_and_cannot_enter_either_ranking(store,boundary):
    from dataclasses import replace
    from rentalsourcing.discovery.report import snapshot
    source,eid=setup_property(store)
    weaker=Source('fixture_rc','rentcast','FICTIONAL-ONLY','fictional estimates',True)
    item=normalize({'id':'rent-2','formattedAddress':'101 Fixture St, Pittsburgh, PA 15212'},weaker,observed_at=NOW.isoformat(),style='rentcast')
    ingest(store,replace(item,fields={'monthly_rent':2000},estimates=('monthly_rent',)),NOW,authorized=True)
    data=inputs();specific={k:data.pop(k) for k in ['annual_insurance','renovation_budget']}
    report=snapshot(store,boundary,NOW,dual_mode=True,assumptions=data,property_inputs={eid:specific},loan_scenarios={'base':terms()})
    assert report['properties'][0]['status']=='Pending Verification'
    assert report['rankings']['cash']==report['rankings']['financed']['base']==[]


def test_historical_cash_snapshot_can_retry_under_new_config_without_rewriting_payload(store,boundary,settings):
    from test_discovery import cfg,assumptions
    source,eid=setup_property(store);old_cfg=cfg(source,assumptions=assumptions(),financing='cash');s=unified(settings)
    oid=prepare(store,boundary,s,old_cfg,'2026-10-24',NOW)
    old=store.execute('SELECT payload FROM outbox WHERE id=?',(oid,)).fetchone()[0]
    new_cfg=replace(old_cfg,dual_enabled=True,financing_scenarios={'base':terms()})
    assert validate_frozen(store,boundary,oid,NOW,new_cfg)
    assert store.execute('SELECT payload FROM outbox WHERE id=?',(oid,)).fetchone()[0]==old


def test_synthetic_dual_email_has_three_ranked_examples_and_no_approved_recommendations(tmp_path):
    from scripts.dual_underwriting_dry_run import generate
    data=generate(tmp_path)
    assert data['counts']['qualified']==data['counts']['recommendations']==0
    assert len(data['rankings']['cash'])==3 and len(data['rankings']['financed']['base'])==3
    assert data['synthetic'] and data['real_api_requests']==data['real_emails']==0
    body=(tmp_path/'report.html').read_text()
    assert 'EVERY PROPERTY AND VALUE IS FICTIONAL' in body and 'PROVISIONAL SCENARIO' in body
    assert '6.5000%' in body and '75.0000%' in body and 'N/A (cash)' in body
    assert all(row['provisional'] for row in data['rankings']['cash'])


def test_property_loan_quote_overrides_only_its_named_profile_and_is_validated():
    cfg=DiscoverySettings(financing_scenarios={'base':terms()},property_financing_scenarios={'property:fictional':{'base':{'interest_rate':source_input(.07),'loan_closing_cost':source_input(4200),'down_payment_rate':source_input(.30)}}})
    selected=cfg.loans_for('property:fictional')['base']
    assert selected['interest_rate'].value==.07 and selected['loan_closing_cost'].value==4200
    assert selected['down_payment_rate'].value==.30 and 'loan_to_value' not in selected
    assert cfg.loans_for('property:other')['base']['loan_to_value'].value==.75
    with pytest.raises(ValueError):DiscoverySettings(property_financing_scenarios={'property:fictional':{'unknown':terms()}})
    with pytest.raises(ValueError):DiscoverySettings(property_inputs={'property:fictional':{'loan_closing_cost':source_input(4200)}})


def test_unapproved_rent_hypothesis_cannot_become_verified_rental_evidence(store,boundary):
    source=Source('fixture_mls','mls','FICTIONAL','fictional scope',True,True)
    item=normalize(record(),source,observed_at=NOW.isoformat());ingest(store,item,NOW,authorized=True)
    data=inputs();data['monthly_rent']=assumption(1500);specific={k:data.pop(k) for k in ['annual_insurance','renovation_budget']}
    result=evaluate(store,item.entity_id,boundary,NOW,dual_mode=True,assumptions=data,property_inputs=specific,loan_scenarios={'base':terms()})
    assert result['status']=='Pending Verification' and 'supported_rent_evidence_required' in result['reasons']


def test_reviewed_hypothetical_live_scenario_stays_provisional_not_verified(store,boundary):
    source,eid=setup_property(store);data=inputs();specific={k:data.pop(k) for k in ['annual_insurance','renovation_budget']}
    specific['renovation_budget']=Input(10000,'hypothetical','Owner-reviewed what-if, not a condition quote','FICTIONAL-REVIEW-REFERENCE')
    cfg=DiscoverySettings(sources=({'source':source,'transport':'reso'},),assumptions=data,property_inputs={eid:specific},financing_scenarios={'base':terms()})
    result=evaluate(store,eid,boundary,NOW,dual_mode=True,assumptions=cfg.assumptions,property_inputs=cfg.property_inputs[eid],loan_scenarios=cfg.loans_for(eid))
    assert result['status']=='Pending Verification' and result['acquisition']['cash']['provisional']


@pytest.mark.parametrize('change',[
    {'ListPrice':200001},{'BedroomsTotal':2},{'BathroomsTotalInteger':0},{'LivingArea':999},
    {'UnparsedAddress':'101 Fixture St, Pittsburgh, PA 15203'},
    {'UnparsedAddress':'101 Fixture St, Pittsburgh, PA 15204'},
    {'UnparsedAddress':'101 Fixture St, Pittsburgh, PA 15210'},
    {'Latitude':40.7},{'StandardStatus':'Pending'}])
def test_original_hard_filters_still_reject_in_dual_mode(store,boundary,change):
    source=Source('fixture_mls','mls','FICTIONAL','fictional scope',True,True)
    item=normalize(record(EstimatedMonthlyRent=1500,**change),source,observed_at=NOW.isoformat());ingest(store,item,NOW,authorized=True)
    data=inputs();specific={k:data.pop(k) for k in ['annual_insurance','renovation_budget']}
    result=snapshot(store,boundary,NOW,dual_mode=True,assumptions=data,property_inputs={item.entity_id:specific},loan_scenarios={'base':terms()})
    assert result['properties'][0]['status']=='Rejected' and not result['rankings']['cash'] and not result['rankings']['financed']['base']


def test_dual_env_roundtrip_and_synthetic_delivery_guard(store,boundary,settings,monkeypatch):
    from dataclasses import asdict
    from rentalsourcing.discovery.models import normalize
    monkeypatch.setenv('UNIFIED_FINANCING_SCENARIOS_JSON',json.dumps({'base':{k:asdict(v) for k,v in terms().items()}}))
    monkeypatch.setenv('UNIFIED_RANKING_WEIGHTS_JSON','{"annual_return":0.5,"monthly_cash_flow":0.5}')
    cfg=DiscoverySettings.from_env()
    assert cfg.dual_enabled and not cfg.enabled and not cfg.daily_enabled and cfg.financing_scenarios['base']['interest_rate'].value==.06
    source=Source('fixture_mls','mls','FICTIONAL','fictional scope',True,True,True)
    item=normalize(record(EstimatedMonthlyRent=1500),source,observed_at=NOW.isoformat());ingest(store,item,NOW,authorized=True)
    data=inputs();specific={k:data.pop(k) for k in ['annual_insurance','renovation_budget']}
    active=replace(cfg,enabled=True,daily_enabled=True,approval_id='FICTIONAL',sources=({'source':source,'transport':'reso'},),assumptions=data,property_inputs={item.entity_id:specific})
    oid=prepare(store,boundary,unified(settings),active,'2026-10-24',NOW)
    with pytest.raises(PermissionError):Digest(store,boundary,unified(settings),lambda:NOW,Provider(),discovery_settings=active).send(oid)


def test_new_hypothetical_type_cannot_weaken_legacy_qualification(store,boundary):
    source,eid=setup_property(store);data=inputs();data['annual_insurance']=Input(1200,'hypothetical','fictional review')
    result=evaluate(store,eid,boundary,NOW,assumptions=data,financing='cash',dual_mode=False)
    assert result['status']=='Pending Verification' and 'hypothetical_financial_inputs' in result['reasons']


def test_stress_storage_keeps_only_explicit_deltas_with_full_base_reference():
    results=sensitivities(150000,inputs(),{'base':terms()})
    shock=results['rent_minus10']['comparison']['cash']
    assert 'inputs' not in shock and shock['baseline_reference']=='acquisition.cash'
    assert set(shock['changed_inputs'])=={'monthly_rent'} and shock['changed_inputs']['monthly_rent']['value']==1350
    closing=results['closing_cost_2pct']['comparison']['financed']['base']
    assert closing['removed_inputs']==['closing_cost'] and set(closing['changed_inputs'])=={'closing_cost_rate'}
