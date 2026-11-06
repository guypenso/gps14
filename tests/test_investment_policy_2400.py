import datetime as dt
import json
import pytest
from conftest import listing, taxes
from rentalsourcing.core import latest_tax, tax_verification, verification
from rentalsourcing.collector import LIVE
from rentalsourcing.listing_intake import evaluate
from rentalsourcing.criteria import reevaluate
from rentalsourcing.offmarket import claim_schedule, next_source_refresh

@pytest.mark.parametrize('amount,expected',[(1900,'Qualified'),(2299.99,'Qualified'),(2300,'Qualified'),(2300.001,'Rejected'),(2300.01,'Rejected')])
def test_inclusive_tax_ceiling_without_rounding_down(amount,expected):
    assert tax_verification(taxes(amount=amount))[0]==expected
    assert bool(latest_tax(taxes(amount=amount)))==(expected=='Qualified')

def test_tax_change_preserves_identity_decisions_and_unverified_evidence(store,boundary,clock):
    p=listing('known',addressLine1='100 Fixture Ave')
    store.save_property('known',p,'Rejected',['annual_tax_too_high'],LIVE,1,clock().isoformat())
    store.save_tax('known',taxes('known',2300),LIVE,clock().isoformat(),(clock()+dt.timedelta(days=7)).isoformat())
    store.save_setting('penten_review:known',{'status':'rejected','reviewed_at':clock().isoformat()})
    reevaluate(store,boundary,clock())
    from test_listing_intake import alert
    result=evaluate(store,boundary,[alert()],clock())
    assert not result['cards'] and result['counts']['owner_deferred']==1
    assert json.loads(store.execute('SELECT payload FROM settings WHERE name=?',('penten_review:known',)).fetchone()[0])['status']=='rejected'
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    assert tax_verification({'id':'estimate','taxAssessments':{'2026':{'value':50000}}})[0]=='Pending Verification'
    from rentalsourcing.listing_intake import audit
    change=audit(store,boundary,clock())['summary']['tax_ceiling_recheck']
    assert change['cached_records_newly_passing_tax']==1
    assert change['new_qualified_opportunities']==0
    assert change['records'][0]['human_decision_preserved']

def test_price_and_single_family_requirements(boundary):
    assert verification(listing(price=200001),boundary)[0]=='Candidate'
    assert verification(listing(price=200001.01),boundary)[0]=='Rejected'
    assert verification(listing(propertyType='Duplex'),boundary)[0]=='Rejected'
    assert verification(listing(propertyType='Unknown'),boundary)[0]=='Pending Verification'

def test_existing_worker_pre_report_claim_and_bounded_drain(store):
    # November 8, 07:45 Israel; a stale six-hour next_at must not skip the report cutoff.
    now=dt.datetime(2026,11,8,5,45,tzinfo=dt.timezone.utc)
    store.save_setting('offmarket_scheduler',{'next_at':(now+dt.timedelta(hours=5)).isoformat()})
    assert claim_schedule(store,now)
    assert not claim_schedule(store,now)
    before=now-dt.timedelta(hours=1)
    assert next_source_refresh(before,{'state':'success'})==now.isoformat()
    assert next_source_refresh(before,{'state':'partial'})==(before+dt.timedelta(minutes=5)).isoformat()
    from rentalsourcing.offmarket import listing_operation_id
    first=listing_operation_id(before.isoformat());second=listing_operation_id((before+dt.timedelta(minutes=5)).isoformat())
    assert first!=second
    assert first==listing_operation_id(before.isoformat())
    assert listing_operation_id(before.isoformat(),{'id':'fixed-validation'})=='validation:fixed-validation'

