import datetime as dt,json,hashlib,hmac
from conftest import listing
from rentalsourcing.collector import LIVE
from rentalsourcing.review_feedback import refresh
from rentalsourcing.tax_backlog import review_deferred
from rentalsourcing.price_sync import enqueue

def test_conditional_watch_is_durable_deferral_not_rejection_and_price_observations_continue(store,clock):
    config=('https://fixture.test/api/rentalsourcing/review-sync','s'*32,'fixture')
    class Response:
        status=200
        def __init__(self,req,watch):
            nonce=json.loads(req.data)['nonce'];self.body=json.dumps({'nonce':nonce,'decisions':[{'source_id':'source','status':'pursue','reviewed_at':clock().isoformat(),'conditional_watch':watch}],'next_cursor':None}).encode();self.headers={'X-RentalSourcing-Response-Signature':'v1='+hmac.new(config[1].encode(),self.body,hashlib.sha256).hexdigest()}
        def read(self,n):return self.body
        def __enter__(self):return self
        def __exit__(self,*a):pass
    class Wire:
        watch={'active':True,'state':'active','target_cents':15000000,'revision':1}
        def open(self,req,timeout):return Response(req,self.watch)
    wire=Wire();refresh(store,clock(),wire,config)
    assert review_deferred('source',store)
    row=json.loads(store.execute('SELECT payload FROM settings WHERE name=?',('penten_review:source',)).fetchone()[0]);assert row['status']=='pursue'
    assert enqueue(store,listing('source',price=140000),clock().isoformat(),clock().isoformat(),observation_id='drop')
    clock.advance(seconds=1);wire.watch={'active':False,'state':'triggered','target_cents':15000000,'revision':2};refresh(store,clock(),wire,config)
    assert not review_deferred('source',store)
    assert store.execute('SELECT COUNT(*) FROM usage').fetchone()[0]==0
    assert store.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]==0
    assert store.execute('SELECT COUNT(*) FROM settings WHERE name LIKE ?',('penten_review_history:%',)).fetchone()[0]==1
