"""All provider traffic is mocked. Only an explicitly configured local test DB is used."""
import datetime as dt
import io
import json
import os
import uuid
from dataclasses import replace
from urllib.parse import urlsplit,parse_qs,unquote,quote
import pytest
from rentalsourcing.storage import Store
from rentalsourcing.config import Settings
from rentalsourcing.core import Boundary
from rentalsourcing.collector import Client

@pytest.fixture(autouse=True)
def block_provider_network(monkeypatch):
    def denied(*a,**k):raise AssertionError('Real provider requests are forbidden in tests')
    monkeypatch.setattr('urllib.request.OpenerDirector.open',denied)
    monkeypatch.setenv('RENTCAST_API_KEY','fixture-key-not-real')
    monkeypatch.setenv('RESEND_API_KEY','fixture-resend-not-real')

@pytest.fixture(params=['sqlite','postgres'])
def store(request,tmp_path):
    admin=None;schema=None
    if request.param=='postgres':
        url=os.getenv('TEST_DATABASE_URL')
        if not url:pytest.skip('Set TEST_DATABASE_URL to run PostgreSQL integration tests')
        import psycopg
        admin=psycopg.connect(url,autocommit=True)
        schema='test_'+uuid.uuid4().hex
        admin.execute('CREATE SCHEMA '+schema)
        url+=(' & ' if '?' in url else '?').replace(' & ','&')+'options='+quote('-csearch_path='+schema)
    else:url='sqlite:///'+str(tmp_path/'db.sqlite')
    result=Store(url);result.migrate();result.test_url=url
    yield result
    result.close()
    if admin:
        admin.execute('DROP SCHEMA '+schema+' CASCADE');admin.close()

@pytest.fixture
def boundary(tmp_path):
    p=tmp_path/'boundary.geojson'
    p.write_text(json.dumps(dict(type='Polygon',coordinates=[
      [[-80.1,40.4],[-79.9,40.4],[-79.9,40.6],[-80.1,40.6],[-80.1,40.4]],
      [[-80.05,40.5],[-79.95,40.5],[-79.95,40.55],[-80.05,40.55],[-80.05,40.5]]])))
    return Boundary(p)

class Clock:
    def __init__(self):self.value=dt.datetime(2026,10,16,12,tzinfo=dt.timezone.utc)
    def __call__(self):return self.value
    def advance(self,**kwargs):self.value+=dt.timedelta(**kwargs)

@pytest.fixture
def clock():return Clock()

@pytest.fixture
def settings():
    return Settings(enabled=True,email_enabled=True,email_approval_id='fixture-email-approval',email_to='investor@example.test',email_from='fixture@example.test',
                    approval_id='local-test-only',approved_requests=10000,per_run=100,per_day=1000,per_month=10000,page_size=2)


def listing(identity='fixture-property',**changes):
    return dict(dict(id=identity,formattedAddress='FICTIONAL TEST ADDRESS',city='Pittsburgh',state='PA',zipCode='15212',
      status='Active',bedrooms=3,bathrooms=1,squareFootage=1000,price=180000,latitude=40.45,longitude=-80,
      listedDate='2026-10-01T00:00:00.000Z'),**changes)


def taxes(identity='fixture-property',amount=1500):
    return dict(id=identity,propertyTaxes={'2025':{'year':2025,'total':amount}})

class Response(io.StringIO):
    def __init__(self,data,total=None,status=200):
        super().__init__(json.dumps(data));self.status=status;self.headers={} if total is None else {'X-Total-Count':str(total)}

class Provider:
    def __init__(self,rows=None):
        self.rows=rows if rows is not None else [listing()]
        self.records={r['id']:taxes(r['id']) for r in self.rows if r.get('id')}
        self.calls=[];self.failures=[];self.messages=[]
    def open(self,request,timeout=None):
        self.calls.append(request)
        if self.failures:raise self.failures.pop(0)
        parts=urlsplit(request.full_url)
        if parts.hostname=='api.resend.com':
            self.messages.append(json.loads(request.data));return Response({'id':'fixture-message-id'})
        assert parts.hostname=='api.rentcast.io'
        if parts.path.endswith('/listings/sale'):
            q=parse_qs(parts.query);offset=int(q['offset'][0]);limit=int(q['limit'][0])
            return Response(self.rows[offset:offset+limit],len(self.rows))
        identity=unquote(parts.path.split('/properties/')[1]);return Response(self.records.get(identity,{'id':identity}))
    @property
    def tax_calls(self):return sum('/properties/' in r.full_url for r in self.calls)

@pytest.fixture
def provider():return Provider()

@pytest.fixture
def client(store,settings,clock,provider):
    return Client(store,settings,clock,provider,lambda _:None)
