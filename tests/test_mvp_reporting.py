"""Listing-only MVP policy, persistence and report regressions; fictional inputs only."""
import datetime as dt,json
from dataclasses import replace
import pytest
from rentalsourcing.discovery.config import DiscoverySettings
from rentalsourcing.discovery.evaluate import screen
from rentalsourcing.discovery.models import Source,normalize
from rentalsourcing.discovery.repository import ingest
from rentalsourcing.discovery.report import snapshot,render,prepare,validate_frozen
from test_discovery import NOW,record,unified


def add(store,**changes):
    source=Source('fixture_mls','mls','FICTIONAL','fictional cohort',True,True)
    item=normalize(record(**changes),source,observed_at=NOW.isoformat())
    ingest(store,item,NOW,authorized=True);return source,item


def test_default_mvp_and_advanced_disabled(monkeypatch):
    monkeypatch.delenv('UNIFIED_REPORT_MODE',raising=False);monkeypatch.delenv('ENABLE_DUAL_UNDERWRITING',raising=False)
    cfg=DiscoverySettings.from_env();assert cfg.report_mode=='mvp' and not cfg.dual_enabled
    assert not cfg.enabled and not cfg.daily_enabled and not cfg.rentcast_discovery
    with pytest.raises(ValueError):DiscoverySettings(report_mode='advanced')


@pytest.mark.parametrize('changes,status',[
    ({},'Qualified'),({'TaxAnnualAmount':1899.99},'Qualified'),({'TaxAnnualAmount':1900},'Rejected'),
    ({'TaxAnnualAmount':None},'Pending Verification'),({'TaxYear':None},'Pending Verification'),
    ({'ListPrice':200001},'Rejected'),({'BedroomsTotal':2},'Rejected'),({'BathroomsTotalInteger':0},'Rejected'),
    ({'LivingArea':999},'Rejected'),({'Latitude':40.7},'Rejected'),({'StandardStatus':'Pending'},'Rejected'),
    *[({'UnparsedAddress':'101 Fixture St, Pittsburgh, PA '+z},'Rejected') for z in ('15203','15204','15210')]])
def test_hard_rules_only_no_financial_inputs(store,boundary,changes,status):
    _,item=add(store,**changes);result=screen(store,item.entity_id,boundary,NOW)
    assert result['status']==status and 'financial' not in result and result['investment_score'] is None


def test_financial_conflict_does_not_block_but_hard_conflict_does(store,boundary):
    source,item=add(store,EstimatedMonthlyRent=1500)
    other=Source('other_mls','mls','FICTIONAL','fictional cohort',True,True)
    alt=normalize(record(EstimatedMonthlyRent=2000),other,observed_at=NOW.isoformat());ingest(store,alt,NOW,authorized=True)
    assert screen(store,item.entity_id,boundary,NOW)['status']=='Qualified'
    later=NOW+dt.timedelta(seconds=1)
    ingest(store,replace(alt,fields={**alt.fields,'bedrooms':4},observed_at=later.isoformat(),modified_at=later.isoformat()),later,authorized=True)
    assert screen(store,item.entity_id,boundary,later)['status']=='Pending Verification'


def test_report_dedup_price_changes_freshness_and_format(store,boundary):
    source,item=add(store)
    assert ingest(store,item,NOW,authorized=True)['duplicate']
    later=NOW+dt.timedelta(hours=1)
    ingest(store,replace(item,fields={**item.fields,'price':145000},observed_at=later.isoformat(),modified_at=later.isoformat()),later,authorized=True)
    data=snapshot(store,boundary,later,report_mode='mvp',since=NOW.isoformat());p=data['properties'][0]
    assert data['counts']['total']==1 and p['price_reduction'] and not p['new'] and p['status']=='Qualified'
    data['source_results']=[{'source':source.name,'error':'BudgetExceeded'}]
    body=render(data)
    assert 'Qualifying price reductions (1)' in body['text'] and 'internal api budget exhausted' in body['text']
    for forbidden in ['DSCR','cash-on-cash','Net yield','Financial metrics','Investment score','FINANCED']:
        assert forbidden not in body['text'] and forbidden not in body['html']
    assert '2025' in body['text'] and item.url in body['html'] and 'viewport' in body['html']


