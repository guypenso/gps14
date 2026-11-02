"""Durable price observations using stored history, existing settings and signed sync.
Never calls RentCast, sends email or changes an investment criterion.
"""
import datetime as dt,hashlib,json,math,os,urllib.request,logging
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
    if price is None:return False
    try:
        at=dt.datetime.fromisoformat(observed.replace('Z','+00:00'));when=dt.datetime.fromisoformat(detected.replace('Z','+00:00'))
        if not at.tzinfo or not when.tzinfo:return False
    except (ValueError,TypeError,AttributeError):return False
    body=dict(source='RentCast',source_id=p.get('id'),address=p.get('formattedAddress'),street_address=p.get('addressLine1'),unit=p.get('addressLine2'),city=p.get('city'),state=p.get('state'),zip_code=p.get('zipCode'),price_cents=price,previous_cents=money(previous),observed_at=at.isoformat(),detected_at=when.isoformat(),observation_id=observation_id,kind=kind,historical=historical,listing_status=p.get('status'),non_tax_eligible=eligible)
    identity=hashlib.sha256((str(p.get('id'))+'|'+observation_id+'|'+at.isoformat()+'|'+str(price)).encode()).hexdigest();key=PREFIX+identity
    old=store.execute('SELECT payload FROM settings WHERE name=?',(key,)).fetchone()
    if old:return False
    store.save_setting(key,dict(observation=body,attempts=0,synced_at=None,error_type=None));return True

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

def flush(store,config=None,now=None,opener=None):
    config=config or review_configuration()
    if not config:return {'state':'disabled'}
    now=now or dt.datetime.now(dt.timezone.utc);opener=opener or urllib.request.build_opener(NoRedirect)
    url,key,token=config;url=url.rsplit('/',1)[0]+'/review-price-observations'
    pending=[]
    for name,raw in store.execute('SELECT name,payload FROM settings WHERE name LIKE ?',(PREFIX+'%',)).fetchall():
        row=json.loads(raw)
        if not row.get('synced_at'):pending.append((name,row))
    pending.sort(key=lambda r:(r[1]['observation']['observed_at'],r[1]['observation']['source_id'],0 if r[1]['observation']['kind']=='price_change' else 1,r[0]))
    sent=0;coverage=None
    for offset in range(0,min(len(pending),900),BATCH_SIZE):
        part=pending[offset:offset+BATCH_SIZE];payload={'observations':[r['observation'] for name,r in part]};payload['batch_id']=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest();body=json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode()
        for name,row in part:row['attempts']+=1;store.save_setting(name,row)
        try:
            with opener.open(signed_request((url,key,token),body,now),timeout=30) as response:
                result=json.loads(response.read(200000))
                if response.status!=200 or result.get('accepted') is not True or result.get('batch_id')!=payload['batch_id']:raise ValueError('Invalid price receipt')
            for name,row in part:row.update(synced_at=now.isoformat(),error_type=None);store.save_setting(name,row)
            sent+=len(part);coverage=result.get('coverage')
        except Exception as exc:
            for name,row in part:row['error_type']=type(exc).__name__;store.save_setting(name,row)
            event('price_sync_deferred',error_type=type(exc).__name__,http_status=getattr(exc,'code',None),pending=len(pending)-sent);return {'state':'deferred','sent':sent,'pending':len(pending)-sent}
    summary={k:v for k,v in (coverage or {}).items() if k!='properties'}
    event('price_sync_accepted',observations=sent,pending=max(0,len(pending)-sent),coverage=summary)
    return {'state':'accepted','sent':sent,'pending':max(0,len(pending)-sent),'coverage':summary}

def safe_current_baselines(store,boundary):
    """After report imports, establish newly tracked candidates without old alerts."""
    try:
        if not review_configuration():return {'state':'disabled'}
        from .core import verification
        for identity,raw,observed in store.execute('SELECT id,payload,observed_at FROM properties WHERE source=?',(LIVE,)).fetchall():
            p=json.loads(raw)
            if verification(p,boundary)[0]=='Candidate':
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
