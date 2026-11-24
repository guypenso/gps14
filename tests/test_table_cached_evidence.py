def test_table_source_evidence_uses_current_listing_and_never_substitutes_a_discovery_date(store,clock):
    from rentalsourcing.dashboard import cached_table_evidence
    from rentalsourcing.collector import LIVE
    import json
    now=clock()
    p={'id':'fixture', 'formattedAddress':'5 Test St, Pittsburgh, PA 15212','addressLine1':'5 Test St',
       'zipCode':'15212','state':'PA','mlsNumber':'second','listedDate':'2026-10-16','history':{'2020-01-01':{'listedDate':'2020-01-01'}}}
    store.save_property('fixture',p,'Pending Verification',[],LIVE,1,now.isoformat())
    result=cached_table_evidence(store,['fixture','other'],now)
    assert result['provider_calls']==0
    assert result['records'][0]['listing_record']['published']=='2026-10-16'
    assert result['records'][0]['listing_record']['id']=='second'
    del p['listedDate']
    store.execute("UPDATE properties SET payload=? WHERE id=?", (json.dumps(p),'fixture'))
    assert cached_table_evidence(store,['fixture'],now)['records'][0]['listing_record']['published'] is None
