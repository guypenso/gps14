import datetime as dt,json
from conftest import listing
from rentalsourcing.price_sync import enqueue,flush,seed,money
from rentalsourcing.collector import LIVE

def test_exact_money_and_invalid_values():
    assert money(109000)==10900000
    for v in [None,True,0,-1,'100',float('nan'),1.001]:assert money(v) is None

def test_durable_retry_preserves_body_ids_and_no_paid_usage(store,clock):
    p=listing('price-fixture');now=clock().isoformat()
    assert enqueue(store,p,now,now,observation_id='source:1',previous=130000,kind='price_change')
    assert not enqueue(store,p,now,now,observation_id='source:1',previous=130000,kind='price_change')
    class Response:
        status=200
        def __init__(self,req):self.payload=json.loads(req.data)
        def read(self,n):return json.dumps({'accepted':True,'batch_id':self.payload['batch_id'],'coverage':{}}).encode()
        def __enter__(self):return self
        def __exit__(self,*a):pass
    class Opener:
        calls=[];fail=True
        def open(self,req,timeout):
            self.calls.append(req.data);assert req.full_url.endswith('/review-price-observations')
            if self.fail:raise TimeoutError()
            return Response(req)
    opener=Opener();config=('https://fixture.test/api/rentalsourcing/review-sync','s'*32,'service')
    assert flush(store,config,clock(),opener)['state']=='deferred';opener.fail=False
    assert flush(store,config,clock(),opener)['sent']==1
    assert opener.calls[0]==opener.calls[1]
    assert flush(store,config,clock(),opener)['sent']==0
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0

def test_bootstrap_existing_history_is_historical_and_idempotent(store,boundary,clock):
    p=listing('price-fixture');store.save_property(p['id'],p,'Pending Verification',[],LIVE,1,clock().isoformat())
    store.execute('INSERT INTO price_history VALUES (?,?,?,?,?,?)',('source:1',p['id'],p['price'],130000,1,clock().isoformat()))
    seed(store,boundary,clock());seed(store,boundary,clock())
    rows=store.execute('SELECT payload FROM settings WHERE name LIKE ?',('price-observation:%',)).fetchall()
    assert len(rows)==2 and all(json.loads(r[0])['observation']['historical'] for r in rows)

def test_batches_fit_receiver_query_budget_without_losing_events(store,clock):
    now=clock().isoformat()
    for i in range(23):assert enqueue(store,listing(f'price-{i}'),now,now,observation_id=f'history:{i}')
    sizes=[]
    class Response:
        status=200
        def __init__(self,req):self.payload=json.loads(req.data)
        def read(self,n):return json.dumps({'accepted':True,'batch_id':self.payload['batch_id']}).encode()
        def __enter__(self):return self
        def __exit__(self,*a):pass
    class Opener:
        def open(self,req,timeout):
            sizes.append(len(json.loads(req.data)['observations']))
            return Response(req)
    result=flush(store,('https://fixture.test/api/rentalsourcing/review-sync','s'*32,'service'),clock(),Opener())
    assert sizes==[10,10,3]
    assert result['sent']==23 and result['pending']==0
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0
