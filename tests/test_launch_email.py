import datetime as dt
import json
from dataclasses import replace
import pytest
from rentalsourcing.digest import render, Digest, recent_timestamp
from rentalsourcing.engine import Engine
from rentalsourcing.collector import Client
from conftest import Provider, listing


def test_incomplete_scan_cannot_claim_no_deals():
    for stats in [{'partial':True},{'error_type':'TimeoutError','partial':True}]:
        text,markup=render('2026-10-16',[],stats,{'remaining':0},[])
        assert 'לא ניתן לקבוע שאין עסקאות חדשות' in text
        assert 'אין נכסים חדשים או הורדות מחיר חדשות שעברו' not in text
        assert 'לא ניתן לקבוע' in markup
    text,_=render('2026-10-16',[],{}, {'remaining':1},[])
    assert 'אין נכסים חדשים או הורדות מחיר חדשות שעברו' in text


def test_real_listing_link_is_rendered_and_unsafe_links_are_not(store,boundary,settings,clock):
    provider=Provider([listing(listingUrl='https://broker.example.test/listing/123')])
    c=Client(store,settings,clock,provider,lambda _:None)
    stats=Engine(store,boundary,c,settings,clock).run('email-links',clock().date().isoformat())
    d=Digest(store,boundary,settings,clock,provider)
    key=d.prepare('email-links',clock().date().isoformat(),stats)
    body=json.loads(store.execute('SELECT payload FROM outbox WHERE id=?',(key,)).fetchone()[0])
    assert 'https://broker.example.test/listing/123' in body['text']
    assert 'width=device-width' in body['html']


def test_failed_digest_subject_reports_failure(store,boundary,settings,clock):
    d=Digest(store,boundary,settings,clock)
    key=d.prepare('failed',clock().date().isoformat(),{'partial':True,'error_type':'TimeoutError'})
    body=json.loads(store.execute('SELECT payload FROM outbox WHERE id=?',(key,)).fetchone()[0])
    assert 'תקלה בסריקה' in body['subject']
    assert 'TimeoutError' in body['text'] and 'TimeoutError' in body['html']


def test_future_and_naive_evidence_never_qualifies_email():
    now=dt.datetime(2026,10,16,12,tzinfo=dt.timezone.utc)
    assert not recent_timestamp('2026-10-16T13:00:00+00:00',now)
    assert not recent_timestamp('2026-10-16T11:00:00',now)
    assert not recent_timestamp(None,now)
    assert recent_timestamp('2026-10-16T11:00:00Z',now)


def test_invalid_listing_link_not_fabricated():
    from rentalsourcing.digest import listing_link
    for url in ['javascript:alert(1)','http://broker.example.test/a','https://user:pass@broker.example.test/a',None]:
        assert listing_link({'listingUrl':url}) is None


def test_recovered_scan_clears_previous_error_and_records_run_times(store,boundary,settings,clock):
    import urllib.error
    provider=Provider([listing()]);provider.failures=[urllib.error.URLError('fixture failure')]
    engine=Engine(store,boundary,Client(store,settings,clock,provider,lambda _:None),settings,clock)
    with pytest.raises(urllib.error.URLError):engine.run('recovered',clock().date().isoformat())
    assert store.job('recovered','ignored')['stats']['error_type']=='URLError'
    clock.advance(minutes=16)
    result=engine.run('recovered',clock().date().isoformat())
    assert 'error_type' not in result and not result['partial']
    for (payload,) in store.execute('SELECT payload FROM runs').fetchall():
        run=json.loads(payload)
        assert run.get('started_at') and run.get('finished_at')
