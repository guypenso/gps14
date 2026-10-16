import json
from dataclasses import replace
from urllib.parse import urlsplit,parse_qs
from rentalsourcing.poc import plan,PocEngine,report,safe_listing_link,main
from rentalsourcing.collector import Client
from conftest import Provider,listing


def test_plan_has_exact_filters_and_valid_encoding_without_network():
    p=plan();q=parse_qs(urlsplit(p['url']).query)
    assert q=={k:[str(v)] for k,v in p['parameters'].items()}
    assert q['limit']==['100'] and q['bathrooms']==['1:*'] and q['price']==['*:200000']
    assert p['max_total_attempts']==4 and p['max_tax_candidates']==3 and not p['emails']


def test_default_command_only_prints_plan(capsys):
    main([])
    assert json.loads(capsys.readouterr().out)['mode']=='plan_only_no_requests'


def test_poc_never_fetches_second_page_or_more_than_three_tax_candidates(store,boundary,settings,clock):
    provider=Provider([listing(str(i)) for i in range(101)])
    s=replace(settings,page_size=100,max_pages=1,approved_requests=4,per_run=4)
    client=Client(store,s,clock,provider,lambda _:None)
    stats=PocEngine(store,boundary,client,s,clock).run('poc',clock().date().isoformat(),candidate_limit=3,first_page_only=True)
    result=report(store,boundary,'poc',stats,clock)
    assert result['received']==100 and result['total_matching_api_query']==101
    assert result['initial_pass_count']==100 and result['qualified_in_sample']==3
    assert provider.tax_calls==3 and client.count==4
    assert sum('/listings/sale' in r.full_url for r in provider.calls)==1
    # Resuming the same POC must not expand to three further properties.
    client2=Client(store,s,clock,provider,lambda _:None)
    PocEngine(store,boundary,client2,s,clock).run('poc',clock().date().isoformat(),candidate_limit=3,first_page_only=True)
    assert provider.tax_calls==3 and client2.count==0


def test_rejected_and_missing_rows_are_counted_without_tax_calls(store,boundary,settings,clock):
    provider=Provider([listing('excluded',zipCode='15210'),listing('missing',bedrooms=None),listing('ok')])
    s=replace(settings,page_size=100)
    client=Client(store,s,clock,provider,lambda _:None)
    stats=PocEngine(store,boundary,client,s,clock).run('poc',clock().date().isoformat(),candidate_limit=3,first_page_only=True)
    r=report(store,boundary,'poc',stats,clock)
    assert r['received']==3 and r['rejected_initially']==1 and r['pending_missing_initial_fields']==1
    assert r['initial_pass_count']==1 and provider.tax_calls==1
    assert r['properties'][0]['tax_year']==2025
    assert r['properties'][0]['adjusted_annual_tax']==1600
    assert r['properties'][0]['listing_url'] is None and r['coverage']['value'] is None


def test_links_are_not_invented_or_accepted_as_javascript():
    assert safe_listing_link({}) is None
    assert safe_listing_link({'listingUrl':'javascript:alert(1)'}) is None
    assert safe_listing_link({'listingUrl':'https://example.test/listing/1'})=='https://example.test/listing/1'
