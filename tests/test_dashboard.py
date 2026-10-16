import base64
import datetime as dt
import json
from dataclasses import replace
import pytest
from rentalsourcing.dashboard import create_app
from rentalsourcing.engine import Engine
from rentalsourcing.collector import Client
from conftest import Provider,listing,taxes

PASSWORD='fixture-dashboard-password-123'
HEADERS={'Authorization':'Basic '+base64.b64encode(('owner:'+PASSWORD).encode()).decode()}

@pytest.fixture
def web(store,boundary,clock,monkeypatch):
    monkeypatch.setenv('DASHBOARD_PASSWORD',PASSWORD)
    return create_app(store.test_url,testing=True,clock=clock,boundary=boundary).test_client()


def ingest(store,boundary,clock,settings,rows,records=None):
    provider=Provider(rows)
    if records is not None:provider.records=records
    client=Client(store,settings,clock,provider,lambda _:None)
    Engine(store,boundary,client,settings,clock).run('web-fixtures',clock().date().isoformat())
    return provider


def test_health_public_but_data_private(web):
    assert web.get('/healthz').status_code==200
    assert web.get('/').status_code==401
    assert web.get('/api/dashboard').status_code==401
    assert web.get('/api/email-preview').status_code==401
    assert web.get('/readyz').status_code==401
    assert web.get('/readyz',headers=HEADERS).status_code==200


def test_incorrect_password_does_not_unlock_dashboard(web):
    bad={'Authorization':'Basic '+base64.b64encode(b'owner:bad-password').decode()}
    assert web.get('/api/dashboard',headers=bad).status_code==401


def test_empty_dashboard_has_no_demo_and_coverage_unknown(web):
    r=web.get('/',headers=HEADERS)
    assert r.status_code==200
    assert 'טרם נשמרו נכסים אמיתיים' in r.text
    assert 'FICTIONAL TEST ADDRESS' not in r.text
    d=web.get('/api/dashboard',headers=HEADERS).get_json()
    assert d['total']==0 and d['coverage']['percent'] is None
    assert r.headers['Cache-Control']=='no-store'
    assert "frame-ancestors 'none'" in r.headers['Content-Security-Policy']


def test_dashboard_shows_verified_pending_rejected_and_tax_fields(web,store,boundary,clock,settings):
    provider=ingest(store,boundary,clock,replace(settings,page_size=100),[
      listing('verified'),listing('missing',bedrooms=None),listing('excluded',zipCode='15203'),listing('tax-high')],
      {'verified':taxes('verified',1500),'tax-high':taxes('tax-high',1900)})
    before=len(provider.calls)
    d=web.get('/api/dashboard',headers=HEADERS).get_json()
    assert d['counts']=={'Qualified':1,'Pending Verification':1,'Rejected':2}
    good=next(p for p in d['properties'] if p['id']=='verified')
    assert good['annual_tax']==1500 and good['adjusted_tax']==1600 and good['tax_year']==2025
    assert good['source']=='RentCast Sale Listings'
    assert any(p['reason_text'] for p in d['properties'] if p['state']=='Rejected')
    page=web.get('/?state=Qualified',headers=HEADERS)
    assert page.status_code==200 and '$1,600.00' in page.text
    assert len(provider.calls)==before # Reading UI cannot consume provider requests.
    assert web.post('/api/scan',headers=HEADERS).status_code==404


def test_expired_listing_downgraded_without_changing_database(web,store,boundary,clock,settings):
    ingest(store,boundary,clock,settings,[listing()])
    clock.advance(hours=25)
    d=web.get('/api/dashboard',headers=HEADERS).get_json()
    assert d['counts']['Qualified']==0 and d['properties'][0]['state']=='Pending Verification'
    assert 'stale_listing' in d['properties'][0]['reasons']
    assert store.get_property('fixture-property')['state']=='Qualified'


def test_unknown_geography_never_displays_a_qualified_deal(store,boundary,clock,settings,monkeypatch,tmp_path):
    ingest(store,boundary,clock,settings,[listing()])
    monkeypatch.setenv('DASHBOARD_PASSWORD',PASSWORD)
    monkeypatch.setenv('CITY_BOUNDARY_FILE',str(tmp_path/'missing.geojson'))
    app=create_app(store.test_url,testing=True,clock=clock)
    d=app.test_client().get('/api/dashboard',headers=HEADERS).get_json()
    assert d['counts']['Qualified']==0 and 'city_boundary_unavailable' in d['properties'][0]['reasons']
    assert app.test_client().get('/readyz',headers=HEADERS).status_code==503


def test_untrusted_fixture_source_hidden_from_live_dashboard(web,store,clock):
    p=listing()
    store.save_property(p['id'],p,'Qualified',[],'fixture',1,clock().isoformat())
    assert web.get('/api/dashboard',headers=HEADERS).get_json()['total']==0


