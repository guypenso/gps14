"""Specific MVP fixes; all HTTP traffic is fake. No paid requests or email."""
import datetime as dt,json
from dataclasses import replace
from types import SimpleNamespace
import urllib.error
import pytest
from conftest import Provider,listing
from rentalsourcing.cloud import daily_discovery
from rentalsourcing.collector import Client
from rentalsourcing.billing import cycle_key
from rentalsourcing.digest import Digest
from rentalsourcing.engine import Engine
from rentalsourcing.poc import validate_proof_target


def approved(settings):
    return replace(settings,approval_mode='billing_cycle',billing_anchor='2026-10-01T00:00:00+00:00',billing_baseline=9,approved_requests=40,per_month=40,per_day=4,per_run=4,retries=0)


def test_daily_scans_before_digest_and_duplicate_does_not_rescan_or_send(store,boundary,settings,clock):
    p=Provider();s=approved(settings)
    result=daily_discovery(store,boundary,s,clock().date().isoformat(),clock=clock,opener=p)
    assert result['status']=='sent' and result['scan']=='fresh' and result['requests']>=1
    assert '/listings/sale' in p.calls[0].full_url and p.messages
    assert store.get_property('fixture-property')['state']=='Qualified'
    first=json.dumps(p.messages[0],sort_keys=True);before=len(p.calls)
    again=daily_discovery(store,boundary,s,clock().date().isoformat(),clock=clock,opener=p)
    assert again['status']=='already_sent' and len(p.calls)==before and len(p.messages)==1
    assert json.dumps(p.messages[0],sort_keys=True)==first


@pytest.mark.parametrize('case,expected',[('disabled','skipped_disabled'),('budget','skipped_budget_exhausted'),('billing','skipped_billing_configuration')])
def test_skipped_daily_still_sends_truthful_report_without_provider_request(store,boundary,settings,clock,case,expected):
    s=approved(settings);p=Provider()
    if case=='disabled':s=replace(s,enabled=False)
    if case=='billing':s=replace(s,approval_mode='one_time')
    if case=='budget':store.execute('INSERT INTO usage VALUES (?,?)',(cycle_key(s,clock()),40))
    result=daily_discovery(store,boundary,s,clock().date().isoformat(),clock=clock,opener=p)
    assert result['scan']==expected and result['requests']==0 and len(p.messages)==1
    assert 'Discovery skipped' in p.messages[0]['text'] and p.tax_calls==0


def test_failed_discovery_is_not_no_matching_properties(store,boundary,settings,clock):
    p=Provider();p.failures=[urllib.error.URLError('fictional outage')]
    result=daily_discovery(store,boundary,approved(settings),clock().date().isoformat(),clock=clock,opener=p)
    assert result['scan']=='failed' and result['requests']==1
    assert 'Discovery failed' in p.messages[0]['text'] and len(p.messages)==1


def test_missing_tax_shown_separately_with_original_link(store,boundary,settings,clock):
    row=listing(listingUrl='https://listing.example.invalid/fixture')
    p=Provider([row]);p.records={}
    result=daily_discovery(store,boundary,approved(settings),clock().date().isoformat(),clock=clock,opener=p)
    assert store.get_property(row['id'])['state']=='Pending Verification'
    assert 'Pending Verification' in p.messages[0]['text'] and row['listingUrl'] in p.messages[0]['text']
    assert 'Tax/year: not verified' in p.messages[0]['text'] and result['requests']<=4


def test_cached_response_never_becomes_a_fresh_or_new_recommendation(store,boundary,settings,clock):
    s=approved(settings);p=Provider();client=Client(store,s,clock,p,lambda _:None)
    from rentalsourcing.request import PARAMS
    client.get('/listings/sale',dict(PARAMS,limit=s.page_size,offset=0))
    result=daily_discovery(store,boundary,s,clock().date().isoformat(),clock=clock,opener=p)
    assert result['scan']=='cached' and 'Cached listing response' in p.messages[0]['text']
    assert 'נכס חדש שאומת:' not in p.messages[0]['text']


def test_preview_reuses_digest_but_can_never_be_sent(store,boundary,settings,clock):
    p=Provider();s=replace(settings,email_enabled=False)
    stats=Engine(store,boundary,Client(store,s,clock,p,lambda _:None),s,clock).run('fixture',clock().date().isoformat(),candidate_limit=3,first_page_only=True)
    digest=Digest(store,boundary,s,clock,p)
    oid=digest.prepare('fixture',clock().date().isoformat(),stats,preview=True)
    assert json.loads(store.execute('SELECT payload FROM outbox WHERE id=?',(oid,)).fetchone()[0])['preview_only']
    with pytest.raises(PermissionError,match='preview'):
        Digest(store,boundary,replace(s,email_enabled=True),clock,p).send(oid)
    assert not p.messages


