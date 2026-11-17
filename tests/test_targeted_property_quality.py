import json,pytest
from conftest import listing
from rentalsourcing.collector import LIVE
from rentalsourcing.listing_enrichment import run
from rentalsourcing.sync_runtime import get,SyncFailure
from test_sync_reliability import Wire,CONFIG


def test_scheduled_quality_targets_fresh_canonical_feedback_and_preserves_source_history(store,clock,monkeypatch):
    for name in ('new','saved','rejected','not-at-destination'):
        store.save_property(name,listing(name),'Pending Verification',[],LIVE,1,clock().isoformat())
    def feedback(*args):
        for name,status in [('new','new'),('saved','pursue'),('rejected','rejected')]:store.save_setting('penten_review:'+name,{'status':status})
        return {'state':'fresh'}
    monkeypatch.setattr('rentalsourcing.review_feedback.refresh',feedback)
    wire=Wire([{'accepted':True,'matched':2,'next_cursor':None,'summary':{}}])
    job='listing-contact-enrichment:fixture:property-quality-v2'
    value=run(store,CONFIG,job_key=job,opener=wire,target_current=True)
    assert value['done'] and value['source_ids']==['new','saved']
    batch=json.loads(wire.calls[0].data)
    assert batch['cursor']==10000 and {r['source_id'] for r in batch['records']}=={'new','saved'}
    assert run(store,CONFIG,job_key=job,opener=wire,target_current=True)['done'] and len(wire.calls)==1
    assert get(store,'penten_review:rejected')['status']=='rejected'
    assert store.execute('SELECT COUNT(*) FROM properties').fetchone()[0]==4
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0


def test_missing_feedback_leaves_quality_job_durable_for_recovery_instead_of_erasing_it(store,clock,monkeypatch):
    job='listing-contact-enrichment:fixture:property-quality-v2'
    monkeypatch.setattr('rentalsourcing.review_feedback.refresh',lambda *args:{'state':'unavailable'})
    wire=Wire([])
    with pytest.raises(SyncFailure,match='destination_feedback_unavailable'):
        run(store,CONFIG,job_key=job,opener=wire,target_current=True)
    assert get(store,job)['target_current'] and not get(store,job)['done'] and not wire.calls
    monkeypatch.setattr('rentalsourcing.review_feedback.refresh',lambda *args:{'state':'fresh'})
    wire=Wire([{'accepted':True,'matched':0,'next_cursor':None,'summary':{}}])
    assert run(store,CONFIG,job_key=job,opener=wire)['done']
