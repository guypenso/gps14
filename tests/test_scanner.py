import datetime as dt
import json
from dataclasses import replace
import pytest
from rentalsourcing.engine import Engine
from rentalsourcing.digest import Digest,ReconciliationRequired
from rentalsourcing.collector import Client
from rentalsourcing.core import verification,tax_verification
from conftest import listing,taxes,Provider


def execute(store,boundary,settings,clock,provider,job='daily:test'):
    client=Client(store,settings,clock,provider,lambda _:None)
    stats=Engine(store,boundary,client,settings,clock).run(job,clock().date().isoformat())
    return stats,client


def test_new_digest_cached_tax_and_fresh_price_reduction(store,boundary,settings,clock,provider):
    stats,c=execute(store,boundary,settings,clock,provider)
    assert c.count==2
    assert store.get_property('fixture-property')['state']=='Qualified'
    digest=Digest(store,boundary,settings,clock,provider)
    message=digest.prepare('daily:test',clock().date().isoformat(),stats)
    assert digest.send(message)=='sent'
    assert len(provider.messages)==1
    assert '1,600.00' in provider.messages[0]['text']
    assert digest.send(message)=='already_sent'
    clock.advance(days=1)
    stats,c=execute(store,boundary,settings,clock,provider,'daily:next')
    assert c.count==1 and stats['tax_cache_hits']==1
    second=digest.prepare('daily:next',clock().date().isoformat(),stats);digest.send(second)
    assert store.execute('SELECT event_ids FROM outbox WHERE id=?',(second,)).fetchone()[0]=='[]'
    clock.advance(days=1);provider.rows[0]['price']=170000
    stats,c=execute(store,boundary,settings,clock,provider,'daily:reduction')
    third=digest.prepare('daily:reduction',clock().date().isoformat(),stats);digest.send(third)
    assert 'הורדת מחיר חדשה' in provider.messages[-1]['text']
    assert '180,000.00' in provider.messages[-1]['text']
    assert provider.tax_calls==1
    assert store.execute('SELECT COUNT(*) FROM price_history').fetchone()[0]==2


@pytest.mark.parametrize('change,expected',[
    ({'bedrooms':None},'Pending Verification'),({'bathrooms':None},'Pending Verification'),
    ({'squareFootage':None},'Pending Verification'),({'price':None},'Pending Verification'),
    ({'longitude':None},'Pending Verification'),({'zipCode':'15210'},'Rejected'),
    ({'zipCode':'15203'},'Rejected'),({'price':200001},'Rejected'),
    ({'bedrooms':2},'Rejected'),({'bathrooms':0},'Rejected'),({'squareFootage':999},'Rejected'),
    ({'latitude':40.52},'Rejected'),({'longitude':-80.1},'Rejected'),
    ({'status':'Inactive'},'Rejected'),({'demo':True},'Rejected')])
def test_missing_fields_persist_pending_and_known_failures_rejected(store,boundary,settings,clock,change,expected):
    provider=Provider([listing(**change)])
    stats,c=execute(store,boundary,settings,clock,provider)
    assert store.get_property('fixture-property')['state']==expected
    assert provider.tax_calls==0
    d=Digest(store,boundary,settings,clock,provider)
    message=d.prepare('daily:test',clock().date().isoformat(),stats)
    assert json.loads(store.execute('SELECT event_ids FROM outbox WHERE id=?',(message,)).fetchone()[0])==[]


@pytest.mark.parametrize('amount,expected',[(1899,'Qualified'),(1899.99,'Qualified'),(1900,'Rejected'),(None,'Pending Verification')])
def test_real_schema_tax_threshold(store,boundary,settings,clock,provider,amount,expected):
    provider.records['fixture-property']=taxes(amount=amount)
    execute(store,boundary,settings,clock,provider)
    assert store.get_property('fixture-property')['state']==expected
    assert store.execute('SELECT COUNT(*) FROM tax_history').fetchone()[0]==1


