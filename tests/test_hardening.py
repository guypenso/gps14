"""Production-boundary regressions. Fictional evidence and explicit fake transports only."""
from dataclasses import replace
import json
import pytest
from test_discovery import NOW,add,assumptions,complete_inputs,unified
from rentalsourcing.discovery.models import Source,normalize
from rentalsourcing.discovery.repository import ingest
from rentalsourcing.discovery.evaluate import evaluate
from rentalsourcing.discovery.finance import Input,underwriting
from rentalsourcing.discovery.config import DiscoverySettings
from rentalsourcing.discovery.report import scan_outcomes,render,snapshot
from rentalsourcing.storage import BudgetExceeded,Store
from scripts.hardening_simulation import simulate,recover

@pytest.mark.parametrize('field,value',[('monthly_rent',1800),('hoa_monthly',200)])
def test_conflicting_operating_income_or_hoa_cannot_qualify(store,boundary,field,value):
    mls=Source('fixture_mls','mls','fictional-license','fictional scope',True,True)
    eid=add(store,mls,EstimatedMonthlyRent=1500)
    weaker=Source('fixture_rc','rentcast','fictional-approval','fictional scope',True)
    item=normalize({'id':'conflict','formattedAddress':'101 Fixture St, Pittsburgh, PA 15212'},weaker,observed_at=NOW.isoformat(),style='rentcast')
    ingest(store,replace(item,fields={field:value}),NOW,authorized=True)
    result=evaluate(store,eid,boundary,NOW,assumptions=assumptions(),financing='cash')
    assert result['status']=='Pending Verification' and result['investment_score'] is None
    assert 'conflicting_'+field in result['reasons']


def test_financing_modes_unknown_cost_names_and_zero_equity_are_not_silently_approved():
    with pytest.raises(ValueError):underwriting(150000,complete_inputs(),financing='mystery')
    with pytest.raises(ValueError):underwriting(150000,{**complete_inputs(),'annual_insurence':Input(1200,'assumption','typo','fictional')})
    inputs=complete_inputs(closing_cost=0,renovation_budget=0)
    inputs.update({k:Input(v,'assumption','fictional-loan','fictional') for k,v in dict(down_payment_rate=0,interest_rate=.06,loan_years=30).items()})
    result=underwriting(150000,inputs,financing='loan')
    assert not result['complete'] and result['metrics']['cash_on_cash_pct'] is None


@pytest.mark.parametrize('kind,transport',[('mls','rentcast'),('rentcast','trestle'),('mls','typo')])
def test_source_configuration_rejects_silent_kind_transport_mismatch(kind,transport):
    source=Source('fixture',kind,'fictional','fictional scope',True)
    with pytest.raises(ValueError):DiscoverySettings(sources=({'source':source,'transport':transport},))


def test_report_scan_outcomes_are_independent_of_new_listing_count():
    rows=[{'source':'a','fresh_scan':True,'records':0}, {'source':'b','mode':'cached_provider_page'},
          {'source':'c','mode':'discovery_not_due'}, {'source':'d','error':'BudgetExceeded'},
          {'source':'e','error':'TimeoutError'}, {'source':'f','enrichment':{'errors':['approved_budget_unavailable']}},
          {'source':'g','fresh_scan':True,'all_pages_consumed':False}, {'source':'h','mode':'ingested_alert_bridge','records':0}]
    states=[r['state'] for r in scan_outcomes({'source_results':rows})]
    assert states==['fresh_discovery','cached_results_only','scan_skipped','internal_api_budget_exhausted','source_unavailable','internal_api_budget_exhausted','fresh_partial_discovery','no_new_alerts']


def test_daily_fallback_budget_allocation_stops_at40_without_provider_requests(store,settings):
    cfg=replace(unified(settings),approval_mode='billing_cycle',discovery_cap=31,enrichment_cap=3,verification_reserve=6,per_day=100)
    for purpose,count in [('discovery',31),('enrichment',3),('verification',6)]:
        for _ in range(count):store.reserve_api(cfg,NOW,purpose=purpose)
    assert store.budget_health(cfg,NOW)['used']==40
    with pytest.raises(BudgetExceeded):store.reserve_api(cfg,NOW,purpose='discovery')
    assert store.budget_health(cfg,NOW)['used']==40


def test_full_pipeline_fake_enrichment_outage_budget_and_reopened_database_recovery(store,boundary,tmp_path):
    checkpoint=simulate(store,boundary,tmp_path)
    report=json.loads((tmp_path/'report.json').read_text());body=(tmp_path/'report.html').read_text()
    assert report['synthetic'] and '<table' in body and 'https://listing.example.invalid/MLS-1001' in body
    assert report['properties'][0]['financial']['metrics']['net_yield_pct']==pytest.approx(7.269903,abs=1e-6)
    store.close()
    reopened=Store(store.test_url,initialize=False)
    try:assert recover(reopened,boundary,checkpoint)['duplicate_send_prevented']
    finally:reopened.close()


