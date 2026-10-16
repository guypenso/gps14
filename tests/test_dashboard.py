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