def test_no_fallback_to_older_valid_tax():
    record={'propertyTaxes':{'2024':{'year':2024,'total':100},'2025':{'year':2025,'total':None}}}
    assert tax_verification(record)[0]=='Pending Verification'


def test_pagination_and_resume_at_saved_cursor(store,boundary,settings,clock):
    provider=Provider([listing(str(i)) for i in range(5)])
    limited=replace(settings,per_run=1)
    stats,c=execute(store,boundary,limited,clock,provider)
    assert stats['partial']
    assert store.job('daily:test','x')['offset']==2
    # Properties awaiting a tax check must not appear as approved candidates.
    assert store.get_property('0')['state']=='Pending Verification'
    stats,c=execute(store,boundary,settings,clock,provider)
    assert not stats['partial'] and stats['received']==5
    listing_urls=[r.full_url for r in provider.calls if '/listings/sale' in r.full_url]
    assert sum('offset=0' in u for u in listing_urls)==1
    assert len(listing_urls)==3
    assert provider.tax_calls==5


def test_network_failure_then_manual_resume(store,boundary,settings,clock,provider):
    import urllib.error
    provider.failures=[urllib.error.URLError(TimeoutError('simulated timeout'))]
    with pytest.raises(urllib.error.URLError):execute(store,boundary,settings,clock,provider)
    assert store.job('daily:test','x')['state']=='failed'
    stats,c=execute(store,boundary,settings,clock,provider)
    assert not stats['partial'] and store.job('daily:test','x')['state']=='complete'
    assert store.execute('SELECT COUNT(*) FROM runs').fetchone()[0]==2


def test_resend_uncertain_retries_same_key_and_body(store,boundary,settings,clock,provider):
    stats,_=execute(store,boundary,settings,clock,provider)
    d=Digest(store,boundary,settings,clock,provider);identity=d.prepare('daily:test',clock().date().isoformat(),stats)
    provider.failures=[TimeoutError('simulated email timeout')]
    with pytest.raises(TimeoutError):d.send(identity)
    first=provider.calls[-1]
    assert d.send(identity)=='sent'
    assert provider.calls[-1].data==first.data
    assert provider.calls[-1].get_header('Idempotency-key')==first.get_header('Idempotency-key')
    assert len(provider.messages)==1


def test_resend_does_not_retry_after_idempotency_window(store,boundary,settings,clock,provider):
    stats,_=execute(store,boundary,settings,clock,provider)
    d=Digest(store,boundary,settings,clock,provider);identity=d.prepare('daily:test',clock().date().isoformat(),stats)
    provider.failures=[TimeoutError()]
    with pytest.raises(TimeoutError):d.send(identity)
    count=len(provider.calls);clock.advance(hours=24)
    with pytest.raises(ReconciliationRequired):d.send(identity)
    assert len(provider.calls)==count
    d.reconcile(identity,'accepted-id-checked-in-provider')
    assert d.send(identity)=='already_sent'


def test_stale_prepared_digest_cancelled_before_network(store,boundary,settings,clock,provider):
    stats,_=execute(store,boundary,settings,clock,provider)
    d=Digest(store,boundary,settings,clock,provider);identity=d.prepare('daily:test',clock().date().isoformat(),stats)
    clock.advance(hours=25);count=len(provider.calls)
    assert d.send(identity)=='cancelled_stale'
    assert len(provider.calls)==count


def test_price_reduction_candidates_checked_first(store,boundary,settings,clock):
    provider=Provider([listing('a-normal'),listing('z-reduced',history={'2026-10-01':{'price':190000}})])
    stats,c=execute(store,boundary,replace(settings,per_run=2),clock,provider)
    assert stats['partial']
    assert provider.calls[-1].full_url.endswith('/properties/z-reduced')


