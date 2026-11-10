"""Durable price observations using stored history, existing settings and signed sync.
Never calls RentCast, sends email or changes an investment criterion.
"""
import datetime as dt,hashlib,json,math,os,urllib.request,logging,time,threading
from decimal import Decimal,InvalidOperation
from .command_center import review_configuration,signed_request
from .request import NoRedirect
from .collector import LIVE
from .storage import Store
from .database import validate_postgres_url
from .telemetry import event

PREFIX='price-observation:'
BATCH_SIZE=10  # At most 32 receiver writes, leaving room for D1 read/guard queries.

def money(value):
    if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value) or value<=0:return None
    d=Decimal(str(value))*100
    if d!=d.to_integral_value() or d>=100000000000:return None
    return int(d)

def enqueue(store,p,observed,detected,*,observation_id,previous=None,kind='snapshot',historical=False,eligible=True):
    price=money(p.get('price'))
    if price is None and str(p.get('status','')).lower() not in ('active','sold','closed','under contract','under contract - show','active under contract','contingent','pending'):return False
    try:
        at=dt.datetime.fromisoformat(observed.replace('Z','+00:00'));when=dt.datetime.fromisoformat(detected.replace('Z','+00:00'))
        if not at.tzinfo or not when.tzinfo:return False
    except (ValueError,TypeError,AttributeError):return False
    body=dict(source='RentCast',source_id=p.get('id'),address=p.get('formattedAddress'),street_address=p.get('addressLine1'),unit=p.get('addressLine2'),city=p.get('city'),state=p.get('state'),zip_code=p.get('zipCode'),price_cents=price,previous_cents=money(previous),observed_at=at.isoformat(),detected_at=when.isoformat(),observation_id=observation_id,kind=kind,historical=historical,listing_status=p.get('status'),non_tax_eligible=eligible)
    semantic=('tracked-baseline' if observation_id.startswith('tracked-baseline:') else 'source')
    identity=hashlib.sha256((str(p.get('id'))+'|'+semantic+'|'+at.isoformat()+'|'+str(price)+'|'+str(p.get('status'))).encode()).hexdigest();key=PREFIX+identity
    old=store.execute('SELECT payload FROM settings WHERE name=?',(key,)).fetchone()
    if old:return False
    return bool(store.execute('INSERT INTO settings(name,payload) VALUES (?,?) ON CONFLICT(name) DO NOTHING',(key,json.dumps(dict(observation=body,attempts=0,synced_at=None,error_type=None)))).rowcount)

def seed(store,boundary,now):
    key='price-history-bootstrap:v1';old=store.execute('SELECT payload FROM settings WHERE name=?',(key,)).fetchone()
    if old:return
    from .core import verification
    properties={identity:(json.loads(raw),observed) for identity,raw,observed in store.execute('SELECT id,payload,observed_at FROM properties WHERE source=?',(LIVE,)).fetchall()}
    with store.transaction():
        count=0
        for history_id,identity,price,previous,reduced,at in store.execute('SELECT id,property_id,price,previous_price,reduced,observed_at FROM price_history ORDER BY observed_at,id').fetchall():
            if identity not in properties:continue
            p,observed=properties[identity];row=dict(p,price=price)
            count+=enqueue(store,row,at,at,observation_id='history:'+history_id,previous=previous,kind='price_change',historical=True,eligible=verification(p,boundary)[0]=='Candidate')
        for identity,(p,observed) in properties.items():count+=enqueue(store,p,observed,observed,observation_id='snapshot:'+identity+':'+observed,historical=True,eligible=verification(p,boundary)[0]=='Candidate')
        store.save_setting(key,dict(created_at=now.isoformat(),queued=count))

def queue_counts(store):
    rows=[json.loads(r[0]) for r in store.execute('SELECT payload FROM settings WHERE name LIKE ?',(PREFIX+'%',)).fetchall()]
    pending=[r for r in rows if not r.get('synced_at')]
    return {'pending':len(pending),'held':sum(bool(r.get('held_at')) for r in pending),
            'snapshots':sum(r['observation']['kind']=='snapshot' for r in pending),
            'price_changes':sum(r['observation']['kind']=='price_change' for r in pending)}


def flush(store,config=None,now=None,opener=None,max_batches=30,max_seconds=25):
    config=config or review_configuration()
    if not config:return {'state':'disabled'}
    if store.pg and not store.execute('SELECT pg_try_advisory_lock(73541212)').fetchone()[0]:
        return {'state':'busy',**queue_counts(store)}
    from .sync_runtime import DEADLINE
    deadline=DEADLINE.get();token=DEADLINE.set(min(deadline,time.monotonic()+max_seconds) if deadline else time.monotonic()+max_seconds)
    try:return _flush(store,config,now,opener,max_batches,max_seconds)
    finally:
        DEADLINE.reset(token)
        if store.pg:store.execute('SELECT pg_advisory_unlock(73541212)')


