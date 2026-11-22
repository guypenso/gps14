import datetime as dt
from conftest import listing,taxes
from rentalsourcing.core import tax_verification
from rentalsourcing.criteria import reconcile_tax_policy
from rentalsourcing.collector import LIVE
from rentalsourcing.collector import tax_expiry
from rentalsourcing.investment_policy import SCREENING_POLICY_VERSION,annual_tax_passes


def test_bedroom_specific_verified_tax_and_buffer():
    for beds,amount,expected in [
        (2,1899.99,'Qualified'),(2,1900,'Qualified'),(2,1900.01,'Rejected'),(2,2000,'Rejected'),
        (3,2899.99,'Qualified'),(3,2900,'Qualified'),(3,2900.01,'Rejected'),(3,3000,'Rejected'),
        (4,2899.99,'Qualified'),(4,2900,'Qualified'),(4,2900.01,'Rejected'),(4,3000,'Rejected')]:
        record=taxes(amount=amount,bedrooms=beds)
        assert tax_verification(record,dt.date(2026,11,22))[0]==expected
        assert annual_tax_passes(amount,beds)==(expected=='Qualified')
    assert tax_verification(taxes(amount=1800,bedrooms=None))[0]=='Pending Verification'
    assert tax_verification({'id':'x','bedrooms':4,'taxAssessments':{'2026':{'value':50000}}})[0]=='Pending Verification'


def test_valid_provider_tax_history_retains_full_cache_ttl_without_bedrooms(settings,clock):
    record=taxes('cache',2800,bedrooms=None)
    complete=tax_expiry(record,clock(),settings)
    incomplete=tax_expiry({'id':'cache','propertyTaxes':{}},clock(),settings)
    assert complete>incomplete


def test_stored_policy_backfill_preserves_owner_and_listing_dates(store,boundary,clock):
    for name,beds,tax,status,owner in [
        ('three',3,2600,'Rejected',None),('four',4,2800,'Rejected',None),
        ('owner',4,2700,'Rejected','rejected'),('two',2,1950,'Rejected',None),
        ('missing',4,None,'Pending Verification',None)]:
        row=listing(name,bedrooms=beds,price=140000)
        store.save_property(name,row,status,['annual_tax_too_high'] if status=='Rejected' else ['tax_verification_required'],LIVE,1,clock().isoformat())
        if tax is not None:store.save_tax(name,taxes(name,tax,bedrooms=beds),LIVE,clock().isoformat(),(clock()+dt.timedelta(days=7)).isoformat())
        if owner:store.save_setting('penten_review:'+name,{'status':owner,'reviewed_at':clock().isoformat()})
    before={i:store.get_property(i)['observed_at'] for i in ('three','four','owner','two','missing')}
    result=reconcile_tax_policy(store,boundary,clock())
    assert result['policy_version']==SCREENING_POLICY_VERSION and result['stored_reevaluated']==5
    assert result['newly_eligible_3_bedrooms']==1
    assert result['newly_eligible_4_bedrooms']==2  # One remains owner rejected.
    assert result['manual_decisions_preserved']==1
    assert set(result['source_ids'])=={'three','four'}
    assert store.get_property('three')['state']=='Qualified'
    assert store.get_property('four')['state']=='Qualified'
    assert store.get_property('owner')['state']=='Rejected'
    assert store.get_property('two')['state']=='Rejected'
    assert store.get_property('missing')['state']=='Pending Verification'
    assert before=={i:store.get_property(i)['observed_at'] for i in before}
    assert reconcile_tax_policy(store,boundary,clock())==result
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0


def test_policy_backfill_preserves_managed_deal_and_conditional_watch(store,boundary,clock):
    for identity,decision in (
        ('managed',{'status':'monitor'}),
        ('watched',{'status':'new','conditional_watch':{'active':True,'target_price':100000}}),
    ):
        row=listing(identity,bedrooms=4,price=140000)
        store.save_property(identity,row,'Rejected',['annual_tax_too_high'],LIVE,1,clock().isoformat())
        store.save_tax(identity,taxes(identity,2800,bedrooms=4),LIVE,clock().isoformat(),(clock()+dt.timedelta(days=7)).isoformat())
        store.save_setting('penten_review:'+identity,decision)
    result=reconcile_tax_policy(store,boundary,clock())
    assert result['manual_decisions_preserved']==2
    assert result['fresh_delivery_candidates']==0
    for identity in ('managed','watched'):
        assert store.get_property(identity)['state']=='Rejected'
        assert store.execute('SELECT payload FROM settings WHERE name=?',('penten_review:'+identity,)).fetchone()
