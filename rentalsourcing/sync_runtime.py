"""Durable, bounded sync retries. This module never contacts RentCast or Resend."""
import datetime as dt,hashlib,json,time,urllib.error,urllib.request,uuid,contextvars,functools
from urllib.parse import urlsplit
from .command_center import signed_request
from .request import NoRedirect
UTC=dt.timezone.utc

DEADLINE=contextvars.ContextVar('sync_recovery_deadline',default=None)
def budget_timeout(seconds):
 deadline=DEADLINE.get()
 if deadline is None:return seconds
 remaining=deadline-time.monotonic()
 if remaining<=0:raise SyncFailure('recovery_time_budget',None,True)
 return min(seconds,max(.1,remaining))
def bounded_recovery(function):
 @functools.wraps(function)
 def wrapped(*args,**kwargs):
  token=DEADLINE.set(time.monotonic()+120)
  try:return function(*args,**kwargs)
  finally:DEADLINE.reset(token)
 return wrapped

def utc():return dt.datetime.now(UTC)
def get(store,key,default=None):
 row=store.execute('SELECT payload FROM settings WHERE name=?',(key,)).fetchone()
 return json.loads(row[0]) if row else default

def attempt_count(store,name,identity):return get(store,'sync_operation:'+name+':'+hashlib.sha256(identity.encode()).hexdigest(),{}).get('attempts',0)

def component(store,name,state,*,now=None,processed=0,pending=0,error=None,recovery=None):
 now=now or utc();old=get(store,'health_component:'+name,{})
 value={**old,'component':name,'last_attempt_at':now.isoformat(),'state':state,'processed':processed,'pending':pending,'error':error,'recovery':recovery}
 if state=='success':value['last_success_at']=now.isoformat()
 store.save_setting('health_component:'+name,value)
 return value

class SyncFailure(RuntimeError):
 def __init__(self,category,status=None,retryable=False,endpoint=None,operation=None):
  super().__init__('Synchronization '+category);self.category=category;self.http_status=status;self.retryable=retryable;self.endpoint=endpoint;self.operation=operation
 def safe(self):return {'category':self.category,'http_status':self.http_status,'retryable':self.retryable,'endpoint':self.endpoint,'operation':self.operation}

def classify(exc,endpoint=None,operation=None):
 if isinstance(exc,SyncFailure):return exc
 status=getattr(exc,'code',None)
 if isinstance(exc,urllib.error.HTTPError):
  if status in (301,302,307,308):return SyncFailure('endpoint_redirect',status,False,endpoint,operation)
  if status in (401,403):return SyncFailure('authentication',status,False,endpoint,operation)
  if status==429:return SyncFailure('rate_limit',status,True,endpoint,operation)
  if status>=500:
   try:
    body=json.loads(exc.read(4096));known=body.get('error') in ('Synchronization is not configured','Synchronization is disabled')
   except Exception:known=False
   return SyncFailure('configuration' if known else 'server',status,not known,endpoint,operation)
  return SyncFailure('contract' if status in (400,404,409,413,415,422) else 'http',status,False,endpoint,operation)
 if isinstance(exc,(TimeoutError,urllib.error.URLError,OSError)):return SyncFailure('timeout' if isinstance(exc,TimeoutError) else 'network',None,True,endpoint,operation)
 return SyncFailure('response_validation',None,False,endpoint,operation)

def incident(store,name,error,*,now=None,resolved=False):
 now=now or utc();stamp=now.isoformat();key='health_incident:'+name
 # Compare-and-swap preserves one episode under concurrent dashboard workers.
 for _ in range(8):
  row=store.execute('SELECT payload FROM settings WHERE name=?',(key,)).fetchone();old=json.loads(row[0]) if row else {}
  if old.get('last_seen_at','')>stamp:return
  if resolved:
   if old.get('state')!='open':return
   value={**old,'state':'resolved','resolved_at':stamp,'last_seen_at':stamp,'automatic_recovery_attempted':True}
  else:
   if old.get('state')!='open':old={'id':uuid.uuid4().hex,'component':name,'opened_at':stamp,'state':'open'}
   value={**old,'last_seen_at':stamp,'error':error,'manual_action':not error.get('retryable',False),'automatic_recovery_attempted':True}
  raw=json.dumps(value)
  if row:
   changed=store.execute('UPDATE settings SET payload=? WHERE name=? AND payload=?',(raw,key,row[0])).rowcount
  else:changed=store.execute('INSERT INTO settings(name,payload) VALUES (?,?) ON CONFLICT(name) DO NOTHING',(key,raw)).rowcount
  if changed:return
 raise RuntimeError('Operational incident update conflicted repeatedly')

