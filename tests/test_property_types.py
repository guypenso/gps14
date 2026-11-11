import pytest,json
from pathlib import Path
from rentalsourcing.property_types import property_type_evidence
from rentalsourcing.core import property_type_verification
@pytest.mark.parametrize('value',['Townhouse','Town Home','Row House','Rowhome','Single Family Attached','Townhouse, Single Family Residence','Condominium'])
def test_specific_subtype_cannot_be_overridden(value):
 p={'propertyType':'Single Family','propertyDetails':{'propertySubtypes':[value]}}
 assert property_type_verification(p)[0]=='Rejected'
 assert property_type_evidence(p)['state']=='excluded'
def test_house_description_and_attached_garage_not_townhouse():
 assert property_type_verification({'propertyType':'Single Family','description':'Detached house near townhouses','features':{'garageType':'attached'}})[0]=='Candidate'
def test_subtype_proof_and_ambiguity():
 assert not property_type_evidence({'propertyType':'Single Family'})['detail_confirmed']
 assert property_type_evidence({'propertyType':'Single Family','propertySubType':'Single Family Residence'})['detail_confirmed']
 assert property_type_evidence({'propertyType':'Single Family','attached':True})['state']=='pending'
def test_acorn_and_neighbor_identity():
 assert property_type_evidence({'addressLine1':'3818 Acorn Street','zipCode':'15207','state':'PA'})['reason']=='townhouse'
 assert property_type_evidence({'addressLine1':'3816 Acorn St','zipCode':'15207','state':'PA','propertyType':'Single Family'})['state']=='eligible'
def test_source_preserves_type_fields_and_cached_rich_facts():
 from rentalsourcing.listing_enrichment import evidence
 info=evidence({'propertyType':'Single Family','features':{'propertySubType':'Townhouse','roofReplacementYear':2024}},'2026-11-11T18:00:00Z')
 assert info['property_type_evidence']['state']=='excluded'
 assert info['insight_facts']['roofReplacementYear']==2024
def test_postgres_persistence_recheck_and_identity_are_lossless(store,settings,clock):
 from conftest import listing,Provider
 from rentalsourcing.engine import Engine
 from rentalsourcing.geography import load_boundary
 from rentalsourcing.criteria import reevaluate
 boundary=load_boundary(Path(__file__).resolve().parents[1]/'data/pittsburgh.geojson')
 engine=Engine(store,boundary,Provider([]),settings,clock=clock)
 row=listing(id='unseen-quality-type',propertyDetails={'propertySubType':'Townhouse'},description='New furnace installed in 2024. Detached garage.')
 engine.ingest('quality-fixture',row)
 saved=store.get_property(row['id'])
 assert saved['state']=='Rejected'
 assert saved['payload']['propertyDetails']['propertySubType']=='Townhouse'
 before=store.execute('SELECT COUNT(*) FROM price_history').fetchone()[0]
 reevaluate(store,boundary,clock());reevaluate(store,boundary,clock())
 assert store.execute('SELECT COUNT(*) FROM price_history').fetchone()[0]==before
 assert store.execute('SELECT COUNT(*) FROM properties WHERE id=?',(row['id'],)).fetchone()[0]==1
 # A later broad provider label cannot silently resurrect an excluded subtype.
 engine.ingest('quality-fixture-2',listing(id=row['id'],propertyType='Single Family'))
 assert store.get_property(row['id'])['state']=='Rejected'
 assert property_type_evidence(store.get_property(row['id'])['payload'])['reason']=='townhouse'
@pytest.mark.parametrize('slot',['morning','evening'])
def test_each_scan_runs_stored_quality_audit_without_provider_or_extra_email(store,boundary,settings,clock,monkeypatch,slot):
 from dataclasses import replace
 from conftest import Provider
 from rentalsourcing.cloud import daily_discovery
 import rentalsourcing.command_center as cc
 import rentalsourcing.listing_enrichment as le
 import rentalsourcing.review_feedback as rf
 import rentalsourcing.review_inventory as ri
 import rentalsourcing.listing_report as lr
 import rentalsourcing.future_discovery as fd
 config=('https://private.example.test/api/rentalsourcing/sync','synthetic-key','synthetic-token')
 monkeypatch.setattr(cc,'review_configuration',lambda:config)
 monkeypatch.setattr(rf,'refresh',lambda *a,**kw:{'state':'fresh'})
 monkeypatch.setattr(ri,'run',lambda *a,**kw:{'receipt_counts':{}})
 monkeypatch.setattr(lr,'sync_before_report',lambda *a,**kw:{'state':'success'})
 monkeypatch.setattr(fd,'process',lambda *a,**kw:{'state':'complete'})
 seen=[]
 def audit(*args,**kw):
  seen.append(kw['job_key']);return {'summary':{'property_quality':{'counts':{'review_ready':0},'review_ready_without_insights':0}}}
 monkeypatch.setattr(le,'run',audit)
 provider=Provider([])
 result=daily_discovery(store,boundary,replace(settings,enabled=False,email_enabled=False),clock().date().isoformat(),clock=clock,opener=provider,slot=slot)
 assert seen==['listing-contact-enrichment:'+clock().date().isoformat()+':'+slot+':property-quality-v1']
 assert not provider.messages and not any('api.rentcast.io' in request.full_url for request in provider.calls)
 if slot=='evening':assert result['status']=='evening_no_email'
