"""ZIP-only change. Fictional data and injected HTTP; no real emails or API calls."""
import datetime as dt
import json
from unittest.mock import patch
import pytest
from conftest import Provider,listing,taxes
from rentalsourcing.core import EXCLUDED_ZIPS,basic_rejection,verification
from rentalsourcing.collector import Client,LIVE
from rentalsourcing.engine import Engine
from rentalsourcing.criteria import reevaluate
from rentalsourcing.digest import Digest,ReconciliationRequired,render


@pytest.mark.parametrize('zip',['15203','15204','15210','15211','15211-1234'])
def test_complete_exclusion_list_and_zip_plus_four(boundary,zip):
    assert EXCLUDED_ZIPS=={'15203','15204','15210','15211'}
    row=listing(zipCode=zip)
    assert basic_rejection(row)=='excluded_zip' and verification(row,boundary)==('Rejected',['excluded_zip'])


@pytest.mark.parametrize('zip',['15207','15212','15216'])
def test_other_zips_keep_existing_behavior(boundary,zip):
    assert basic_rejection(listing(zipCode=zip)) is None
    assert verification(listing(zipCode=zip),boundary)==('Candidate',[])


def test_discovery_rejects_before_tax_request_and_preserves_property(store,boundary,settings,clock):
    p=Provider([listing('excluded',zipCode='15211')]);client=Client(store,settings,clock,p,lambda _:None)
    Engine(store,boundary,client,settings,clock).run('zip-test',clock().date().isoformat())
    assert store.get_property('excluded')['state']=='Rejected'
    assert p.tax_calls==0 and store.execute('SELECT COUNT(*) FROM price_history').fetchone()[0]==1


def test_saved_qualified_and_pending_are_rejected_without_losing_history(store,boundary,clock):
    for identity,state in [('old-qualified','Qualified'),('old-pending','Pending Verification')]:
        row=listing(identity,zipCode='15211')
        store.save_property(identity,row,state,[],LIVE,1,clock().isoformat())
        store.save_tax(identity,taxes(identity),LIVE,clock().isoformat(),(clock()+dt.timedelta(days=7)).isoformat())
    before=store.execute('SELECT COUNT(*) FROM tax_history').fetchone()[0]
    reevaluate(store,boundary,clock())
    assert store.execute('SELECT COUNT(*) FROM properties').fetchone()[0]==2
    assert store.execute('SELECT COUNT(*) FROM tax_history').fetchone()[0]==before
    assert all(store.get_property(i)['state']=='Rejected' for i in ['old-qualified','old-pending'])
    assert json.loads(store.execute("SELECT payload FROM settings WHERE name='criteria'").fetchone()[0])['excluded_zip']==sorted(EXCLUDED_ZIPS)


def test_new_reduction_and_pending_cards_all_excluded_at_render(clock):
    def item(zip,kind='new'):
        return dict(listing=listing(zip,zipCode=zip),tax={'year':2025,'annual':1500,'adjusted':1600},kind=kind,verified_at=clock().isoformat(),tax_fetched_at=clock().isoformat())
    text,markup=render('2026-10-27',[item('15211'),item('15211','price_reduction'),item('15212')],
        {'pending_properties':[dict(listing=listing('excluded-pending',zipCode='15211'),reasons=['tax_not_verified'],observed_at=clock().isoformat())]}, {'remaining':1},[])
    assert '15211' not in text+markup and 'excluded-pending' not in text+markup and '15212' in text+markup


@pytest.mark.parametrize('attempted',[False,True])
@pytest.mark.parametrize('section',['new','price_reduction','pending'])
def test_frozen_prechange_email_cannot_send_excluded_card(store,boundary,settings,clock,attempted,section):
    p=Provider();digest=Digest(store,boundary,settings,clock,p)
    # Simulate an earlier release preparing an otherwise valid 15211 report.
    row=listing('before-change',zipCode='15211')
    store.save_property(row['id'],row,'Qualified',[],LIVE,1,clock().isoformat())
    store.save_tax(row['id'],taxes(row['id']),LIVE,clock().isoformat(),(clock()+dt.timedelta(days=7)).isoformat())
    if section=='price_reduction':
        store.execute('INSERT INTO price_history VALUES (?,?,?,?,?,?)',('old-reduction',row['id'],row['price'],190000,1,clock().isoformat()))
    with patch('rentalsourcing.core.EXCLUDED_ZIPS',frozenset({'15203','15204','15210'})),patch('rentalsourcing.digest.EXCLUDED_ZIPS',frozenset({'15203','15204','15210'})):
        oid=digest.prepare_stored('2026-10-27')
    payload=store.execute('SELECT payload FROM outbox WHERE id=?',(oid,)).fetchone()[0]
    # Pending cards are not represented by deal_events: guard must inspect frozen body too.
    if section=='pending':
        body=json.loads(payload);body['text']='Pending Verification | ZIP 15211';body['html']='<p>Pending Verification | ZIP 15211</p>'
        payload=json.dumps(body);store.execute("UPDATE outbox SET event_ids='[]',payload=? WHERE id=?",(payload,oid))
    if attempted:store.execute("UPDATE outbox SET state='uncertain',first_attempt_at=? WHERE id=?",(clock().isoformat(),oid))
    if attempted:
        with pytest.raises(ReconciliationRequired):digest.send(oid)
    else:assert digest.send(oid)=='cancelled_stale'
    assert not p.calls and store.execute('SELECT payload FROM outbox WHERE id=?',(oid,)).fetchone()[0]==payload
    assert store.execute('SELECT COUNT(*) FROM properties').fetchone()[0]==1


def test_already_sent_historical_body_is_not_rewritten_or_resent(store,boundary,settings,clock):
    p=Provider();digest=Digest(store,boundary,settings,clock,p);oid=digest.prepare_test()
    body=json.loads(store.execute('SELECT payload FROM outbox WHERE id=?',(oid,)).fetchone()[0]);body['text']='Historic property ZIP 15211'
    payload=json.dumps(body);store.execute("UPDATE outbox SET payload=?,state='sent' WHERE id=?",(payload,oid))
    assert digest.send(oid)=='already_sent' and not p.calls
    assert store.execute('SELECT payload FROM outbox WHERE id=?',(oid,)).fetchone()[0]==payload



def test_allowed_zip_is_not_blocked_by_price_matching_excluded_zip(store,boundary,settings,clock):
    row=listing('allowed',zipCode='15212',price=15211)
    store.save_property(row['id'],row,'Qualified',[],LIVE,1,clock().isoformat())
    store.save_tax(row['id'],taxes(row['id']),LIVE,clock().isoformat(),(clock()+dt.timedelta(days=7)).isoformat())
    p=Provider();digest=Digest(store,boundary,settings,clock,p);oid=digest.prepare_stored('2026-10-27')
    assert digest.send(oid)=='sent' and len(p.messages)==1