def test_price_reduction_remains_one_property_and_one_new_event(store,boundary,settings,clock):
    s=approved(settings);p=Provider([listing(price=180000)])
    first=daily_discovery(store,boundary,s,clock().date().isoformat(),clock=clock,opener=p)
    clock.advance(days=1);p.rows=[listing(price=170000)]
    again=daily_discovery(store,boundary,s,clock().date().isoformat(),clock=clock,opener=p)
    assert first['status']==again['status']=='sent' and len(p.messages)==2
    assert store.execute('SELECT COUNT(*) FROM properties').fetchone()[0]==1
    assert 'הורדת מחיר חדשה' in p.messages[1]['text']


@pytest.mark.parametrize('used,age,host,isolated,ok',[(9,0,'localhost',True,True),(46,0,'localhost',True,True),(47,0,'localhost',True,False),(9,16,'localhost',True,False),(9,0,'production.example.invalid',True,False),(9,0,'localhost',False,False)])
def test_proof_rejects_stale_usage_overage_and_production_target(used,age,host,isolated,ok):
    now=dt.datetime(2026,10,26,12,tzinfo=dt.timezone.utc)
    args=SimpleNamespace(isolated=isolated,provider_used=used,usage_as_of=(now-dt.timedelta(minutes=age)).isoformat(),renewal_date='2026-11-14')
    url='postgresql://fixture:fixture@'+host+'/rentalsourcing_test'
    if ok:validate_proof_target(url,args,now)
    else:
        with pytest.raises(PermissionError):validate_proof_target(url,args,now)


def test_proof_endpoint_slots_survive_restart_and_block_second_listing(store,settings,clock):
    from rentalsourcing.poc import ProofClient
    from rentalsourcing.storage import BudgetExceeded
    p=Provider();s=replace(settings,approved_requests=4,per_run=4,retries=0)
    client=ProofClient(store,s,clock,p,lambda _:None)
    client.get('/listings/sale',{'limit':1,'offset':0})
    reopened=ProofClient(store,s,clock,p,lambda _:None)
    with pytest.raises(BudgetExceeded):reopened.get('/listings/sale',{'limit':1,'offset':0})
    for i in range(3):reopened.get('/properties/fixture-property',cache=False)
    with pytest.raises(BudgetExceeded):reopened.get('/properties/fixture-property',cache=False)
    assert len(p.calls)==4 and p.tax_calls==3


def test_resuming_completed_scan_does_not_claim_another_fresh_scan(store,boundary,settings,clock):
    p=Provider();s=replace(settings,email_enabled=False)
    engine=Engine(store,boundary,Client(store,s,clock,p,lambda _:None),s,clock)
    assert engine.run('fixture',clock().date().isoformat())['discovery_state']=='fresh'
    before=len(p.calls)
    assert engine.run('fixture',clock().date().isoformat())['discovery_state']=='previous_scan'
    assert len(p.calls)==before


def test_bounded_proof_command_end_to_end_no_send(store,settings,monkeypatch,tmp_path,capsys):
    if not store.pg:pytest.skip('Proof CLI requires isolated PostgreSQL')
    from rentalsourcing.poc import main
    now=dt.datetime.now(dt.timezone.utc)
    p=Provider([listing(listingUrl='https://listing.example.invalid/proof')])
    for name,value in {'DATABASE_URL':store.test_url,'ENABLE_RENTCAST':'true','ENABLE_EMAIL':'false','API_APPROVAL_MODE':'one_time','API_APPROVAL_ID':'fixture-proof','APPROVED_API_REQUESTS':'4','MAX_API_REQUESTS_PER_RUN':'4','MAX_API_REQUESTS_PER_DAY':'4','MAX_API_REQUESTS_PER_MONTH':'40'}.items():monkeypatch.setenv(name,value)
    monkeypatch.setattr('urllib.request.build_opener',lambda *args:p)
    monkeypatch.setattr('rentalsourcing.collector.time.sleep',lambda *args:None)
    main(['--execute','--isolated','--provider-used','9','--usage-as-of',now.isoformat(),'--renewal-date',(now.date()+dt.timedelta(days=10)).isoformat(),'--output',str(tmp_path)])
    data=json.loads(capsys.readouterr().out)
    assert data['fresh'] and data['received']==1 and data['qualified_in_sample']==1
    assert data['attempts_this_run']==2 and data['provider_used_upper_bound']==11
    assert 'Original listing' in (tmp_path/'report.html').read_text()
    assert not p.messages and data['no_real_emails']