def test_missing_required_field_never_displayed_as_approved_even_if_db_says_qualified(web,store,boundary,clock,settings):
    ingest(store,boundary,clock,settings,[listing()])
    p=listing(bedrooms=None)
    store.save_property(p['id'],p,'Qualified',[],'rentcast-live',1,clock().isoformat())
    assert web.get('/api/dashboard',headers=HEADERS).get_json()['counts']['Qualified']==0


def test_html_is_escaped(web,store,boundary,clock,settings):
    ingest(store,boundary,clock,settings,[listing(formattedAddress='<script>alert(1)</script>')])
    r=web.get('/',headers=HEADERS)
    assert '<script>alert(1)</script>' not in r.text
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in r.text


def test_preview_is_read_only_and_never_sends(web,store):
    count=store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]
    preview=web.get('/api/email-preview',headers=HEADERS).get_json()
    assert preview['mode']=='preview_only_no_send' and preview['state']=='not_prepared'
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==count


def test_weak_dashboard_password_prevents_start(monkeypatch,store):
    monkeypatch.setenv('DASHBOARD_PASSWORD','weak')
    with pytest.raises(ValueError,match='20'):create_app(store.test_url,testing=True)


def test_missing_price_after_historical_reduction_stays_pending_not_server_error(web,store,boundary,clock,settings):
    provider=ingest(store,boundary,clock,settings,[listing(history={'2026-10-01':{'price':190000}})])
    prior=store.get_property('fixture-property')
    row=dict(prior['payload'],price=None)
    store.save_property(row['id'],row,'Pending Verification',['price'],'rentcast-live',prior['revision'],clock().isoformat())
    response=web.get('/api/dashboard',headers=HEADERS)
    assert response.status_code==200
    item=response.get_json()['properties'][0]
    assert item['state']=='Pending Verification' and item['price_reduction'] is False


@pytest.mark.parametrize('value',[None,'not-a-date','2026-10-16T12:00:00','2026-10-17T12:00:00+00:00'])
def test_invalid_or_future_listing_timestamps_never_approve(web,store,boundary,clock,settings,value):
    ingest(store,boundary,clock,settings,[listing()])
    store.execute('UPDATE properties SET observed_at=? WHERE id=?',(value or '', 'fixture-property'))
    response=web.get('/api/dashboard',headers=HEADERS)
    assert response.status_code==200
    p=response.get_json()['properties'][0]
    assert p['state']=='Pending Verification' and p['stored_state']=='Qualified'
    assert {'invalid_timestamp','future_timestamp'} & set(p['reasons'])


@pytest.mark.parametrize('field,value',[
    ('expires_at','2026-10-16T11:59:59+00:00'),
    ('expires_at','2026-10-17T12:00:00'),
    ('expires_at','bad'),
    ('fetched_at','2026-10-17T12:00:00+00:00'),
    ('fetched_at','2026-10-16T12:00:00')])
def test_invalid_or_expired_tax_is_masked(web,store,boundary,clock,settings,field,value):
    ingest(store,boundary,clock,settings,[listing()])
    store.execute('UPDATE tax_history SET '+field+'=? WHERE property_id=?',(value,'fixture-property'))
    p=web.get('/api/dashboard',headers=HEADERS).get_json()['properties'][0]
    assert p['state']=='Pending Verification'
    assert p['annual_tax'] is None and p['adjusted_tax'] is None and not p['tax_current']
    assert 'tax_cache_expired' in p['reasons']


def test_launch_operational_status_and_event_states(web,store,boundary,clock,settings):
    ingest(store,boundary,clock,settings,[listing()])
    # The completed scan remains visible even when many later jobs fail.
    for n in range(12):
        store.job('failure-'+str(n),clock().date().isoformat())
        clock.advance(minutes=1)
        store.checkpoint('failure-'+str(n),0,False,{},'failed')
    store.run('failed-attempt',dict(status='failed',error_type='TimeoutError'))
    store.operation('launch-error','A provider timeout left verification pending')
    d=web.get('/api/dashboard',headers=HEADERS).get_json()
    assert d['last_successful_scan']['state']=='complete'
    assert d['new_deals'][0]['kind']=='new'
    assert d['new_deals'][0]['state']=='ready'
    assert d['new_deals'][0]['current_property_state']=='Qualified'
    assert d['failed_runs'][0]['error_type']=='TimeoutError'
    assert any('provider timeout' in message for message in d['operations'])
    assert 'סריקה אחרונה שהושלמה' in web.get('/',headers=HEADERS).text


def test_demo_marked_live_rows_and_unverified_events_are_hidden(web,store,boundary,clock,settings):
    ingest(store,boundary,clock,settings,[listing()])
    row=listing('not-real',demo=True)
    store.save_property(row['id'],row,'Qualified',[],'rentcast-live',1,clock().isoformat())
    store.execute('INSERT INTO deal_events VALUES (?,?,?,?,?,?,?,?,?)',('unverified-event','fixture-property','price_reduction',180000,190000,1,'ready','{}',clock().isoformat()))
    d=web.get('/api/dashboard',headers=HEADERS).get_json()
    assert d['total']==1
    assert not d['price_reductions']
