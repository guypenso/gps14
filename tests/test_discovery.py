"""Multi-source contract/integration tests. All feeds, keys and addresses are fictional."""
from dataclasses import replace
import datetime as dt
import hashlib,hmac,io,json,os
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit,parse_qs
import pytest
from rentalsourcing.discovery.models import Source,Listing,normalize,instant
from rentalsourcing.discovery.connectors import ResoConnector,CSVConnector,WPRDCConnector,safe_endpoint
from rentalsourcing.discovery.repository import ingest,resolution,register
from rentalsourcing.discovery.finance import Input,underwriting,scenarios
from rentalsourcing.discovery.evaluate import evaluate
from rentalsourcing.discovery.config import DiscoverySettings
from rentalsourcing.discovery.report import snapshot,prepare,validate_frozen
from rentalsourcing.discovery.runtime import daily
from rentalsourcing.discovery.coverage import compare
from rentalsourcing.discovery.export_web import verify
from rentalsourcing.storage import BudgetExceeded,Store
from rentalsourcing.collector import Client
from rentalsourcing.billing import cycle_key
from rentalsourcing.dashboard import create_app
from rentalsourcing.digest import Digest,ReconciliationRequired
from conftest import Response,Provider

NOW=dt.datetime(2026,10,24,12,tzinfo=dt.timezone.utc)

@pytest.fixture
def mls():return Source('licensed_mls','mls','synthetic-license','fictional Pennsylvania residential sale cohort',True,True)


def record(identity='1',**changes):
    return dict(dict(ListingKey=identity,ListingId='MLS-'+identity,OriginatingSystemName='FictionalMLS',
        UnparsedAddress='101 Fixture St, Pittsburgh, PA 15212',ListPrice=150000,BedroomsTotal=3,BathroomsTotalInteger=1,
        LivingArea=1200,StandardStatus='Active',Latitude=40.45,Longitude=-80,TaxAnnualAmount=1500,TaxYear=2025,
        PropertySubType='SingleFamilyResidence',AssociationFee=0,AssociationFeeFrequency='Monthly',
        ModificationTimestamp=(NOW-dt.timedelta(hours=1)).isoformat(),ListingURL='https://listing.example.invalid/MLS-'+identity),**changes)


def add(store,source,now=NOW,**changes):
    item=normalize(record(**changes),source,observed_at=now.isoformat());return ingest(store,item,now,authorized=True)['entity_id']


def assumptions(**changes):
    values=dict(annual_insurance=1200,vacancy_rate=.05,maintenance_rate=.05,capex_rate=.05,management_rate=.08,closing_cost=4500,renovation_budget=10000)
    values.update(changes)
    return {k:Input(v,'assumption','explicit fictional scenario','SYNTHETIC-NOT-OWNER-POLICY') for k,v in values.items()}


def complete_inputs(**changes):
    values=dict(monthly_rent=1500,annual_tax=1500,hoa_monthly=0)
    values.update(changes)
    return {**assumptions(),**{k:Input(v,'verified_source','fictional-source') for k,v in values.items()}}


def cfg(mls,**changes):return DiscoverySettings(report_mode="legacy",dual_enabled=False,enabled=True,daily_enabled=True,approval_id='synthetic-only',sources=({'source':mls,'transport':'reso'},),**changes)


def unified(settings,**changes):
    return replace(settings,discovery_source='unified',per_month=40,per_day=40,per_run=40,approved_requests=40,
        billing_anchor='2026-10-01T00:00:00+00:00',billing_baseline=0,**changes)


def test_normalization_dedup_across_sources_units_and_no_private_links(store,mls):
    first=add(store,mls)
    rc=Source('rentcast','rentcast','approval','same cohort',True)
    alias=normalize(dict(id='rc1',formattedAddress='101 Fixture Street, Pittsburgh, PA 15212',price=150000),rc,observed_at=NOW.isoformat(),style='rentcast')
    assert ingest(store,alias,NOW,authorized=True)['entity_id']==first
    assert ingest(store,alias,NOW,authorized=True)['duplicate']
    unit=add(store,mls,identity='unit',UnparsedAddress='101 Fixture St Unit 2, Pittsburgh, PA 15212')
    assert unit!=first and store.execute('SELECT COUNT(*) FROM discovery_entities').fetchone()[0]==2
    assert store.execute('SELECT COUNT(*) FROM discovery_observations').fetchone()[0]==3
    with pytest.raises(ValueError):normalize(record(ListingURL='https://portal.example.invalid/private/SECRET'),mls)


@pytest.mark.parametrize('field,value,reason',[('ListPrice',200001,'price'),('BedroomsTotal',2,'bedrooms'),('LivingArea',999,'squareFootage'),('UnparsedAddress','101 Fixture St, Pittsburgh, PA 15210','excluded_zip'),('Latitude',40.7,'outside_city_or_boundary'),('StandardStatus','Pending','status')])
def test_original_hard_filters_preserved(store,mls,boundary,field,value,reason):
    eid=add(store,mls,**{field:value});result=evaluate(store,eid,boundary,NOW)
    assert result['status']=='Rejected' and reason in result['reasons']


