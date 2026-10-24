"""Offline pipeline/recovery exercise. Only isolated local PostgreSQL or pytest stores.
No production credentials/transports. Every HTTP opener is blocked except explicit fixtures.
"""
import argparse,datetime as dt,hashlib,io,json,os,re,sys,uuid
from pathlib import Path
from dataclasses import replace
from urllib.parse import urlsplit,parse_qs,quote
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from rentalsourcing.storage import Store
from rentalsourcing.config import Settings
from rentalsourcing.billing import cycle_key
from rentalsourcing.geography import load_boundary
from rentalsourcing.discovery.models import Source,normalize
from rentalsourcing.discovery.repository import ingest
from rentalsourcing.discovery.connectors import CSVConnector
from rentalsourcing.discovery.adapters import RentCastAdapter
from rentalsourcing.discovery.config import DiscoverySettings
from rentalsourcing.discovery.finance import Input
from rentalsourcing.discovery.runtime import daily
from rentalsourcing.discovery.report import prepare,render
from rentalsourcing.collector import Client
from rentalsourcing.digest import Digest
NOW=dt.datetime(2026,10,24,12,tzinfo=dt.timezone.utc)

class Response(io.BytesIO):
    def __init__(self,data,total=None):
        super().__init__(json.dumps(data).encode());self.status=200;self.headers={} if total is None else {'X-Total-Count':str(total)}

class FixtureTransport:
    def __init__(self,timeout=False):self.calls=[];self.timeout=timeout;self.messages=[]
    def open(self,request,timeout=None):
        self.calls.append(request);path=urlsplit(request.full_url).path
        if urlsplit(request.full_url).hostname=='api.resend.com':
            if self.timeout:raise TimeoutError('fictional delivery timeout')
            self.messages.append(json.loads(request.data));return Response({'id':'fictional-local-delivery'})
        assert urlsplit(request.full_url).hostname=='api.rentcast.io'
        address='101 Fixture St, Pittsburgh, PA 15212'
        subject={'id':'fixture-101','formattedAddress':address,'price':140000,'status':'Active','bedrooms':3,'bathrooms':1,'squareFootage':1200,'latitude':40.45,'longitude':-80,'propertyType':'Single Family'}
        if path.endswith('/listings/sale'):return Response([subject],1)
        if path.endswith('/properties'):return Response([{'id':'fixture-404','formattedAddress':'404 Fiction Rd, Pittsburgh, PA 15212'}])
        if path.endswith('/avm/rent/long-term'):
            wanted=parse_qs(urlsplit(request.full_url).query)['address'][0]
            if wanted!=address:subject={'id':'fixture-404','formattedAddress':wanted}
            return Response({'subjectProperty':subject,'rent':1500,'comparables':[{'id':'fictional-comp','price':1500,'distance':.3}]})
        raise AssertionError('Unexpected fixture request')

def configuration():
    # Non-synthetic source flag exercises sender behavior ONLY through injected fake transport.
    # All artifacts are forcibly labeled synthetic; production synthetic-delivery gate stays intact.
    mls=Source('fixture_mls','mls','FIXTURE-NOT-A-LICENSE','fictional purchase cohort',True,True)
    outage=replace(mls,name='fixture_mls_outage')
    rc=Source('fixture_rentcast','rentcast','FIXTURE-NOT-API-APPROVAL','fictional complement',True)
    assumptions={k:Input(v,'assumption','fictional scenario','FIXTURE-NOT-OWNER-APPROVAL') for k,v in dict(annual_insurance=1200,vacancy_rate=.05,maintenance_rate=.05,capex_rate=.05,management_rate=.08,closing_cost=4500,renovation_budget=10000).items()}
    cfg=DiscoverySettings(dual_enabled=False,enabled=True,daily_enabled=True,approval_id='FIXTURE-ONLY',sources=({'source':mls,'transport':'reso'},{'source':outage,'transport':'reso'},{'source':rc,'transport':'rentcast'}),assumptions=assumptions,financing='cash',rentcast_discovery=True,rentcast_enrichment=True,max_candidates=2,reverify_limit=0)
    settings=Settings(enabled=True,discovery_source='unified',per_run=8,per_day=10,per_month=40,approval_mode='billing_cycle',approved_requests=40,approval_id='FIXTURE-ONLY',billing_anchor='2026-10-01T00:00:00+00:00',billing_baseline=0,rent_estimates_enabled=True,email_approval_id='FIXTURE-ONLY',email_from='fixture@example.invalid',email_to='owner@example.invalid')
    return mls,outage,rc,cfg,settings

