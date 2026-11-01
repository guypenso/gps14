import datetime as dt,hashlib,json,urllib.parse
from dataclasses import replace
import pytest
from conftest import listing,taxes,Provider
from rentalsourcing.tax_backlog import reconcile,review_key,priorities,RESEARCH
from rentalsourcing.collector import LIVE,Client
from rentalsourcing.engine import Engine
from rentalsourcing.criteria import reevaluate


def seed(store,row,now):
    store.save_property(row['id'],row,'Pending Verification',['tax_not_verified'],LIVE,1,now.isoformat())


def cache(store,row,record,clock,source=LIVE,age=dt.timedelta()):
    when=clock()-age
    key=hashlib.sha256(('https://api.rentcast.io/v1/properties/'+urllib.parse.quote(row['id'],safe='')).encode()).hexdigest()
    store.cache_put(key,{'data':record,'fetched_at':when.isoformat(),'total':None},source,(clock()+dt.timedelta(hours=1)).isoformat())


def test_recover_valid_cache_without_refreshing_ttl_or_spending(store,boundary,settings,clock):
    row=listing();seed(store,row,clock());cache(store,row,taxes(),clock)
    assert reconcile(store,settings,clock())['resolved_tax_decisions']==1
    old=store.get_tax(row['id'],clock().isoformat())
    reevaluate(store,boundary,clock());assert store.get_property(row['id'])['state']=='Qualified'
    assert reconcile(store,settings,clock())['recovered_cache_records']==0
    assert store.get_tax(row['id'],clock().isoformat())==old
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0

@pytest.mark.parametrize('kind',['wrong-id','fixture','expired-evidence','assessment','missing-tax'])
def test_untrusted_or_incomplete_records_never_qualify(store,boundary,settings,clock,kind):
    row=listing();seed(store,row,clock());record=taxes();source=LIVE;age=dt.timedelta()
    if kind=='wrong-id':record['id']='different'
    if kind=='fixture':source='fixture'
    if kind=='expired-evidence':age=dt.timedelta(days=10)
    if kind=='assessment':record={'id':row['id'],'COUNTYTOTAL':1500,'LOCALTOTAL':1500,'TAXYEAR':2026}
    if kind=='missing-tax':record={'id':row['id']}
    cache(store,row,record,clock,source,age)
    reconcile(store,settings,clock());reevaluate(store,boundary,clock())
    assert store.get_property(row['id'])['state']=='Pending Verification'


def test_latest_tax_rejection_is_preserved(store,boundary,settings,clock):
    row=listing();seed(store,row,clock());cache(store,row,taxes(amount=1900),clock)
    reconcile(store,settings,clock());reevaluate(store,boundary,clock())
    assert store.get_property(row['id'])['state']=='Rejected'
    assert priorities(store,boundary,clock())==[]


def test_factual_order_does_not_create_tax_or_yield(store,boundary,clock):
    for row in [listing('expensive',price=190000),listing('lower',price=100000),listing('large',price=100000,squareFootage=1600),listing('excluded',zipCode='15211'),listing('townhome',propertyType='Townhome')]:seed(store,row,clock())
    ranked=priorities(store,boundary,clock())
    assert [p['source_id'] for p in ranked]==['large','lower','expensive']
    assert all(p['tax_status']=='pending_verification' for p in ranked)
    assert all('yield' not in p and 'annual_tax' not in p for p in ranked)


def test_price_reduction_priority_and_old_queue_fairness(store,boundary,settings,clock):
    provider=Provider([listing('old',price=180000)])
    s=replace(settings,page_size=20,per_run=1,max_pages=1)
    Engine(store,boundary,Client(store,s,clock,provider,lambda _:None),s,clock).run('first','2026-10-16')
    clock.advance(days=1)
    provider.rows=[listing('new',price=50000),listing('old',price=180000)]
    provider.records['new']=taxes('new')
    s=replace(s,per_run=2)
    Engine(store,boundary,Client(store,s,clock,provider,lambda _:None),s,clock).run('second','2026-10-17')
    assert store.get_property('old')['state']=='Qualified'
    assert store.get_property('new')['state']=='Pending Verification'
    a=listing('reduced',price=110000);b=listing('cheap',price=100000)
    assert review_key(a,boundary,clock().isoformat(),clock(),150000)<review_key(b,boundary,clock().isoformat(),clock())