@pytest.mark.parametrize('tax,state',[(1899.99,'Qualified'),(1900,'Rejected'),(1900.01,'Rejected')])
def test_strict_tax_boundary_and_missing_financials_stay_pending(store,mls,boundary,tax,state):
    eid=add(store,mls,TaxAnnualAmount=tax,EstimatedMonthlyRent=1500)
    result=evaluate(store,eid,boundary,NOW,assumptions=assumptions(),financing='cash')
    assert result['status']==state
    if state=='Qualified':assert result['financial']['metrics']['cash_flow_monthly'] is not None
    else:assert 'annual_tax_too_high' in result['reasons']
    if tax==1899.99:
        missing=evaluate(store,eid,boundary,NOW)
        assert missing['status']=='Pending Verification' and missing['investment_score'] is None


def test_latest_tax_year_is_atomic_no_old_bill_new_year_mix(store,mls,boundary):
    eid=add(store,mls,TaxAnnualAmount=1200,TaxYear=2024)
    rc=Source('rc','rentcast','approval','financial evidence',True)
    item=normalize(dict(id='tax',formattedAddress='101 Fixture St, Pittsburgh, PA 15212'),rc,observed_at=NOW.isoformat(),style='rentcast')
    ingest(store,replace(item,fields={'annual_tax':2200,'tax_year':2025}),NOW,authorized=True)
    assert evaluate(store,eid,boundary,NOW)['status']=='Rejected'
    assert resolution(store,eid,NOW)['fields']['annual_tax']==2200


def test_latest_invalid_tax_does_not_fall_back_to_older_valid_bill(store,mls,boundary):
    eid=add(store,mls,TaxAnnualAmount=1200,TaxYear=2024)
    rc=Source('rc','rentcast','approval','financial evidence',True)
    item=normalize(dict(id='tax',formattedAddress='101 Fixture St, Pittsburgh, PA 15212'),rc,observed_at=NOW.isoformat(),style='rentcast')
    ingest(store,replace(item,fields={'tax_year':2025}),NOW,authorized=True)
    r=evaluate(store,eid,boundary,NOW)
    assert r['status']=='Pending Verification' and r['fields']['tax_year']==2025 and 'annual_tax' not in r['fields']


def test_conflicting_newer_lower_trust_record_does_not_overwrite_mls(store,mls,boundary):
    eid=add(store,mls,now=NOW-dt.timedelta(minutes=1))
    rc=Source('rc','rentcast','approval','same scope',True)
    item=normalize(dict(id='rc',formattedAddress='101 Fixture St, Pittsburgh, PA 15212',price=100000),rc,observed_at=NOW.isoformat(),style='rentcast')
    ingest(store,item,NOW,authorized=True);r=evaluate(store,eid,boundary,NOW)
    assert r['fields']['price']==150000 and 'price' in r['conflicts'] and r['status']=='Pending Verification'


def test_source_identity_collision_and_delayed_data_cannot_move_or_revert_property(store,mls):
    eid=add(store,mls)
    with pytest.raises(ValueError):add(store,mls,UnparsedAddress='999 Example Rd, Pittsburgh, PA 15212')
    add(store,mls,now=NOW+dt.timedelta(hours=1),ListPrice=140000,ModificationTimestamp=NOW.isoformat())
    add(store,mls,now=NOW+dt.timedelta(hours=2),ListPrice=180000,ModificationTimestamp=(NOW-dt.timedelta(days=1)).isoformat())
    assert resolution(store,eid,NOW+dt.timedelta(hours=2))['fields']['price']==140000
    assert store.execute("SELECT COUNT(*) FROM discovery_observations WHERE kind='historical'").fetchone()[0]==1


def test_current_poll_refreshes_unchanged_listing_without_claiming_new_discovery(store,mls,boundary):
    eid=add(store,mls,now=NOW,ModificationTimestamp=(NOW-dt.timedelta(days=30)).isoformat())
    assert evaluate(store,eid,boundary,NOW)['fields']['status']=='Active'
    add(store,mls,now=NOW+dt.timedelta(days=2),ModificationTimestamp=(NOW-dt.timedelta(days=30)).isoformat())
    r=snapshot(store,boundary,NOW+dt.timedelta(days=2))
    assert r['counts']['new']==0 and r['counts']['seen_this_window']==1