def simulate(store,boundary,out):
    mls,outage,rc,cfg,settings=configuration();transport=FixtureTransport()
    class Feed:
        def scan(self,s,now):
            CSVConnector(mls).import_bytes(s,Path('tests/fixtures/discovery/mls.csv').read_bytes(),(NOW-dt.timedelta(minutes=10)).isoformat(),now,authorized=True)
            row=dict(ListingKey='fixture-1',ListingId='MLS-1001',UnparsedAddress='101 Fixture St, Pittsburgh, PA 15212',ListPrice=140000,BedroomsTotal=3,BathroomsTotalInteger=1,LivingArea=1200,StandardStatus='Active',Latitude=40.45,Longitude=-80,TaxAnnualAmount=1500,TaxYear=2025,AssociationFee=0,AssociationFeeFrequency='Monthly',PropertySubType='SingleFamilyResidence',ListingURL='https://listing.example.invalid/MLS-1001',ModificationTimestamp=(NOW-dt.timedelta(minutes=1)).isoformat())
            ingest(s,normalize(row,mls,observed_at=now.isoformat()),now,authorized=True)
            return dict(source=mls.name,mode='licensed_api',complete=True,records=5,requests=1)
    class Outage:
        def scan(self,*args):raise TimeoutError('fictional source outage')
    adapter=RentCastAdapter(rc,Client(store,settings,clock=lambda:NOW,opener=transport),discovery_enabled=True,rent_enabled=True)
    result=daily(store,boundary,settings,'2026-10-24',cfg=cfg,clock=lambda:NOW,providers={mls.name:Feed(),outage.name:Outage(),rc.name:adapter})
    assert result['status']=='prepared_not_sent'
    # Same canonical address in MLS and RentCast is one entity, not two recommendations.
    assert store.execute('SELECT COUNT(*) FROM discovery_entities').fetchone()[0]==4
    assert store.execute("SELECT COUNT(*) FROM discovery_observations WHERE kind='price_reduction'").fetchone()[0]==1
    assert any(row.get('enrichment',{}).get('requests',0)>0 for row in result['sources'])
    # A later uncached scan must stop at the ORIGINAL persistent cycle ceiling, before wire.
    cycle=cycle_key(settings,NOW);store.execute('UPDATE usage SET used=40 WHERE period=?',(cycle,))
    later=NOW+dt.timedelta(minutes=20);blocked_transport=FixtureTransport()
    blocked=RentCastAdapter(rc,Client(store,settings,clock=lambda:later,opener=blocked_transport),discovery_enabled=True).scan(store,later)
    assert blocked.get('error')=='BudgetExceeded' and not blocked_transport.calls
    oid=prepare(store,boundary,settings,cfg,'2026-10-24',later,source_results=result['sources']+[blocked])
    raw=store.execute('SELECT snapshot FROM discovery_reports WHERE outbox_id=?',(oid,)).fetchone()[0];report=json.loads(raw)
    assert report['counts']['new']==4 and report['counts']['qualified']==1 and report['counts']['pending']==2 and report['counts']['rejected']==1
    assert report['properties'][0]['financial']['metrics']['cash_flow_monthly']==936
    assert any(r['state']=='internal_api_budget_exhausted' for r in report['scan_outcomes'])
    # Failure/recovery use mocked Resend only; the production synthetic gate is not bypassed.
    failed=FixtureTransport(timeout=True)
    try:Digest(store,boundary,replace(settings,email_enabled=True),lambda:later,failed,discovery_settings=cfg).send(oid)
    except TimeoutError:pass
    else:raise AssertionError('Expected fixture failure')
    assert store.execute('SELECT state FROM outbox WHERE id=?',(oid,)).fetchone()[0]=='uncertain'
    payload=store.execute('SELECT payload FROM outbox WHERE id=?',(oid,)).fetchone()[0]
    checkpoint={'outbox_id':oid,'payload_sha256':hashlib.sha256(payload.encode()).hexdigest(),'cycle':cycle,'used':40,'entities':4,'fake_property_requests':len(transport.calls),'simulated_prior_account_usage_added':40-len(transport.calls),'real_provider_requests':0,'real_emails':0}
    report.update(synthetic=True,dry_run=True,authorized_mls_connected=False)
    out.mkdir(parents=True,exist_ok=True);body=render(report)
    for suffix,value in [('json',json.dumps(report,indent=2)),('html',body['html']),('txt',body['text'])]:(out/('report.'+suffix)).write_text(value)
    (out/'checkpoint.json').write_text(json.dumps(checkpoint,indent=2))
    return checkpoint

