import datetime as dt,json
from dataclasses import replace
from conftest import listing,Provider
from rentalsourcing.collector import Client,LIVE
from rentalsourcing.engine import Engine
from rentalsourcing.digest import render
from rentalsourcing.cloud import daily_discovery
from rentalsourcing.diagnostics import funnel


def test_missing_tax_does_not_starve_other_candidates_across_days(store,boundary,settings,clock):
    provider=Provider([listing('a-missing'),listing('b-valid'),listing('c-valid')])
    provider.records['a-missing']['propertyTaxes']={}
    s=replace(settings,per_run=2,per_day=4,max_pages=1,page_size=10)
    Engine(store,boundary,Client(store,s,clock,provider,lambda _:None),s,clock).run('daily:one','2026-10-16')
    assert store.get_property('a-missing')['state']=='Pending Verification'
    clock.advance(days=1)
    Engine(store,boundary,Client(store,s,clock,provider,lambda _:None),s,clock).run('daily:two','2026-10-17')
    assert store.get_property('b-valid')['state']=='Qualified'
    assert store.get_property('c-valid')['state']=='Pending Verification'
    assert store.count_usage('approval:'+s.approval_id)==4


def test_partial_verification_is_not_reported_as_discovery_outage():
    stats={'discovery_state':'fresh','partial':True,'received_this_run':441,'discovery_complete':True,'funnel':{'counts':{'non_tax_pass':171,'tax_uncached_backlog':170}},'requests_consumed':2}
    text,html=render('2026-11-01',[],stats,{'remaining':15},[])
    assert 'הגילוי הצליח' in text and 'הגילוי הצליח' in html
    assert 'הסריקה נכשלה' not in text and 'הסריקה נכשלה' not in html
    assert '441' in text and '170' in text and 'requests this run: 2' in text


def test_one_page_daily_scan_resumes_cursor_without_more_daily_calls(store,boundary,settings,clock):
    provider=Provider([listing('a'),listing('b'),listing('c')]);s=replace(settings,page_size=2,per_run=2,max_pages=1)
    daily_discovery(store,boundary,s,clock().date().isoformat(),clock=clock,opener=provider)
    clock.advance(days=1)
    daily_discovery(store,boundary,s,clock().date().isoformat(),clock=clock,opener=provider)
    from urllib.parse import parse_qs,urlsplit
    offsets=[parse_qs(urlsplit(r.full_url).query)['offset'][0] for r in provider.calls if '/listings/sale?' in r.full_url]
    assert offsets==['0','2']
    assert store.get_property('c') is not None


def test_funnel_counts_not_inventory_assumptions(store,boundary,clock):
    for row in [listing('valid'),listing('wrong-zip',zipCode='15211'),listing('missing',longitude=None)]:
        store.save_property(row['id'],row,'Pending Verification',[],LIVE,1,clock().isoformat())
    result=funnel(store,boundary,clock())
    assert result['counts']=={'stored_unique':3,'non_tax_pass':1,'tax_uncached_backlog':1,'non_tax_rejected':1,'non_tax_pending':1}
    assert result['rejection_reasons']['excluded_zip']==1


def test_target_auth_probe_distinguishes_receiver_and_gateway_without_writes(clock):
    import io,urllib.error,pytest
    from rentalsourcing.targeted_sync import verify_auth
    config=('https://example.test/api/rentalsourcing/sync','x'*32,'fixture-service')
    class Probe:
        def __init__(self,code,body):self.code=code;self.body=body
        def open(self,req,timeout):
            assert req.data==b'{}' and timeout==20
            assert req.get_header('Oai-sites-authorization')=='Bearer fixture-service'
            raise urllib.error.HTTPError(req.full_url,self.code,'fixture',{},io.BytesIO(self.body))
    verify_auth(config,clock(),Probe(400,b'{"error":"Unsupported report"}'))
    with pytest.raises(ValueError,match='Receiver rejected HMAC'):
        verify_auth(config,clock(),Probe(401,b'{"error":"Invalid synchronization authentication"}'))
    with pytest.raises(ValueError,match='gateway_or_non_json'):
        verify_auth(config,clock(),Probe(401,b'fixture gateway rejection'))