def test_underwriting_known_cash_model_and_no_default_assumptions():
    result=underwriting(150000,complete_inputs(),financing='cash');m=result['metrics']
    assert m['gross_yield_pct']==12
    # 18000 rent -900 vacancy -1500 tax -1200 insurance -900 maintenance -1368 management =12132 NOI
    assert m['noi_annual']==12132 and m['cash_flow_monthly']==936
    assert m['net_yield_pct']==pytest.approx(100*11232/164500,abs=1e-6)
    assert m['cash_on_cash_pct']==m['net_yield_pct'] and result['complete']
    missing=underwriting(150000,{})
    assert not missing['complete'] and missing['metrics']['net_yield_pct'] is None
    with pytest.raises(PermissionError):Input(1200,'assumption','guess')
    with pytest.raises(ValueError):underwriting(150000,complete_inputs(vacancy_rate=1.1))


def test_financing_and_explicit_scenarios_only():
    base=complete_inputs();base.update({k:Input(v,'assumption','fictional-loan','synthetic') for k,v in dict(down_payment_rate=.25,interest_rate=.06,loan_years=30).items()})
    m=underwriting(150000,base,financing='loan')['metrics']
    assert m['debt_service_monthly']==pytest.approx(674.49434,abs=.001)
    assert m['cash_flow_monthly']==pytest.approx(261.50566,abs=.001)
    assert underwriting(150000,base)['metrics']['cash_flow_monthly'] is None
    compared=scenarios(150000,base,{'specified_low_rent':{'monthly_rent':Input(1300,'assumption','explicit-scenario','synthetic')}},financing='loan')
    assert compared['specified_low_rent']['metrics']['cash_flow_monthly']<m['cash_flow_monthly']


class JSONResponse(io.BytesIO):
    def __init__(self,obj,status=200):super().__init__(json.dumps(obj).encode());self.status=status;self.headers={}


def test_reso_oauth_incremental_pages_checkpoint_and_nextlink_credentials_protected(store,mls):
    class Transport:
        calls=[]
        def open(self,req,timeout=None):
            self.calls.append(req)
            if req.full_url.endswith('/token'):
                assert req.data and req.get_header('Authorization') is None
                return JSONResponse({'access_token':'synthetic-token','expires_in':3600})
            assert req.get_header('Authorization')=='Bearer synthetic-token'
            if '$skip=1' in req.full_url:return JSONResponse({'value':[record('2',UnparsedAddress='202 Sample Ave, Pittsburgh, PA 15212')]})
            return JSONResponse({'value':[record()], '@odata.nextLink':'https://feed.example.invalid/odata/Property?$skip=1'})
    t=Transport();adapter=ResoConnector(mls,'https://feed.example.invalid/odata/',token_url='https://feed.example.invalid/token',client_id='synthetic',client_secret='synthetic',opener=t,expected_system='FictionalMLS')
    result=adapter.scan(store,NOW)
    assert result['complete'] and result['requests']==2 and store.execute('SELECT COUNT(*) FROM discovery_entities').fetchone()[0]==2
    cursor=json.loads(store.execute('SELECT cursor FROM discovery_sources').fetchone()[0]);assert cursor=={'watermark':NOW.isoformat()}
    for url in ['https://attacker.invalid/odata/Property','https://feed.example.invalid/other','http://feed.example.invalid/odata/Property']:
        with pytest.raises(PermissionError):safe_endpoint(url,adapter.base)


def test_reso_bad_nextlink_isolated_failure_preserves_committed_page(store,mls):
    class Transport:
        def open(self,req,timeout=None):return JSONResponse({'value':[record()],'@odata.nextLink':'https://attacker.invalid/steal'})
    result=ResoConnector(mls,'https://feed.example.invalid/odata/',token='synthetic',opener=Transport()).scan(store,NOW)
    assert not result['complete'] and result['error']=='PermissionError'
    assert store.execute('SELECT COUNT(*) FROM discovery_entities').fetchone()[0]==1
    assert store.execute('SELECT last_success FROM discovery_sources').fetchone()[0] is None


def test_csv_replay_old_export_and_public_assessments_are_not_taxes(store,mls,boundary):
    body=Path('tests/fixtures/discovery/mls.csv').read_bytes();adapter=CSVConnector(mls)
    assert adapter.import_bytes(store,body,NOW.isoformat(),NOW,authorized=True)['records']==4
    adapter.import_bytes(store,body,NOW.isoformat(),NOW+dt.timedelta(hours=1),authorized=True)
    assert store.execute('SELECT COUNT(*) FROM discovery_entities').fetchone()[0]==4
    assert store.execute('SELECT COUNT(*) FROM discovery_observations').fetchone()[0]==4
    source=Source('county','public_records','CC0-terms-approved','Allegheny characteristics only',True,True)
    row=dict(PARID='0000A00000000000',PROPERTYHOUSENUM='101',PROPERTYADDRESS='Fixture St',PROPERTYCITY='Pittsburgh',PROPERTYSTATE='PA',PROPERTYZIP='15212',BEDROOMS=3,FULLBATHS=1,HALFBATHS=1,FINISHEDLIVINGAREA=1200,COUNTYTOTAL=90000,LOCALTOTAL=100000,TAXYEAR=2025,ASOFDATE='2026-10-07',OWNER='PRIVATE NAME MUST NOT BE STORED')
    item=WPRDCConnector(source).normalize(row,NOW)
    assert not {'price','annual_tax','tax_year','status'}&item.fields.keys() and item.fields['bathrooms']==1.5
    ingest(store,item,NOW,authorized=True)
    assert 'PRIVATE NAME' not in json.dumps(item.payload())


