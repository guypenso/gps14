"""All-area scheduled discovery with isolated listings, storage and signed-source stand-ins."""
import datetime as dt

import pytest
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

from conftest import Response, listing
from rentalsourcing.cloud import daily_discovery
from rentalsourcing.municipal_coverage import AREAS, UNIFIED, geometries, miles, pages_for_total, page_evidence, unified_query
from rentalsourcing.partition_discovery import PREFIX, _state, get, run, scopes, snapshot
from test_monthly_allocation import grant


def at(clock, day, hour):
    clock.value = dt.datetime(2026, 11, day, hour, tzinfo=dt.timezone.utc)


def from_area(boundary, area, identity, **changes):
    point = geometries(boundary)[area].representative_point()
    return listing(identity, longitude=point.x, latitude=point.y, zipCode='15222', **changes)


class CircleProvider:
    def __init__(self, boundary, rows):
        self.planned = unified_query(boundary)[0]
        self.rows = rows
        self.offsets = []
        self.total_override = None

    def open(self, request, timeout=None):
        url = urlsplit(request.full_url)
        assert url.hostname == 'api.rentcast.io' and url.path == '/v1/listings/sale'
        query = {k: values[0] for k, values in parse_qs(url.query).items()}
        assert {k: v for k, v in query.items() if k not in {'limit', 'offset'}} == {
            k: str(v) for k, v in self.planned.items() if k not in {'limit', 'offset'}
        }
        offset, limit = int(query['offset']), int(query['limit'])
        self.offsets.append(offset)
        return Response(self.rows[offset:offset + limit], self.total_override if self.total_override is not None else len(self.rows))


def test_combined_circle_covers_every_official_polygon(official_boundary):
    query, extents = unified_query(official_boundary)
    assert query['radius'] == 8.4
    assert query['limit'] == 500 and query['includeTotalCount'] == 'true'
    assert {'city', 'zipCode'}.isdisjoint(query)
    for slug, polygon in geometries(official_boundary).items():
        assert extents[slug] < query['radius']
        assert all(miles(query['longitude'], query['latitude'], x, y) < query['radius']
                   for x, y in polygon.convex_hull.exterior.coords)
    legacy = scopes(official_boundary)['pittsburgh-bellevue']
    park = geometries(official_boundary)['bethel-park']
    assert any(miles(legacy['longitude'], legacy['latitude'], x, y) > legacy['radius']
               for x, y in park.convex_hull.exterior.coords)
    assert pages_for_total(None, 500) is None
    assert [pages_for_total(n, 500) for n in (0, 500, 501, 975, 976)] == [1, 1, 2, 2, 3]


def test_one_request_checks_all_areas_and_keeps_ineligible_rows_out(store,official_boundary,settings,clock):
    b = official_boundary
    rows = [from_area(b, area, area) for area in AREAS]
    rows.extend([
        listing('neighbor',latitude=40.3576,longitude=-80.0510,zipCode='15228'),
        listing('unlocated',latitude=None,longitude=None),
        from_area(b,'bethel-park','townhouse',propertyType='Townhouse'),
    ])
    at(clock,15,6)
    cfg = replace(grant(settings),page_size=500)
    provider = CircleProvider(b,rows)
    store.save_setting(PREFIX+'pleasant-hills',{'partition':'pleasant-hills','offset':9,'query_complete':False})
    stats,_ = run(store,b,cfg,'2026-11-15','morning',clock=clock,opener=provider)
    assert provider.offsets == [0] and stats['requests_consumed'] == 1
    assert stats['target_areas'] == list(AREAS) and stats['discovery_complete']
    assert set(stats['partitions']) == {UNIFIED}
    assert stats['pages'][0]['area_observations'] == page_evidence(b,rows)
    assert {slug: stats['pages'][0]['area_observations'][slug] for slug in AREAS} == {
        'pittsburgh':1,'bellevue':1,'bethel-park':2,'pleasant-hills':1}
    assert store.get_property('neighbor')['state'] == 'Rejected'
    assert store.get_property('townhouse')['state'] == 'Rejected'
    assert get(store,PREFIX+'pleasant-hills')['offset'] == 9
    live = snapshot(store,clock(),b)
    assert live['unified_query_evidence']['provider_total'] == 7
    assert live['unified_query_evidence']['stored_inventory']['bellevue']['rentcast']['records'] == 1
    assert live['geographic_completeness'] == 'not_verified'
    at(clock,15,18)
    provider.rows.append(from_area(b,'pleasant-hills','later-offer'))
    again,_ = run(store,b,cfg,'2026-11-15','evening',clock=clock,opener=provider)
    assert again['new_source_ids'] == ['later-offer'] and provider.offsets == [0,0]