def test_high_estimated_tax_stays_pending_instead_of_hard_rejection(store,boundary):
    mls=Source('fixture_mls','mls','fictional-license','fictional scope',True,True)
    item=normalize(__import__('test_discovery').record(TaxAnnualAmount=2500),mls,observed_at=NOW.isoformat())
    ingest(store,replace(item,estimates=('annual_tax',)),NOW,authorized=True)
    result=evaluate(store,item.entity_id,boundary,NOW,assumptions=assumptions(),financing='cash')
    assert result['status']=='Pending Verification' and 'annual_tax_is_only_an_estimate' in result['reasons']


def test_fractional_tax_year_and_invalid_rate_cannot_pass_startup_validation():
    mls=Source('fixture_mls','mls','fictional-license','fictional scope',True,True)
    with pytest.raises(ValueError):normalize(__import__('test_discovery').record(TaxYear=2025.5),mls,observed_at=NOW.isoformat())
    with pytest.raises(ValueError):DiscoverySettings(assumptions={'vacancy_rate':Input(5,'assumption','wrong units','fictional')})
    with pytest.raises(ValueError):DiscoverySettings(assumptions={'loan_years':Input(0,'assumption','invalid term','fictional')})


def test_superseded_realscout_draft_does_not_block_bound_unified_report(store,boundary,settings):
    from rentalsourcing.discovery.report import prepare
    from rentalsourcing.digest import Digest,ReconciliationRequired
    from conftest import Provider
    mls=Source('fixture_mls','mls','fictional-license','fictional scope',True,True)
    add(store,mls,EstimatedMonthlyRent=1500)
    config=DiscoverySettings(enabled=True,daily_enabled=True,approval_id='fixture',sources=({'source':mls,'transport':'reso'},),assumptions=assumptions())
    s=unified(settings);oid=prepare(store,boundary,s,config,'2026-10-24',NOW)
    # Preserve prior unsent RS snapshot/history for the shared daily ID.
    store.execute('INSERT INTO realscout_reports VALUES (?,?,?,?,?)',(oid,'2026-10-24','{}',NOW.isoformat(),NOW.isoformat()))
    provider=Provider()
    assert Digest(store,boundary,s,lambda:NOW,provider,discovery_settings=config).send(oid)=='sent'
    assert len(provider.messages)==1 and store.execute('SELECT snapshot FROM realscout_reports').fetchone()[0]=='{}'


def test_tampered_report_body_is_not_accepted_under_a_different_snapshot(store,boundary,settings):
    from rentalsourcing.discovery.report import prepare
    from rentalsourcing.digest import Digest,ReconciliationRequired
    from conftest import Provider
    mls=Source('fixture_mls','mls','fictional-license','fictional scope',True,True);add(store,mls)
    cfg=DiscoverySettings(enabled=True,daily_enabled=True,approval_id='fixture',sources=({'source':mls,'transport':'reso'},))
    s=unified(settings);oid=prepare(store,boundary,s,cfg,'2026-10-24',NOW)
    row=json.loads(store.execute('SELECT payload FROM outbox WHERE id=?',(oid,)).fetchone()[0]);row['subject']='changed after preparation'
    store.execute('UPDATE outbox SET payload=? WHERE id=?',(json.dumps(row),oid));provider=Provider()
    with pytest.raises(ReconciliationRequired):Digest(store,boundary,s,lambda:NOW,provider,discovery_settings=cfg).send(oid)
    assert not provider.calls


def test_historical_stored_listing_import_is_not_a_new_daily_discovery(store,boundary):
    import datetime as dt
    mls=Source('fixture_mls','mls','fictional-license','fictional scope',True,True)
    old=NOW-dt.timedelta(days=2)
    row=__import__('test_discovery').record(ModificationTimestamp=old.isoformat())
    item=normalize(row,mls,observed_at=old.isoformat());ingest(store,item,NOW,authorized=True)
    data=snapshot(store,boundary,NOW)
    assert data['counts']['new']==0 and not data['properties'][0]['recommendation']
    assert data['properties'][0]['discovered_at']==old.isoformat()


def test_source_summary_keeps_enrichment_request_count():
    result=scan_outcomes({'source_results':[{'source':'fixture','enrichment':{'requests':3,'errors':[]}}]})
    assert result[0]['requests']==3 and result[0]['state']=='verification_only'