def test_budget_allocations_reserve_verification_conservative_failures_and_cache(store,settings,monkeypatch):
    s=unified(settings);now=lambda:NOW
    class Transport:
        calls=0
        def open(self,req,timeout=None):self.calls+=1;return Response([])
    t=Transport();client=Client(store,s,now,t,lambda _:None)
    for i in range(8):client.get('/listings/sale',{'city':'Pittsburgh','offset':i,'limit':1},purpose='discovery')
    with pytest.raises(BudgetExceeded):client.get('/listings/sale',{'city':'Pittsburgh','offset':99,'limit':1},purpose='discovery')
    assert t.calls==8
    client.get('/listings/sale',{'city':'Pittsburgh','offset':0,'limit':1},purpose='discovery')
    assert t.calls==8 and store.execute('SELECT COUNT(*) FROM discovery_api_ledger').fetchone()[0]==8
    assert store.count_usage(cycle_key(s,NOW))==8
    with pytest.raises(ValueError):replace(s,per_month=51)
    with pytest.raises(BudgetExceeded):Client(store,replace(s,billing_baseline=-1),now,t).get('/properties',{'address':'101 Fixture St, Pittsburgh, PA 15212','limit':1})


def test_provider50_limit_and_prior_usage_baseline_are_not_reset(store,settings):
    s=unified(settings,discovery_cap=50,verification_reserve=0);s=replace(s,per_month=50,approved_requests=50,billing_baseline=49)
    class T:
        calls=0
        def open(self,req,timeout=None):self.calls+=1;return Response([])
    t=T();c=Client(store,s,lambda:NOW,t,lambda _:None)
    c.get('/listings/sale',{'limit':1,'offset':0})
    with pytest.raises(BudgetExceeded):c.get('/listings/sale',{'limit':1,'offset':1})
    assert t.calls==1 and store.count_usage(cycle_key(s,NOW))==50


def test_coverage_is_measured_only_against_authorized_cohort():
    ref=[dict(key=k,relevant=True,listed_at='2026-10-24T00:00:00+00:00',status='Active',zipcode='15212') for k in 'abcd']
    rows=[dict(source='mls',key=k,discovered_at=NOW.isoformat(),status='Active') for k in 'abc']+[dict(source='rc',key=k,discovered_at=NOW.isoformat(),status='Active') for k in 'abbd']
    with pytest.raises(PermissionError):compare(rows,reference=ref)
    r=compare(rows,reference=ref,reference_permission='fictional-benchmark-only',as_of=NOW.isoformat())
    assert r['union_coverage_pct']==100 and r['sources']['mls']['coverage_pct']==75 and r['sources']['rc']['duplicate_records']==1
    assert compare(rows)['union_coverage_pct'] is None


def test_existing_outbox_daily_dedup_no_second_send_and_changed_qualification(store,mls,boundary,settings):
    eid=add(store,mls,EstimatedMonthlyRent=1500);s=unified(settings);config=cfg(mls,assumptions=assumptions(),financing='cash')
    identity=prepare(store,boundary,s,config,'2026-10-24',NOW)
    provider=Provider();digest=Digest(store,boundary,s,lambda:NOW,provider,discovery_settings=config)
    assert digest.send(identity)=='sent' and digest.send(identity)=='already_sent' and len(provider.messages)==1
    assert daily(store,boundary,s,'2026-10-24',cfg=config,clock=lambda:NOW)['status']=='already_sent'
    nextday=NOW+dt.timedelta(days=1)
    add(store,mls,now=nextday,EstimatedMonthlyRent=1500,ModificationTimestamp=nextday.isoformat())
    fresh=prepare(store,boundary,s,config,'2026-10-25',nextday)
    add(store,mls,now=nextday+dt.timedelta(minutes=1),TaxAnnualAmount=1900,EstimatedMonthlyRent=1500,ModificationTimestamp=nextday.isoformat())
    assert Digest(store,boundary,s,lambda:nextday+dt.timedelta(minutes=1),provider,discovery_settings=config).send(fresh)=='cancelled_stale'