def sequence(store):
 key='operational_snapshot_sequence'
 for _ in range(8):
  row=store.execute('SELECT payload FROM settings WHERE name=?',(key,)).fetchone();n=(int(row[0]) if row else 0)+1
  result=store.execute('UPDATE settings SET payload=? WHERE name=? AND payload=?',(str(n),key,row[0])) if row else store.execute('INSERT INTO settings(name,payload) VALUES (?,?) ON CONFLICT(name) DO NOTHING',(key,str(n)))
  if result.rowcount:return n
 raise RuntimeError('Operational snapshot sequence conflicted')


def request_json(store,name,identity,config,body,*,timeout=20,max_bytes=1024*1024,opener=None,now=None,validate=None,poll=False,sleeper=time.sleep):
 url,key,token=config;endpoint=urlsplit(url).path
 digest=hashlib.sha256(body).hexdigest();fingerprint=hashlib.sha256((url+'\0'+key+'\0'+token).encode()).hexdigest()
 circuit=get(store,'sync_circuit:'+name,{})
 if circuit.get('configuration')==fingerprint and circuit.get('blocked'):
  e=circuit['error'];raise SyncFailure(e['category'],e.get('http_status'),False,endpoint,identity)
 ledger_key='sync_operation:'+name+':'+hashlib.sha256(identity.encode()).hexdigest();ledger=get(store,ledger_key,{})
 stamp=now or utc()
 if ledger.get('state')=='success' and not poll:
  if ledger.get('digest')!=digest:raise SyncFailure('frozen_operation_changed',409,False,endpoint,identity)
  return ledger['receipt']
 same=ledger.get('configuration')==fingerprint
 if same and ledger.get('state')=='dead_letter':raise SyncFailure(**{k:v for k,v in ledger['error'].items() if k in ('category','retryable','endpoint','operation')},status=ledger['error'].get('http_status'))
 if same and ledger.get('state')=='pending' and ledger.get('next_attempt_at','')>stamp.isoformat():raise SyncFailure('recovery_scheduled',None,True,endpoint,identity)
 if same and not poll and ledger.get('digest') and ledger['digest']!=digest:raise SyncFailure('frozen_operation_changed',409,False,endpoint,identity)
 ledger={**ledger,'id':identity,'component':name,'endpoint':endpoint,'configuration':fingerprint,'digest':digest,'created_at':ledger.get('created_at',stamp.isoformat()),'attempts':ledger.get('attempts',0),'state':'pending'}
 opener=opener or urllib.request.build_opener(NoRedirect)
 for attempt in range(3):
  stamp=now or utc();ledger.update(last_attempt_at=stamp.isoformat(),attempts=ledger['attempts']+1);store.save_setting(ledger_key,ledger)
  try:
   req=signed_request(config,body,stamp);req.add_header('X-RentalSourcing-Operation',hashlib.sha256(identity.encode()).hexdigest())
   with opener.open(req,timeout=budget_timeout(timeout)) as response:
    raw=response.read(max_bytes+1)
    if response.status!=200 or len(raw)>max_bytes:raise SyncFailure('response_validation',getattr(response,'status',None),False,endpoint,identity)
    result=json.loads(raw)
    if validate:validate(result,raw,response)
   receipt={k:result[k] for k in ('accepted','report_id','batch_id','counts','next_cursor','summary','audit','accepted_reports','state') if k in result}
   ledger.update(state='success',last_success_at=stamp.isoformat(),error=None,next_attempt_at=None,receipt=receipt);store.save_setting(ledger_key,ledger)
   counts=result.get('counts',{});count=counts.get('received',counts.get('processed',result.get('processed',len(result.get('decisions',[])))))
   store.save_setting('sync_circuit:'+name,{'blocked':False,'configuration':fingerprint})
   component(store,name,'success',now=stamp,processed=count,recovery='recovered' if ledger.get('attempts',0)>1 else None);incident(store,name,None,now=stamp,resolved=True)
   return result
  except Exception as exc:
   error=classify(exc,endpoint,identity)
   if error.category in ('authentication','configuration','endpoint_redirect','response_signature') or error.http_status in (404,415):store.save_setting('sync_circuit:'+name,{'blocked':True,'configuration':fingerprint,'error':error.safe()})
   incident(store,name,error.safe(),now=stamp)
   delay=min(300,max(5,2**min(ledger['attempts'],8)))
   retry_after=getattr(exc,'headers',None)
   if retry_after:
    try:delay=max(delay,min(3600,int(retry_after.get('Retry-After','0'))))
    except (ValueError,TypeError):pass
   ledger.update(state='pending' if error.retryable else 'dead_letter',error=error.safe(),next_attempt_at=(stamp+dt.timedelta(seconds=delay)).isoformat() if error.retryable else None);store.save_setting(ledger_key,ledger)
   component(store,name,'partial' if error.retryable else 'failed',now=stamp,pending=1,error=error.safe(),recovery='scheduled' if error.retryable else 'manual_action_required')
   if not error.retryable or attempt==2 or error.http_status==429:raise error from None
   sleeper((.25,1)[attempt])
 raise AssertionError('Unreachable retry state')