def recover(store,boundary,checkpoint):
    *_,cfg,settings=configuration();store.migrate();store.migrate()
    assert store.execute('SELECT COUNT(*) FROM discovery_entities').fetchone()[0]==checkpoint['entities']
    assert store.count_usage(checkpoint['cycle'])==40
    payload=store.execute('SELECT payload FROM outbox WHERE id=?',(checkpoint['outbox_id'],)).fetchone()[0]
    assert hashlib.sha256(payload.encode()).hexdigest()==checkpoint['payload_sha256']
    transport=FixtureTransport();now=NOW+dt.timedelta(minutes=21)
    result=daily(store,boundary,replace(settings,email_enabled=True),'2026-10-24',cfg=cfg,clock=lambda:now,opener=transport)
    assert result['status']=='sent' and result['sources']==[] and len(transport.messages)==1
    assert daily(store,boundary,replace(settings,email_enabled=True),'2026-10-24',cfg=cfg,clock=lambda:now,opener=transport)['status']=='already_sent'
    assert len(transport.calls)==1 and store.count_usage(checkpoint['cycle'])==40
    return dict(recovered=True,identical_frozen_payload=True,duplicate_send_prevented=True,real_emails=0,real_provider_requests=0)

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--recover',action='store_true');a=p.parse_args()
    url=os.environ['HARDENING_TEST_DATABASE_URL'];parts=urlsplit(url)
    if parts.hostname not in {'127.0.0.1','localhost'} or not parts.path.endswith('_test') or parts.query or parts.fragment:raise PermissionError('Explicit disposable LOCAL test database required')
    import psycopg
    state=a.output/'schema.json'
    with psycopg.connect(url,autocommit=True) as admin:
        schema=json.loads(state.read_text())['schema'] if a.recover else 'hardening_'+uuid.uuid4().hex
        if not re.fullmatch(r'hardening_[0-9a-f]{32}',schema):raise PermissionError('Invalid isolated test schema')
        if not a.recover:admin.execute('CREATE SCHEMA '+schema)
    store=Store(url+'?options='+quote('-csearch_path='+schema));boundary=load_boundary('data/pittsburgh.geojson')
    def deny(*args,**kwargs):raise AssertionError('External provider traffic forbidden')
    try:
        with patch('urllib.request.OpenerDirector.open',deny),patch.dict(os.environ,{'RENTCAST_API_KEY':'fixture-only','RESEND_API_KEY':'fixture-only'}):
            if a.recover:
                result=recover(store,boundary,json.loads((a.output/'checkpoint.json').read_text()));(a.output/'recovery.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
            else:
                store.migrate();result=simulate(store,boundary,a.output);state.write_text(json.dumps({'schema':schema}));print(json.dumps(result))
    finally:store.close()
if __name__=='__main__':main()