def test_source_outage_isolated_while_other_source_completes_and_report_is_truthful(store,mls,boundary,settings):
    another=replace(mls,name='export_mls');config=replace(cfg(mls),sources=({'source':mls,'transport':'reso'},{'source':another,'transport':'reso'}))
    class Broken:
        def scan(self,*a):raise TimeoutError('TOKEN-MUST-NOT-LEAK')
    class Works:
        def scan(self,s,now):add(s,another);return {'source':another.name,'records':1,'complete':True}
    result=daily(store,boundary,replace(unified(settings),email_enabled=False),'2026-10-24',cfg=config,clock=lambda:NOW,providers={mls.name:Broken(),another.name:Works()})
    assert result['status']=='prepared_not_sent' and len(result['sources'])==2
    assert store.execute('SELECT COUNT(*) FROM discovery_entities').fetchone()[0]==1
    payload=store.execute('SELECT payload FROM outbox').fetchone()[0]
    assert 'TOKEN-MUST-NOT-LEAK' not in payload and 'No authorized real MLS benchmark' in payload


def test_csv_webhook_signature_tamper_expiry_and_recipient_source_binding():
    raw=b'fictional,csv\n';secret='synthetic-export-signing-secret-12345678';stamp=str(int(NOW.timestamp()));event='event_'+uuid4().hex;exported=NOW.isoformat()
    value=('.'+stamp+'.'+event+'.'+exported+'.').encode()+raw
    signature=hmac.new(secret.encode(),value,hashlib.sha256).hexdigest()
    headers={'X-MLS-Timestamp':stamp,'X-MLS-Event':event,'X-MLS-Exported-At':exported,'X-MLS-Signature':signature}
    assert verify(raw,headers,secret,NOW)[0]==event
    with pytest.raises(PermissionError):verify(raw+b' ',headers,secret,NOW)
    with pytest.raises(PermissionError):verify(raw,headers,secret,NOW+dt.timedelta(minutes=6))


def test_signed_export_receiver_duplicate_and_dashboard_auth(store,mls,boundary,monkeypatch):
    config=replace(cfg(mls),export_webhook_enabled=True,sources=({'source':mls,'transport':'csv_webhook','secret_env':'MLS_EXPORT_FIXTURE_SECRET'},))
    monkeypatch.setenv('MLS_EXPORT_FIXTURE_SECRET','synthetic-export-signing-secret-12345678')
    monkeypatch.setenv('DASHBOARD_PASSWORD','synthetic-dashboard-password-12345678')
    monkeypatch.setattr(DiscoverySettings,'from_env',classmethod(lambda cls:config))
    app=create_app(store.test_url,testing=True,clock=lambda:NOW,boundary=boundary);client=app.test_client()
    body=Path('tests/fixtures/discovery/mls.csv').read_bytes();stamp=str(int(NOW.timestamp()));event='event_'+uuid4().hex
    headers={'X-MLS-Timestamp':stamp,'X-MLS-Event':event,'X-MLS-Exported-At':NOW.isoformat()}
    headers['X-MLS-Signature']=hmac.new(os.environ['MLS_EXPORT_FIXTURE_SECRET'].encode(),(mls.name+'.'+stamp+'.'+event+'.'+NOW.isoformat()+'.').encode()+body,hashlib.sha256).hexdigest()
    path='/webhooks/mls/export/'+mls.name
    assert client.post(path,data=body).status_code==401
    assert client.post(path,data=body,headers=headers).status_code==200
    assert client.post(path,data=body,headers=headers).json['status']=='duplicate'
    assert client.post('/webhooks/mls/export/unauthorized',data=body,headers=headers).status_code==403
    assert client.get('/api/discovery/report').status_code==401


def test_benchmark_snapshot_excludes_future_discoveries_and_measures_misses_in_timeliness():
    ref=[dict(key=k,relevant=True,listed_at='2026-10-24T00:00:00+00:00',status='Active') for k in 'abcd']
    rows=[dict(source='a',key='a',discovered_at=NOW.isoformat(),status='Active'),dict(source='a',key='b',discovered_at=(NOW+dt.timedelta(days=1)).isoformat(),status='Active')]
    result=compare(rows,reference=ref,reference_permission='synthetic',as_of=NOW.isoformat(),expected_sources=['a','outage'])
    assert result['sources']['a']['coverage_pct']==25
    assert result['sources']['a']['found_within24h_pct']==100 and result['sources']['a']['reference_within24h_coverage_pct']==25
    assert result['sources']['outage']['coverage_pct']==0


def test_current_same_year_tax_conflict_never_qualifies(store,mls,boundary):
    eid=add(store,mls)
    rc=Source('rc','rentcast','approved','financial',True)
    item=normalize(dict(id='rc',formattedAddress='101 Fixture St, Pittsburgh, PA 15212'),rc,observed_at=NOW.isoformat(),style='rentcast')
    ingest(store,replace(item,fields={'annual_tax':1800,'tax_year':2025}),NOW,authorized=True)
    result=evaluate(store,eid,boundary,NOW,assumptions=assumptions())
    assert result['status']=='Pending Verification' and 'annual_tax' in result['conflicts']


