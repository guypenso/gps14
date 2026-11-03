import datetime as dt,json
from pathlib import Path
import pytest
from conftest import listing,Provider
from rentalsourcing.core import verification,report_eligible,property_type_verification,tax_verification
from rentalsourcing.geography import load_boundary
from rentalsourcing.criteria import reevaluate
from rentalsourcing.condominium import evidence
ROOT=Path(__file__).resolve().parents[1]
@pytest.fixture
def policy():return load_boundary(ROOT/'data/pittsburgh.geojson')
def point(region):
 p=region.geometry.representative_point();return {'longitude':p.x,'latitude':p.y}
@pytest.mark.parametrize('kind',['Condo','Condominium','Condo Unit','Residential Condominium','ResidentialCondominium','CONDOMINIUM UNIT'])
def test_condo_types(kind):
 assert property_type_verification(listing(propertyType=kind))==('Rejected','condominium')
@pytest.mark.parametrize('field',['propertySubtype','propertySubType','ownershipType','commonInterest','legalOwnership'])
def test_subtype_and_ownership_overrides_single_family(field):
 assert property_type_verification(listing(**{field:'Condominium'}))==('Rejected','condominium')
 assert evidence(listing(metadata={field:'Condominium'}))['state']=='excluded'
def test_ambiguous_and_noncondo_prose():
 assert property_type_verification(listing(propertySubType='Condo/Co-op'))==('Pending Verification','condominium_ownership_unverified')
 assert property_type_verification(listing(ownershipType='Unknown'))[0]=='Pending Verification'
 assert property_type_verification(listing(description='Detached home near condos',architecturalStyle='Condo'))==('Candidate',None)
 assert evidence(listing(propertySubType='Non-condominium'))['state']=='clear'
 assert evidence(listing(propertySubType='Condo/Co-op',ownershipType='Condominium'))['state']=='excluded'
@pytest.mark.parametrize('slug',['perry-south','elliott','carrick'])
def test_boundary_not_postal_name_or_description(policy,slug):
 row=listing(**point(policy.excluded_regions[slug]),zipCode='15205',neighborhood='Other',description='Brighton Heights')
 state,reasons=verification(row,policy);assert state=='Rejected';assert slug.replace('-','_')+'_neighborhood' in reasons;assert not report_eligible(row,policy)
 row=listing(**point(policy.bellevue),zipCode='15202',neighborhood='Elliot',description='Near Carrick and Perry South')
 assert verification(row,policy)==('Candidate',[])
 assert verification({**row,'latitude':None},policy)[0]=='Pending Verification'
def test_stored_recheck_is_idempotent_and_preserves_payload_and_history(store,policy):
 from rentalsourcing.engine import Engine
 from rentalsourcing.config import Settings
 now=dt.datetime.now(dt.timezone.utc);engine=Engine(store,policy,Provider([]),Settings.from_env(),clock=lambda:now)
 rows=[listing(id=slug,**point(region)) for slug,region in policy.excluded_regions.items()]
 rows.append(listing(id='condo',**point(policy.bellevue),propertySubType='Condo Unit',zipCode='15202'))
 for row in rows:engine.ingest('fixture',row)
 before=store.execute('SELECT id,payload FROM properties ORDER BY id').fetchall();history=store.execute('SELECT COUNT(*) FROM price_history').fetchone()[0]
 reevaluate(store,policy,now);assert reevaluate(store,policy,now)==0
 assert store.execute('SELECT id,payload FROM properties ORDER BY id').fetchall()==before
 assert store.execute('SELECT COUNT(*) FROM price_history').fetchone()[0]==history
 assert all(store.get_property(row['id'])['state']=='Rejected' for row in rows)

def test_daily_email_snapshot_and_paid_tax_tasks_share_exclusions(store,settings,clock,policy):
 from dataclasses import replace
 from rentalsourcing.collector import Client
 from rentalsourcing.engine import Engine
 from rentalsourcing.digest import Digest
 rows=[listing(id=slug,**point(region)) for slug,region in policy.excluded_regions.items()]
 rows.extend([listing(id='condo',**point(policy.bellevue),zipCode='15202',ownershipType='Condominium'),listing(id='allowed',**point(policy.bellevue),zipCode='15202'),listing(id='ambiguous',**point(policy.bellevue),zipCode='15202',propertySubType='Condo/Co-op')])
 provider=Provider(rows);config=replace(settings,page_size=100);client=Client(store,config,clock,provider,lambda _:None)
 stats=Engine(store,policy,client,config,clock).run('synthetic-exclusions',clock().date().isoformat())
 assert provider.tax_calls==1
 assert all('/properties/allowed' in r.full_url for r in provider.calls if '/properties/' in r.full_url)
 digest=Digest(store,policy,config,clock,provider);identity=digest.prepare('synthetic-exclusions',clock().date().isoformat(),stats)
 report=json.loads(store.execute('SELECT payload FROM command_center_reports WHERE outbox_id=?',(identity,)).fetchone()[0])
 assert {p['source_id'] for p in report['opportunities']}=={'allowed','ambiguous'}
 assert next(p for p in report['opportunities'] if p['source_id']=='ambiguous')['qualification']=='pending_verification'
 assert not provider.messages
