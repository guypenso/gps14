"""Durable backlog progression under small quotas; no real provider traffic."""
from dataclasses import replace

from conftest import Provider,listing,taxes
from rentalsourcing.cloud import scheduled_job
from rentalsourcing.collector import Client
from rentalsourcing.engine import Engine


def run(store,boundary,settings,clock,provider,job):
    client=Client(store,settings,clock,provider,lambda _:None)
    return Engine(store,boundary,client,settings,clock).run(job,clock().date().isoformat())


def test_small_run_enriches_before_all_listing_pages_and_resumes_cursor(store,boundary,settings,clock):
    provider=Provider([listing(str(i)) for i in range(6)])
    limited=replace(settings,per_run=2)
    result=run(store,boundary,limited,clock,provider,'daily:first')
    assert result['partial'] and provider.tax_calls==1
    assert store.job('daily:first','ignored')['offset']==2
    assert scheduled_job(store,'2026-10-17')=='daily:first'
    run(store,boundary,limited,clock,provider,'daily:first')
    assert store.job('daily:first','ignored')['offset']==4
    assert provider.tax_calls==2
    # The second page did not jump ahead of the older unprocessed first-page row.
    assert store.get_property('1')['state']=='Qualified'
    assert store.get_property('2')['state']=='Pending Verification'


def test_missing_tax_is_not_completed_and_retries_after_negative_cache(store,boundary,settings,clock):
    provider=Provider([listing('pending')]);provider.records['pending']={'id':'pending'}
    result=run(store,boundary,settings,clock,provider,'daily:first')
    assert result['partial'] and provider.tax_calls==1
    assert store.execute('SELECT done FROM scan_tasks').fetchone()[0]==0
    # Same job remains resumable, and the negative cache suppresses another call.
    run(store,boundary,settings,clock,provider,'daily:first')
    assert provider.tax_calls==1
    clock.advance(hours=7);provider.records['pending']=taxes('pending')
    result=run(store,boundary,settings,clock,provider,'daily:first')
    assert not result['partial'] and provider.tax_calls==2
    assert store.get_property('pending')['state']=='Qualified'


def test_new_day_prioritizes_new_listing_over_old_backlog(store,boundary,settings,clock):
    provider=Provider([listing('z-old')])
    run(store,boundary,replace(settings,per_run=1),clock,provider,'daily:first')
    clock.advance(days=1)
    provider.rows=[listing('a-new'),listing('z-old')]
    provider.records['a-new']=taxes('a-new')
    run(store,boundary,replace(settings,per_run=2),clock,provider,'daily:second')
    assert provider.tax_calls==1
    assert store.get_property('a-new')['state']=='Qualified'
    assert store.get_property('z-old')['state']=='Pending Verification'
    assert store.execute('SELECT done FROM scan_tasks WHERE job_id=? AND property_id=?',('daily:first','z-old')).fetchone()[0]==0


def test_unseen_stale_backlog_remains_pending_and_never_qualifies(store,boundary,settings,clock):
    provider=Provider([listing('old')])
    run(store,boundary,replace(settings,per_run=1),clock,provider,'daily:first')
    clock.advance(days=2);provider.rows=[]
    result=run(store,boundary,settings,clock,provider,'daily:second')
    assert result['partial'] and provider.tax_calls==0
    assert store.get_property('old')['state']=='Pending Verification'
    assert store.get_property('old')['reasons']==['stale_listing']
    assert store.execute('SELECT done FROM scan_tasks WHERE job_id=?',('daily:second',)).fetchone()[0]==0
    # A tax-only backlog must not prevent tomorrow's fresh listing collection.
    assert scheduled_job(store,'2026-10-19')=='daily:2026-10-19'


def test_cached_tax_identity_is_verified_before_approval(store,boundary,settings,clock):
    import datetime as dt
    store.save_tax('expected',taxes('different'),'rentcast-live',clock().isoformat(),(clock()+dt.timedelta(days=1)).isoformat())
    provider=Provider([listing('expected')])
    result=run(store,boundary,settings,clock,provider,'daily:identity')
    assert result['partial'] and provider.tax_calls==0
    assert store.get_property('expected')['state']=='Pending Verification'
    assert store.get_property('expected')['reasons']==['tax_identity_or_source_mismatch']