def test_export_rejects_missing_or_duplicate_contract_headers(store,mls):
    for raw in [b'{}',b'ListingKey,ListingKey,UnparsedAddress\n1,2,foo\n',b'foo,bar\n']:
        with pytest.raises(ValueError):CSVConnector(mls).import_bytes(store,raw,NOW.isoformat(),NOW,authorized=True)


def test_unified_spending_reserve_and_every_retry_is_counted(store,settings):
    import urllib.error
    s=replace(unified(settings),enrichment_cap=50,verification_reserve=10,billing_baseline=29,retries=1)
    class T:
        calls=0
        def open(self,req,timeout=None):
            self.calls+=1
            if self.calls==1:raise urllib.error.HTTPError(req.full_url,503,'synthetic',{},io.BytesIO(b'no sensitive body'))
            return Response([])
    t=T();c=Client(store,s,lambda:NOW,t,lambda _:None)
    # 29 prior requests +1 failed attempt leaves10 reserved. Retry would invade the reserve.
    with pytest.raises(BudgetExceeded):c.get('/properties',{'address':'101 Fixture St, Pittsburgh, PA 15212','limit':1})
    assert t.calls==1 and store.count_usage(cycle_key(s,NOW))==30
    assert store.execute('SELECT outcome FROM discovery_api_ledger').fetchone()[0]=='failed_counted_conservatively'


def test_cached_evidence_survives_disabled_or_exhausted_request_allowance(store,settings):
    s=unified(settings)
    class T:
        calls=0
        def open(self,req,timeout=None):self.calls+=1;return Response([dict(id='rc',formattedAddress='101 Fixture St, Pittsburgh, PA 15212',propertyTaxes={'2025':{'year':2025,'total':1500}})])
    t=T();c=Client(store,s,lambda:NOW,t,lambda _:None)
    args=('/properties',{'address':'101 Fixture St, Pittsburgh, PA 15212','limit':1})
    c.get(*args)
    other=Client(store,replace(s,enabled=False,approved_requests=0),lambda:NOW,t,lambda _:None)
    assert other.get(*args)[0][0]['id']=='rc' and t.calls==1


def test_old_source_failure_does_not_repeat_after_success(store,mls,boundary):
    from rentalsourcing.discovery.repository import health
    register(store,mls);health(store,mls.name,NOW,error='OldFailure')
    health(store,mls.name,NOW,success=True)
    result=snapshot(store,boundary,NOW)
    assert result['sources'][0]['error'] is None


def test_normalization_rejects_rentals_and_demo_as_purchase_sources(mls):
    with pytest.raises(ValueError):normalize(record(PropertyType='Residential Lease'),mls)
    with pytest.raises(PermissionError):normalize(record(demo=True),mls)
    item=normalize(record(BathroomsFull=0,BathroomsHalf=1),mls)
    assert item.fields['bathrooms']==.5


def test_reso_stale_shortlist_refresh_has_no_new_discovery_and_cannot_escape_scope(store,mls):
    eid=add(store,mls,now=NOW-dt.timedelta(days=2),ModificationTimestamp=(NOW-dt.timedelta(days=3)).isoformat())
    class T:
        def open(self,req,timeout=None):
            assert 'ListingKey' in parse_qs(urlsplit(req.full_url).query)['$filter'][0]
            return JSONResponse({'value':[record(ModificationTimestamp=(NOW-dt.timedelta(days=3)).isoformat())]})
    adapter=ResoConnector(mls,'https://feed.example.invalid/odata/',token='synthetic',opener=T())
    assert adapter.verify_keys(store,['1'],NOW)['verified']==1
    assert resolution(store,eid,NOW)['fields']['status']=='Active'
    assert store.execute("SELECT kind FROM discovery_observations ORDER BY processed_at DESC LIMIT 1").fetchone()[0]=='refresh'
    for url in ['https://feed.example.invalid/odata/%2e%2e/private','https://feed.example.invalid/odata/Property?access_token=TOKEN']:
        with pytest.raises(PermissionError):safe_endpoint(url,adapter.base)


def test_trusted_receiving_transport_preserves_provider_verdict_without_accepting_header_claims():
    from rentalsourcing.realscout_worker import ReceivingClient
    from rentalsourcing.realscout_auth import ProviderAuthentication
    key=str(uuid4())
    class T:
        def open(self,req,timeout=None):return JSONResponse(dict(id=key,authentication={'spf':'fail','dkim':'pass','dmarc':'pass'},headers={'Authentication-Results':'forged'}))
    data=ReceivingClient('synthetic-only',opener=T()).get(key)
    assert isinstance(data['_receiver_authentication'],ProviderAuthentication)
    assert data['_receiver_authentication'].spf=='fail' and data['authentication']=={}


