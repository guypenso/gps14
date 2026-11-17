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


def test_realscout_audit_batches_official_location_and_owner_decisions(store,boundary,clock,monkeypatch):
    from rentalsourcing.listing_intake import audit
    for n in range(20):
        p=listing('realscout:'+str(n),addressLine1=f'{100+n} Fictional Fixture Ave')
        store.save_setting('listing_identity:'+p['id'],{'source_id':p['id'],'property':p,'observed_at':clock().isoformat(),
            'matched_rentcast':False,'owner_deferred':False,'state':'Pending Verification'})
        if n<10:store.save_setting('penten_review:'+p['id'],{'status':'rejected'})
    original=store.execute;single=[]
    def measured(sql,args=()):
        if sql.startswith('SELECT payload FROM settings WHERE name=?') and args and str(args[0]).startswith(('penten_review:','listing_geocode:')):single.append(sql)
        return original(sql,args)
    monkeypatch.setattr(store,'execute',measured)
    result=audit(store,boundary,clock())
    assert len(result['properties'])==20 and sum(p['owner_deferred'] for p in result['properties'])==10
    assert single==[] and store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0


def test_listing_report_reuses_one_owner_snapshot_without_per_identity_queries(store,boundary,clock,monkeypatch):
    from rentalsourcing.listing_report import listing_report
    for n in range(20):
        p=listing('realscout:'+str(n),addressLine1=f'{100+n} Fictional Fixture Ave')
        store.save_setting('listing_identity:'+p['id'],{'source_id':p['id'],'property':p,'observed_at':clock().isoformat()})
        if n<10:store.save_setting('penten_review:'+p['id'],{'status':'rejected'})
    original=store.execute;single=[]
    def measured(sql,args=()):
        if sql.startswith('SELECT payload FROM settings WHERE name=?') and args and str(args[0]).startswith('penten_review:'):single.append(sql)
        return original(sql,args)
    monkeypatch.setattr(store,'execute',measured)
    result=listing_report(store,boundary,clock())
    assert result['properties']==20 and len(result['cards'])==10 and single==[]
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
