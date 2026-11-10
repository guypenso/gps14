import json
from dataclasses import replace
import pytest
from conftest import listing
from rentalsourcing.price_sync import enqueue,flush
from rentalsourcing.config import Settings
from rentalsourcing.storage import BudgetExceeded,Store

CONFIG=('https://fixture.test/review-sync','s'*32,'service')
class Response:
    status=200
    def __init__(self,data):self.data=data
    def __enter__(self):return self
    def __exit__(self,*a):pass
    def read(self,n):return json.dumps(self.data).encode()

def test_large_queue_continues_and_partial_receipt_never_loses_work(store,clock):
    now=clock().isoformat()
    for i in range(600):enqueue(store,listing('large-'+str(i)),now,now,observation_id='o'+str(i))
    class Opener:
        partial=True
        def open(self,req,timeout):
            p=json.loads(req.data);ids=[o['observation_id'] for o in p['observations']]
            ack=ids[:3] if self.partial else ids
            self.partial=False
            return Response({'accepted':True,'batch_id':p['batch_id'],'acknowledged_observation_ids':ack})
    o=Opener();first=flush(store,CONFIG,clock(),o)
    assert first['sent']==3 and first['pending']==597
    assert flush(store,CONFIG,clock(),o,max_batches=100)['pending']==0
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0

def test_price_consumer_lock_is_separate_from_discovery_lock(store,clock):
    if not store.pg:pytest.skip('PostgreSQL advisory-lock integration')
    enqueue(store,listing('locked'),clock().isoformat(),clock().isoformat(),observation_id='locked')
    second=Store(store.test_url,initialize=False)
    try:
        store.execute('SELECT pg_advisory_lock(73541212)')
        assert flush(second,CONFIG,clock())['state']=='busy'
    finally:
        store.execute('SELECT pg_advisory_unlock(73541212)');second.close()

def test_recurring_budget_ignores_old_lifetime_approval_but_caps_provider_quota(store,clock):
    settings=Settings(approval_mode='monthly_budget',billing_anchor=clock().isoformat(),billing_baseline=24,
                      monthly_budget_requests=100,provider_included_requests=50,per_month=100,
                      per_day=100,per_run=10,approved_requests=0,approval_id='')
    for _ in range(26):store.reserve_api(settings,clock())
    with pytest.raises(BudgetExceeded):store.reserve_api(settings,clock())
    assert store.budget_health(settings,clock())['used']==50
    clock.advance(days=32)
    store.reserve_api(settings,clock())
    assert store.budget_health(settings,clock())['used']==1