def test_estimated_tax_can_never_satisfy_hard_tax_verification(store,mls,boundary):
    item=normalize(record(EstimatedMonthlyRent=1500),mls,observed_at=NOW.isoformat())
    ingest(store,replace(item,estimates=tuple(item.estimates)+('annual_tax',)),NOW,authorized=True)
    r=evaluate(store,item.entity_id,boundary,NOW,assumptions=assumptions())
    assert r['status']=='Pending Verification' and 'annual_tax_is_only_an_estimate' in r['reasons']


def test_unknown_price_never_becomes_fake_hundred_percent_discount(store,mls,boundary):
    eid=add(store,mls,ListPrice='',EstimatedValue=200000)
    r=evaluate(store,eid,boundary,NOW)
    assert r['status']=='Pending Verification' and 'asking_discount_to_estimated_value_pct' not in r['financial']['metrics']


def test_cycle_baseline_only_applies_to_original_cycle(store,settings):
    from rentalsourcing.discovery.budget import seed
    s=unified(settings);s=replace(s,billing_baseline=49)
    nextmonth=NOW.replace(month=11)
    with store.transaction():seed(store,s,nextmonth)
    assert store.count_usage(cycle_key(s,nextmonth))==0
    with store.transaction():seed(store,s,NOW)
    assert store.count_usage(cycle_key(s,NOW))==49


def test_synthetic_reports_block_actual_sender_even_when_flags_enabled(store,mls,boundary,settings):
    synthetic=replace(mls,synthetic=True);add(store,synthetic,EstimatedMonthlyRent=1500)
    c=cfg(synthetic,assumptions=assumptions());s=unified(settings)
    identity=prepare(store,boundary,s,c,'2026-10-24',NOW)
    with pytest.raises(PermissionError):Digest(store,boundary,s,lambda:NOW,Provider(),discovery_settings=c).send(identity)


def test_failed_delivery_freezes_payload_and_source_rescan_is_not_repeated(store,mls,boundary,settings):
    add(store,mls,EstimatedMonthlyRent=1500);s=unified(settings);c=cfg(mls,assumptions=assumptions())
    identity=prepare(store,boundary,s,c,'2026-10-24',NOW);original=store.execute('SELECT payload FROM outbox').fetchone()[0]
    p=Provider();p.failures=[TimeoutError('synthetic-timeout')]
    with pytest.raises(TimeoutError):Digest(store,boundary,s,lambda:NOW,p,discovery_settings=c).send(identity)
    class Forbidden:
        def scan(self,*a):raise AssertionError('Frozen retry must not rescan')
    result=daily(store,boundary,s,'2026-10-24',cfg=c,clock=lambda:NOW,providers={mls.name:Forbidden()},opener=p)
    assert result['status']=='sent' and result['sources']==[]
    assert store.execute('SELECT payload FROM outbox').fetchone()[0]==original


def test_duplicate_metric_distinguishes_replay_from_price_change(store,mls):
    eid=add(store,mls);add(store,mls)
    add(store,mls,now=NOW+dt.timedelta(hours=1),ListPrice=140000,ModificationTimestamp=NOW.isoformat())
    assert store.execute('SELECT attempts,duplicates FROM discovery_source_metrics').fetchone()==(3,1)


def test_reso_page_cap_persists_resume_cursor_and_does_not_claim_complete_scan(store,mls):
    class T:
        def open(self,req,timeout=None):
            if '$skip=1' in req.full_url:return JSONResponse({'value':[record('2',UnparsedAddress='202 Sample Ave, Pittsburgh, PA 15212')]})
            return JSONResponse({'value':[record()],'@odata.nextLink':'https://feed.example.invalid/odata/Property?$skip=1'})
    adapter=ResoConnector(mls,'https://feed.example.invalid/odata/',token='synthetic',opener=T(),max_pages=1)
    assert not adapter.scan(store,NOW)['complete']
    assert 'next' in json.loads(store.execute('SELECT cursor FROM discovery_sources').fetchone()[0])
    assert adapter.scan(store,NOW+dt.timedelta(minutes=1))['complete']
    assert store.execute('SELECT COUNT(*) FROM discovery_entities').fetchone()[0]==2


def test_broker_csv_signed_for_one_source_cannot_be_replayed_to_another():
    body=b'ListingKey,UnparsedAddress\n';secret='synthetic-secret-long-enough-12345678';event='event_'+uuid4().hex;stamp=str(int(NOW.timestamp()))
    data=('source_a.'+stamp+'.'+event+'.'+NOW.isoformat()+'.').encode()+body
    headers={'X-MLS-Timestamp':stamp,'X-MLS-Event':event,'X-MLS-Exported-At':NOW.isoformat(),'X-MLS-Signature':hmac.new(secret.encode(),data,hashlib.sha256).hexdigest()}
    assert verify(body,headers,secret,NOW,source_name='source_a')
    with pytest.raises(PermissionError):verify(body,headers,secret,NOW,source_name='source_b')