def test_mvp_frozen_payload_ignores_financial_changes_but_not_hard_evidence(store,boundary,settings):
    source,item=add(store)
    cfg=DiscoverySettings(enabled=True,daily_enabled=True,approval_id='FICTIONAL',sources=({'source':source,'transport':'reso'},))
    oid=prepare(store,boundary,unified(settings),cfg,'2026-10-24',NOW)
    payload=store.execute('SELECT payload FROM outbox WHERE id=?',(oid,)).fetchone()[0]
    assert validate_frozen(store,boundary,oid,NOW,cfg)
    from rentalsourcing.discovery.finance import Input
    changed=replace(cfg,assumptions={'annual_insurance':Input(900,'third_party_estimate','fictional quote')})
    assert validate_frozen(store,boundary,oid,NOW,changed)
    assert not validate_frozen(store,boundary,oid,NOW+dt.timedelta(days=2),cfg)
    store.execute("UPDATE outbox SET first_attempt_at=?,state='uncertain' WHERE id=?",(NOW.isoformat(),oid))
    assert prepare(store,boundary,unified(settings),cfg,'2026-10-24',NOW+dt.timedelta(minutes=1))==oid
    assert store.execute('SELECT payload FROM outbox WHERE id=?',(oid,)).fetchone()[0]==payload
    assert json.loads(store.execute('SELECT snapshot FROM discovery_reports WHERE outbox_id=?',(oid,)).fetchone()[0])['report_mode']=='mvp'


def test_latest_tax_pair_missing_newer_bill_and_estimate_remain_pending(store,boundary):
    source,item=add(store,TaxYear=2024,TaxAnnualAmount=1200)
    other=Source('newer_bill','public_records','FICTIONAL','tax evidence',True,True)
    newer=replace(item,source=other,record_id='bill',fields={'tax_year':2025})
    ingest(store,newer,NOW,authorized=True)
    assert screen(store,item.entity_id,boundary,NOW)['status']=='Pending Verification'
    later=NOW+dt.timedelta(seconds=1)
    ingest(store,replace(newer,fields={'tax_year':2025,'annual_tax':1500},estimates=('annual_tax',),observed_at=later.isoformat()),later,authorized=True)
    assert screen(store,item.entity_id,boundary,later)['status']=='Pending Verification'


@pytest.mark.parametrize('result,label',[
    ({'fresh_scan':True,'records':0},'fresh discovery'),
    ({'error':'OSError'},'source unavailable'),
    ({'error':'BudgetExceeded'},'internal api budget exhausted'),
    ({'mode':'cadence_skipped'},'scan skipped'),
    ({'mode':'cached_provider_page'},'cached results only')])
def test_no_matches_is_independent_from_scan_outcome(store,boundary,result,label):
    data=snapshot(store,boundary,NOW,report_mode='mvp');data['source_results']=[dict(source='fixture',**result)]
    body=render(data)
    assert 'No new or newly price-reduced qualifying properties' in body['text']
    assert label in body['text']


def test_stale_true_dual_flag_cannot_enable_finance_and_old_unchanged_not_new(store,boundary,settings):
    source,item=add(store)
    cfg=DiscoverySettings(enabled=True,daily_enabled=True,approval_id='FICTIONAL',dual_enabled=True,sources=({'source':source,'transport':'reso'},))
    oid=prepare(store,boundary,unified(settings),cfg,'2026-10-24',NOW)
    report=json.loads(store.execute('SELECT snapshot FROM discovery_reports WHERE outbox_id=?',(oid,)).fetchone()[0])
    assert report['underwriting_version']=='mvp-v1' and 'acquisition' not in report['properties'][0]
    later=NOW+dt.timedelta(hours=1)
    report=snapshot(store,boundary,later,report_mode='mvp',since=NOW.isoformat())
    assert '101 Fixture St' not in render(report)['text'] and report['counts']['new']==0


def test_html_escapes_untrusted_address_and_missing_link_message(store,boundary):
    source,item=add(store)
    data=snapshot(store,boundary,NOW,report_mode='mvp')
    data['properties'][0].update(address='<script>alert(1)</script>',links=[])
    body=render(data)
    assert '<script>' not in body['html'] and '&lt;script&gt;' in body['html']
    assert 'Original listing link not supplied' in body['text']


def test_synthetic_demo(tmp_path):
    from scripts.mvp_dry_run import generate
    data=generate(tmp_path)
    assert data['counts']['qualified']==2 and data['counts']['pending']==1 and data['counts']['price_reductions']==1
    assert data['synthetic'] and data['real_api_requests']==data['real_emails']==0