def test_only_one_daily_digest_even_after_manual_scan(store,boundary,settings,clock,provider):
    stats,_=execute(store,boundary,settings,clock,provider)
    d=Digest(store,boundary,settings,clock,provider);first=d.prepare('daily:test',clock().date().isoformat(),stats);d.send(first)
    provider.rows[0]['price']=170000;clock.advance(minutes=16)
    stats,_=execute(store,boundary,settings,clock,provider,'manual:new')
    assert d.prepare('manual:new',clock().date().isoformat(),stats)==first
    assert d.send(first)=='already_sent'


def test_non_live_source_never_qualifies(store,boundary,settings,clock):
    class FixtureClient:
        source='fixture'
        def get(self,*args,**kw):return [listing()],1
    Engine(store,boundary,FixtureClient(),settings,clock).run('fixture',clock().date().isoformat())
    assert store.get_property('fixture-property')['state']=='Pending Verification'


def test_price_cut_after_daily_digest_is_sent_next_day(store,boundary,settings,clock,provider):
    stats,_=execute(store,boundary,settings,clock,provider)
    d=Digest(store,boundary,settings,clock,provider)
    first=d.prepare('daily:test',clock().date().isoformat(),stats);d.send(first)
    clock.advance(minutes=16);provider.rows[0]['price']=170000
    stats,_=execute(store,boundary,settings,clock,provider,'manual:cut')
    assert d.prepare('manual:cut',clock().date().isoformat(),stats)==first
    clock.advance(days=1)
    stats,_=execute(store,boundary,settings,clock,provider,'daily:tomorrow')
    message=d.prepare('daily:tomorrow',clock().date().isoformat(),stats);d.send(message)
    assert 'הורדת מחיר חדשה' in provider.messages[-1]['text']
    assert '170,000.00' in provider.messages[-1]['text']


def test_missing_tax_is_rechecked_after_short_negative_cache(store,boundary,settings,clock,provider):
    provider.records['fixture-property']={'id':'fixture-property'}
    execute(store,boundary,settings,clock,provider)
    assert store.get_property('fixture-property')['state']=='Pending Verification'
    clock.advance(hours=7);provider.records['fixture-property']=taxes()
    execute(store,boundary,settings,clock,provider,'manual:verified')
    assert store.get_property('fixture-property')['state']=='Qualified' and provider.tax_calls==2


def test_malformed_property_id_is_pending_not_a_network_request(store,boundary,settings,clock):
    provider=Provider([]);provider.rows=[listing(id={'bad':'id'})]
    execute(store,boundary,settings,clock,provider)
    assert provider.tax_calls==0
    assert store.execute("SELECT COUNT(*) FROM properties WHERE state='Pending Verification'").fetchone()[0]==1


def test_fractional_listing_price_is_preserved_through_postgres(store,boundary,settings,clock):
    provider=Provider([listing(price=180000.01)])
    stats,_=execute(store,boundary,settings,clock,provider)
    d=Digest(store,boundary,settings,clock,provider)
    message=d.prepare('daily:test',clock().date().isoformat(),stats);d.send(message)
    assert '180,000.01' in provider.messages[-1]['text']
    assert len(json.loads(store.execute('SELECT event_ids FROM outbox WHERE id=?',(message,)).fetchone()[0]))==1


def test_rejected_email_releases_events_for_next_day(store,boundary,settings,clock,provider):
    import urllib.error
    stats,_=execute(store,boundary,settings,clock,provider)
    d=Digest(store,boundary,settings,clock,provider);identity=d.prepare('daily:test',clock().date().isoformat(),stats)
    provider.failures=[urllib.error.HTTPError('https://api.resend.com/emails',422,'invalid sender',{},None)]
    with pytest.raises(urllib.error.HTTPError):d.send(identity)
    assert store.execute('SELECT state FROM outbox WHERE id=?',(identity,)).fetchone()[0]=='failed'
    clock.advance(days=1)
    stats,_=execute(store,boundary,settings,clock,provider,'daily:fixed')
    next_id=d.prepare('daily:fixed',clock().date().isoformat(),stats);d.send(next_id)
    assert 'FICTIONAL TEST ADDRESS' in provider.messages[-1]['text']