@pytest.mark.parametrize('lease_type',['ResidentialLease','Commercial Lease'])
def test_mixed_mls_page_skips_known_lease_without_losing_sale_or_watermark(store,mls,lease_type):
    class Transport:
        def open(self,req,timeout=None):
            return JSONResponse({'value':[
                {'ListingKey':'lease-only','PropertyType':lease_type,'OriginatingSystemName':'FictionalMLS'},
                record()]})
    result=ResoConnector(mls,'https://feed.example.invalid/odata/',token='synthetic',opener=Transport(),expected_system='FictionalMLS').scan(store,NOW)
    assert result['complete'] and result['records']==1 and result['skipped_non_purchase']==1
    assert store.execute('SELECT COUNT(*) FROM discovery_records').fetchone()[0]==1
    assert json.loads(store.execute('SELECT cursor FROM discovery_sources').fetchone()[0])=={'watermark':NOW.isoformat()}


def test_mls_skip_does_not_hide_scope_violations_or_malformed_purchase(store,mls):
    class Transport:
        row={'ListingKey':'lease','PropertyType':'ResidentialLease','OriginatingSystemName':'UnauthorizedMLS'}
        def open(self,req,timeout=None):return JSONResponse({'value':[self.row]})
    transport=Transport();connector=ResoConnector(mls,'https://feed.example.invalid/odata/',token='synthetic',opener=transport,expected_system='FictionalMLS')
    assert connector.scan(store,NOW)['error']=='PermissionError'
    transport.row=record(UnparsedAddress='unresolvable address')
    result=connector.scan(store,NOW)
    assert not result['complete'] and result['error']=='ValueError' and result['skipped_non_purchase']==0
    assert store.execute('SELECT cursor FROM discovery_sources').fetchone()[0] is None


def test_documented_trestle_oauth_form_expiry_and_incremental_sale_status_changes(store,mls,monkeypatch):
    import rentalsourcing.discovery.connectors as connectors
    tick=[1000];monkeypatch.setattr(connectors.time,'time',lambda:tick[0])
    class Transport:
        calls=[];tokens=0;changed=False
        def open(self,req,timeout=None):
            self.calls.append(req)
            if req.full_url.endswith('/oidc/connect/token'):
                assert parse_qs(req.data.decode())=={'grant_type':['client_credentials'],'scope':['api'],'client_id':['fictional-client'],'client_secret':['fictional-secret']}
                assert req.get_header('Content-type')=='application/x-www-form-urlencoded'
                self.tokens+=1
                return JSONResponse({'access_token':'fictional-token-'+str(self.tokens),'expires_in':120})
            assert req.get_header('Authorization')=='Bearer fictional-token-'+str(self.tokens)
            query=parse_qs(urlsplit(req.full_url).query)
            assert query['$orderby']==['ModificationTimestamp,ListingKey']
            assert 'StandardStatus' not in query['$filter'][0] and 'ListPrice' not in query['$filter'][0]
            row=record()
            if self.changed:
                assert (NOW-dt.timedelta(minutes=5)).isoformat() in query['$filter'][0]
                row=record(ListPrice=140000,StandardStatus='Pending',ModificationTimestamp=(NOW+dt.timedelta(minutes=1)).isoformat())
            return JSONResponse({'value':[row]})
    transport=Transport();adapter=ResoConnector(mls,'https://api.cotality.com/trestle/odata/',token_url='https://api.cotality.com/trestle/oidc/connect/token',client_id='fictional-client',client_secret='fictional-secret',expected_system='FictionalMLS',opener=transport)
    assert adapter.scan(store,NOW)['complete']
    assert transport.tokens==1
    transport.changed=True;tick[0]+=100
    assert adapter.scan(store,NOW+dt.timedelta(minutes=2))['complete'] and transport.tokens==2
    fields=resolution(store,normalize(record(),mls).entity_id,NOW+dt.timedelta(minutes=2))['fields']
    assert fields['price']==140000 and fields['status']=='Pending'


def test_point_verification_never_refreshes_purchase_with_lease_evidence(store,mls):
    eid=add(store,mls)
    class Transport:
        def open(self,req,timeout=None):return JSONResponse({'value':[record(PropertyType='ResidentialLease',ListPrice=1500)]})
    result=ResoConnector(mls,'https://feed.example.invalid/odata/',token='synthetic',opener=Transport(),expected_system='FictionalMLS').verify_keys(store,['1'],NOW+dt.timedelta(hours=1))
    assert result['verified']==0 and result['unresolved']==1
    assert resolution(store,eid,NOW+dt.timedelta(hours=1))['fields']['price']==150000
