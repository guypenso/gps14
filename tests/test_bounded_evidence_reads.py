import datetime as dt
from conftest import listing
from rentalsourcing.collector import LIVE
from rentalsourcing.tax_backlog import priorities
from rentalsourcing.diagnostics import funnel


def test_review_and_tax_reads_do_not_issue_one_roundtrip_per_source_property(store,boundary,clock,monkeypatch):
    for n in range(20):
        p=listing('source-'+str(n));store.save_property(p['id'],p,'Pending Verification',[],LIVE,1,clock().isoformat())
        if n<15:store.save_setting('penten_review:'+p['id'],{'status':'rejected'})
    original=store.execute;single=[]
    def measured(sql,args=()):
        if sql.startswith('SELECT payload FROM settings WHERE name=?') and args and str(args[0]).startswith('penten_review:'):single.append('review')
        if sql.startswith('SELECT payload,source,fetched_at FROM tax_history WHERE property_id=?'):single.append('tax')
        return original(sql,args)
    monkeypatch.setattr(store,'execute',measured)
    assert len(priorities(store,boundary,clock()))==5
    report=funnel(store,boundary,clock())
    assert report['counts']['owner_review_deferred']==15
    assert report['counts']['non_tax_pass']==report['counts']['tax_uncached_backlog']==20
    assert single==[]
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