def test_shared_cursor_resumes_after_interruption_with_overlap_and_deduplication(store,official_boundary,settings,clock,monkeypatch):
    from rentalsourcing.storage import Store
    b = official_boundary
    rows = [from_area(b,'pittsburgh',f'listing-{i}') for i in range(20)]
    rows.extend([from_area(b,a,a) for a in list(AREAS)[1:]])
    cfg = replace(grant(settings),page_size=20,per_run=1)
    at(clock,15,6)
    provider = CircleProvider(b,rows)
    original = store.save_setting
    failed = False

    def interrupt(name, payload):
        nonlocal failed
        if not failed and name == PREFIX+UNIFIED and payload.get('offset') == 20:
            failed = True
            raise RuntimeError('fixture interrupted after ingestion checkpoint')
        return original(name,payload)

    monkeypatch.setattr(store,'save_setting',interrupt)
    with pytest.raises(RuntimeError,match='interrupted'):
        run(store,b,cfg,'2026-11-15','morning',clock=clock,opener=provider)
    reopened = Store(store.test_url,initialize=False)
    try:
        resumed,_ = run(reopened,b,cfg,'2026-11-15','morning',clock=clock,opener=provider)
        assert provider.offsets == [0]
        assert resumed['requests_consumed'] == 1 and resumed['pages'][0]['checkpoint_recovered']
        assert resumed['discovery_state'] == 'fresh'
        assert resumed['pages'][0]['area_observations']['pittsburgh'] == 20
        assert not resumed['discovery_complete']
        at(clock,15,18)
        rows[19] = dict(rows[19],price=170000)  # Same source ID on the overlapped page.
        end,_ = run(reopened,b,cfg,'2026-11-15','evening',clock=clock,opener=provider)
        assert provider.offsets == [0,19] and end['discovery_complete']
        assert end['pages'][0]['offset_requested'] == 19
        assert end['pages'][0]['duplicates'] == end['pages'][0]['changed'] == 1
        assert not end['pages'][0]['continuity_warning']
        assert end['pages'][0]['area_observations']['pleasant-hills'] == 1
        assert len(end['new_source_ids']) == 3
        assert reopened.execute('SELECT COUNT(*) FROM properties').fetchone()[0] == 23
        assert reopened.get_property('listing-19')['revision'] == 2
    finally:
        reopened.close()


def test_budget_paced_window_still_checks_stored_and_independent_sources_daily(store,official_boundary,settings,clock,monkeypatch):
    b = official_boundary
    cfg = replace(grant(settings),email_enabled=False,page_size=500,per_run=1)
    calls = []

    def source_check(db, operation, now, opener):
        calls.append(operation)
        return {'state':'partial','email':{'results':[{'state':'blocked','error_type':'gmail_reconnect_required'}]},
                'public_inventory':{'state':'fetched','checked_at':now.isoformat()}}

    monkeypatch.setattr('rentalsourcing.listing_report.sync_before_report',source_check)
    for name in ('ENABLE_RENT_ESTIMATES','ENABLE_REVIEW_INVENTORY_SYNC','ENABLE_COMMAND_CENTER_REVIEW_FEEDBACK'):
        monkeypatch.delenv(name,raising=False)
    at(clock,16,6)
    provider = CircleProvider(b,[from_area(b,area,area) for area in AREAS])
    # Use three calls already accounted for in the cycle. Both selected
    # windows are paced with the original 36/8/4/2 budget.
    from rentalsourcing.billing import cycle_key
    from rentalsourcing.monthly_allocation import period
    for key in (cycle_key(cfg,clock()),period(cfg,clock(),'listing')):
        store.execute('INSERT INTO usage VALUES (?,?)',(key,3))
    from rentalsourcing.collector import LIVE
    state = _state(store,UNIFIED,unified_query(b)[0])
    state['last_success_at'] = (clock()-dt.timedelta(hours=23)).isoformat()
    store.save_setting(PREFIX+UNIFIED,state)
    for row in provider.rows:
        store.save_property(row['id'],row,'Pending Verification',[],LIVE,1,(clock()-dt.timedelta(hours=23)).isoformat())
    for hour,slot in [(6,'morning'),(18,'evening')]:
        at(clock,16,hour)
        daily_discovery(store,b,cfg,'2026-11-16',slot=slot,clock=clock,opener=provider)
        receipt = get(store,'multi-discovery:2026-11-16:'+slot)
        assert receipt['requests_consumed'] == 0
        assert receipt['budget_reason'] == 'monthly_listing_pacing'
        assert set(receipt['municipalities']) == set(AREAS)
        for slug, area in receipt['municipalities'].items():
            assert area['rentcast']['state'] == 'monthly_pacing'
            assert area['rentcast']['records'] == 1 and area['rentcast']['fresh_rows_returned'] == 0
            assert area['realscout']['blocked'] == ['gmail_reconnect_required']
            assert area['public_listings']['state'] == ('fetched' if slug=='pittsburgh' else 'no_verified_public_feed_for_area')
    assert calls == ['2026-11-16','2026-11-16:evening'] and provider.offsets == []