def test_daily_report_includes_only_fresh_unreviewed_source_evidence(store,boundary,clock):
    from test_listing_intake import alert
    from rentalsourcing.listing_report import listing_report, section
    evaluate(store,boundary,[alert()],clock())
    report=listing_report(store,boundary,clock())
    assert len(report['cards'])==1 and report['cards'][0]['qualification']=='Pending Verification'
    text,html=section(report)
    assert 'RealScout' in text and 'Pending Verification' in html
    identity=report['cards'][0]['source_id']
    store.save_setting('penten_review:'+identity,{'status':'monitor'})
    assert not listing_report(store,boundary,clock())['cards']
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    store.save_setting('listing_pre_report_receipt',{'at':clock().isoformat(),'result':{'state':'failed'}})
    assert listing_report(store,boundary,clock())['state']=='failed'

def test_pre_report_intake_uses_authenticated_source_only_endpoint_without_sending(store,clock,monkeypatch):
    from conftest import Response
    from rentalsourcing.listing_report import sync_before_report
    monkeypatch.setattr('rentalsourcing.command_center.review_configuration',lambda:('https://fixture.test/api/rentalsourcing/review-feedback','s'*32,''))
    class Opener:
        calls=[]
        def open(self,request,timeout):
            self.calls.append(request)
            assert request.full_url=='https://fixture.test/api/rentalsourcing/offmarket-refresh'
            assert json.loads(request.data)['action']=='listing_refresh'
            assert request.get_header('X-rentalsourcing-signature').startswith('v1=')
            return Response({'state':'success','email':{'authorized_accounts':1,'results':[{'state':'success'}]}})
    opener=Opener();result=sync_before_report(store,'2026-11-08',clock(),opener)
    assert result['state']=='success' and len(opener.calls)==1
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0

def test_actual_scheduled_discovery_runs_selected_sender_intake_before_digest(store,boundary,settings,clock,monkeypatch):
    from conftest import Provider
    from test_minimal_daily import approved
    from rentalsourcing.cloud import daily_discovery
    from rentalsourcing.digest import Digest
    order=[];original=Digest.prepare
    def sync(*args,**kwargs):order.append('selected_sender_intake');return {'state':'success'}
    def prepare(*args,**kwargs):order.append('prepare_daily_digest');return original(*args,**kwargs)
    monkeypatch.setattr('rentalsourcing.listing_report.sync_before_report',sync)
    monkeypatch.setattr(Digest,'prepare',prepare)
    provider=Provider();daily_discovery(store,boundary,approved(settings),clock().date().isoformat(),clock=clock,opener=provider)
    assert order==['selected_sender_intake','prepare_daily_digest']
    assert len(provider.messages)==1

def test_official_missing_suffix_match_preserves_existing_rejection(store,boundary,clock):
    import hashlib
    from test_listing_intake import alert
    from rentalsourcing.offmarket import street_key
    from rentalsourcing.listing_intake import audit, reconcile_official_identities
    p=listing('known',addressLine1='100 Fixture St',formattedAddress='100 Fixture St, Pittsburgh, PA 15212')
    store.save_property('known',p,'Pending Verification',[],LIVE,1,clock().isoformat())
    store.save_setting('penten_review:known',{'status':'rejected','reviewed_at':clock().isoformat()})
    item=alert(addressLine1='100 Fixture',formattedAddress='100 Fixture, Pittsburgh, PA 15212',latitude=None,longitude=None)
    key='listing_geocode:'+hashlib.sha256((street_key(item['property'])+'|15212').encode()).hexdigest()
    store.save_setting(key,{'matched':True,'matched_address':'100 FIXTURE ST, PITTSBURGH, PA, 15212','latitude':40.45,'longitude':-80,'next_at':(clock()+dt.timedelta(days=7)).isoformat()})
    result=evaluate(store,boundary,[item],clock())
    assert result['cards']==[] and result['evaluations'][0]['source_id']=='known'
    shadow={**result['evaluations'][0],'source_id':'old-shadow','property':{**result['evaluations'][0]['property'],'id':'old-shadow'}}
    store.save_setting('listing_identity:old-shadow',shadow)
    assert reconcile_official_identities(store,clock())==1
    after=audit(store,boundary,clock());assert after['summary']['unique_properties']==1
    assert after['identity_reconciliations'][0]['to_source_id']=='known'
    assert after['identity_reconciliations'][0]['canonical_review_rejected']
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