def _flush(store,config,now,opener,max_batches,max_seconds):
    now=now or dt.datetime.now(dt.timezone.utc);opener=opener or urllib.request.build_opener(NoRedirect)
    url,key,token=config;url=url.rsplit('/',1)[0]+'/review-price-observations'
    from .sync_runtime import request_json,get,component
    started=time.monotonic();sent=0;coverage={}
    for _ in range(max_batches):
        if time.monotonic()-started>=max_seconds:break
        frozen=get(store,'price-sync-active-batch')
        if not frozen:
            pending=[]
            for name,raw in store.execute('SELECT name,payload FROM settings WHERE name LIKE ?',(PREFIX+'%',)).fetchall():
                row=json.loads(raw)
                if not row.get('synced_at') and not row.get('held_at'):pending.append((name,row))
            pending.sort(key=lambda r:(r[1]['observation']['observed_at'],r[1]['observation']['source_id'],0 if r[1]['observation']['kind']=='price_change' else 1,r[0]))
            part=pending[:BATCH_SIZE]
            if not part:break
            payload={'observations':[r['observation'] for name,r in part]}
            payload['batch_id']=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            frozen={'names':[n for n,r in part],'payload':payload}
            store.save_setting('price-sync-active-batch',frozen)
        payload=frozen['payload'];body=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode()
        try:
            def validate(result,*args):
                if result.get('accepted') is not True or result.get('batch_id')!=payload['batch_id']:raise ValueError('Invalid price receipt')
                ids=[o['observation_id'] for o in payload['observations']]
                if 'acknowledged_observation_ids' in result:
                    ack=result['acknowledged_observation_ids'];bad=result.get('rejected_observation_ids',[])
                    if not isinstance(ack,list) or not isinstance(bad,list) or set(ack)&set(bad) or not set(ack+bad)<=set(ids):raise ValueError('Invalid observation receipt')
                elif result.get('counts',{}).get('invalid',0):raise ValueError('Legacy receiver rejected observations')
            result=request_json(store,'price_sync',payload['batch_id'],(url,key,token),body,
                timeout=max(.1,min(20,max_seconds-(time.monotonic()-started))),max_bytes=200000,opener=opener,now=now,validate=validate)
            ack=set(result.get('acknowledged_observation_ids',[o['observation_id'] for o in payload['observations']]))
            bad=set(result.get('rejected_observation_ids',[]))
            with store.transaction():
                for name in frozen['names']:
                    row=get(store,name);identity=row['observation']['observation_id'];row['attempts']+=1
                    if identity in ack:row.update(synced_at=now.isoformat(),error_type=None,receiver_disposition='untracked' if identity in result.get('untracked_observation_ids',[]) else 'acknowledged');sent+=1
                    elif identity in bad:
                        row.update(held_at=now.isoformat(),error_type='receiver_invalid_observation')
                        store.save_setting('sync_hold:'+name,{'reason':'receiver_invalid_observation','at':now.isoformat()})
                    store.save_setting(name,row)
                store.execute('DELETE FROM settings WHERE name=?',('price-sync-active-batch',))
            coverage=result.get('coverage',{})
            if len(ack)+len(bad)<len(payload['observations']):break
        except Exception as exc:
            event('price_sync_deferred',error_type=type(exc).__name__,pending=queue_counts(store)['pending'])
            return {'state':'deferred','sent':sent,**queue_counts(store)}
    counts=queue_counts(store);summary={k:v for k,v in coverage.items() if k!='properties'}
    component(store,'price_sync','partial' if counts['pending'] else 'success',now=now,processed=sent,pending=counts['pending'],recovery='scheduled' if counts['pending'] else None)
    event('price_sync_accepted',observations=sent,**counts,coverage=summary)
    return {'state':'accepted','sent':sent,**counts,'coverage':summary}


def start_worker(url):
    """Independent durable price consumer; never lists properties or sends mail."""
    if os.getenv('ENABLE_REVIEW_INVENTORY_SYNC','false').lower()!='true':return
    def loop():
        while True:
            store=None
            try:
                store=Store(url,initialize=False);result=safe_flush(store)
                if result.get('sent') or result.get('pending'):event('price_recovery_worker',**result)
            except Exception as exc:event('price_recovery_worker_failed',error_type=type(exc).__name__)
            finally:
                if store:store.close()
            time.sleep(15)
    threading.Thread(target=loop,name='price-observation-recovery',daemon=True).start()


def safe_current_baselines(store,boundary):
    """After report imports, establish newly tracked candidates without old alerts."""
    try:
        if not review_configuration():return {'state':'disabled'}
        from .core import verification
        delivered_sources={(r['observation']['source_id'],r['observation']['observed_at']) for raw, in store.execute('SELECT payload FROM settings WHERE name LIKE ?',(PREFIX+'%',)).fetchall() for r in [json.loads(raw)] if r.get('synced_at') and r.get('receiver_disposition')=='acknowledged'}
        pending_sources={r['observation']['source_id'] for raw, in store.execute('SELECT payload FROM settings WHERE name LIKE ?',(PREFIX+'%',)).fetchall() for r in [json.loads(raw)] if not r.get('synced_at')}
        for identity,raw,observed in store.execute('SELECT id,payload,observed_at FROM properties WHERE source=?',(LIVE,)).fetchall():
            p=json.loads(raw)
            if identity not in pending_sources and (identity,observed) not in delivered_sources and verification(p,boundary)[0]=='Candidate':
                enqueue(store,p,observed,observed,observation_id='tracked-baseline:'+identity+':'+observed,historical=True)
        return safe_flush(store)
    except Exception as exc:
        event('price_baseline_deferred',error_type=type(exc).__name__)
        return {'state':'deferred'}

def safe_flush(store):
    try:return flush(store)
    except Exception as exc:event('price_sync_deferred',error_type=type(exc).__name__);return {'state':'deferred'}

def main():
    logging.basicConfig(level=logging.INFO,format='%(message)s')
    store=None
    try:
        from .config import Settings
        from .geography import load_boundary
        store=Store(validate_postgres_url(os.getenv('DATABASE_URL','')),initialize=False)
        if not store.lock():raise ValueError('Existing job is busy')
        seed(store,load_boundary(Settings.from_env().boundary_file),dt.datetime.now(dt.timezone.utc))
        for _ in range(10):
            result=safe_flush(store)
            if result.get('state')!='accepted' or not result.get('pending'):break
    finally:
        if store:store.close()
if __name__=='__main__':main()