def test_inconsistent_provider_page_is_recorded_as_failed_and_keeps_cursor(store,official_boundary,settings,clock):
    b = official_boundary
    at(clock,15,6)
    cfg = replace(grant(settings),page_size=20,per_run=1)
    provider = CircleProvider(b,[])
    provider.total_override = 600
    result,_ = run(store,b,cfg,'2026-11-15','morning',clock=clock,opener=provider)
    assert result['requests_consumed'] == 1
    assert not result['fresh_request_made'] and not result['discovery_complete']
    assert result['partitions'][UNIFIED]['state'] == 'retry_scheduled'
    assert get(store,PREFIX+UNIFIED)['offset'] == 0


def test_old_unfinished_traversal_expires_without_erasing_its_checkpoint(store,official_boundary,settings,clock):
    b = official_boundary
    at(clock,15,6)
    cfg = replace(grant(settings),page_size=20,per_run=1)
    provider = CircleProvider(b,[from_area(b,'pittsburgh',str(i)) for i in range(25)])
    run(store,b,cfg,'2026-11-15','morning',clock=clock,opener=provider)
    job = get(store,PREFIX+UNIFIED)['job_id']
    at(clock,23,18)
    run(store,b,cfg,'2026-11-23','evening',clock=clock,opener=provider)
    state = get(store,PREFIX+UNIFIED)
    assert provider.offsets == [0,0] and state['job_id'] != job
    assert state['previous_generation']['end_reason'] == 'stale_traversal'
    assert store.execute('SELECT offset_value FROM scan_jobs WHERE id=?',(job,)).fetchone()[0] == 20


def test_shared_discovery_delivers_new_candidates_once_with_original_review_rules(store,official_boundary,settings,clock):
    import json
    from rentalsourcing.future_discovery import process

    b = official_boundary
    at(clock,15,6)
    rows = [
        from_area(b,'bethel-park','watch-only',formattedAddress='50 Fixture St, Bethel Park, PA 15102'),
        from_area(b,'pleasant-hills','reviewable',formattedAddress='53 Fixture St, Pleasant Hills, PA 15236'),
        from_area(b,'pittsburgh','excluded',formattedAddress='54 Fixture St, Pittsburgh, PA 15203',propertyType='Townhouse'),
    ]
    rows[0]['zipCode'], rows[1]['zipCode'], rows[2]['zipCode'] = '15102','15236','15203'
    store.save_setting('penten_review:watch-only',{'status':'rejected'})
    cfg = replace(grant(settings),page_size=500)
    provider = CircleProvider(b,rows)
    receipt,_ = run(store,b,cfg,'2026-11-15','morning',clock=clock,opener=provider)
    sends = []

    class Receiver:
        def open(self,request,timeout=None):
            assert request.full_url == 'https://penten.example.test/api/rentalsourcing/sync'
            payload = json.loads(request.data)
            assert [p['source_id'] for p in payload['opportunities']] == ['reviewable']
            assert payload['opportunities'][0]['qualification'] == 'pending_verification'
            sends.append(payload['report_id'])
            return Response({'accepted':True,'report_id':payload['report_id'],'counts':{'inserted':1}})

    ids = receipt['job_ids']
    assert len(ids) == 1
    args = (store,b,ids[0],clock())
    opts = dict(config=('https://penten.example.test/api/rentalsourcing/sync','s'*32,'test'),
                opener=Receiver(),new_since=receipt['started_at'])
    delivered = process(*args,**opts)
    assert delivered['state'] == 'accepted' and delivered['receipt_counts'] == {'inserted':1}
    assert process(*args,**opts)['new_source_listings'] == 0 and len(sends) == 1
    assert get(store,'penten_review:watch-only')['status'] == 'rejected'
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0
