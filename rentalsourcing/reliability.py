"""Stored-data health, recovery and watchdog. No provider scans or email sends."""
import datetime as dt,hashlib,hmac,json,logging,os,threading,time,urllib.request,uuid
from .sync_runtime import get,component,incident,request_json,SyncFailure,sequence,bounded_recovery,budget_timeout
from .command_center import configuration,review_configuration
from .schedule import ISRAEL
from .collector import LIVE
UTC=dt.timezone.utc

def stages(store):return [json.loads(r[0]) for r in store.execute("SELECT payload FROM settings WHERE name LIKE ?",('health_component:%',)).fetchall()]
def begin(store,day,now):
 key='daily_health:'+day;old=get(store,key,{})
 if old.get('state')=='running' and old.get('started_at','')>(now-dt.timedelta(minutes=45)).isoformat():return old
 value={**old,'day':day,'attempt_id':uuid.uuid4().hex,'started_at':now.isoformat(),'last_attempt_at':now.isoformat(),'state':'running','stages':{}}
 store.save_setting(key,value);return value

def stage(store,day,name,state,now,**fields):
 key='daily_health:'+day;run=get(store,key,{'day':day,'started_at':now.isoformat(),'stages':{}})
 run['stages'][name]={'state':state,'at':now.isoformat(),**fields};run['last_attempt_at']=now.isoformat();store.save_setting(key,run)
 error=fields.get('error');component(store,name,state,now=now,processed=fields.get('processed',0),pending=fields.get('pending',0),error=error)
 if state=='failed':incident(store,name,error or {'category':'operation_failed','retryable':False},now=now)
 elif state=='success':incident(store,name,None,now=now,resolved=True)

def snapshot(store,now=None):
 now=now or dt.datetime.now(UTC);day=now.astimezone(ISRAEL).date().isoformat();daily=get(store,'daily_health:'+day)
 if not daily:
  out=store.execute('SELECT id,state,updated_at FROM outbox WHERE job_id=?',('daily:'+day,)).fetchone()
  daily={'day':day,'state':'partial' if out else 'not_observed','evidence':'legacy_outbox' if out else 'no_record','email_state':out[1] if out else None,'last_attempt_at':out[2] if out else None,'stages':{}}
 comps=stages(store)
 for name in ('scheduled_run','discovery','listing_processing','eligibility_filtering','tax_verification','property_storage','price_history','review_feedback','property_sync','price_sync','inventory_sync','listing_enrichment','email_delivery','consistency','health_delivery'):
  if not any(c['component']==name for c in comps):comps.append({'component':name,'state':'not_recorded','last_success_at':None,'last_attempt_at':None,'processed':0,'pending':0,'evidence':'No per-stage telemetry recorded'})
 ops=[json.loads(r[0]) for r in store.execute('SELECT payload FROM settings WHERE name LIKE ?',('sync_operation:%',)).fetchall()]
 pending_reports=store.execute("SELECT COUNT(*) FROM command_center_reports r JOIN outbox o ON o.id=r.outbox_id WHERE o.state='sent' AND r.synced_at IS NULL").fetchone()[0]
 prices=[json.loads(r[0]) for r in store.execute('SELECT payload FROM settings WHERE name LIKE ?',('price-observation:%',)).fetchall()]
 feedback=get(store,'penten_review_feed',{})
 identities=store.execute('SELECT COUNT(*) FROM properties').fetchone()[0]
 last_job=store.execute('SELECT day,state,stats,updated_at FROM scan_jobs ORDER BY updated_at DESC LIMIT 1').fetchone();stats=json.loads(last_job[2]) if last_job else {}
 pending_tax=store.execute("SELECT COUNT(*) FROM properties WHERE state='Pending Verification'").fetchone()[0]
 sent=store.execute("SELECT COUNT(*) FROM outbox WHERE job_id LIKE ? AND state='sent'",('daily:%',)).fetchone()[0]
 unsent=store.execute("SELECT COUNT(*) FROM outbox WHERE job_id LIKE ? AND state!='sent' AND state NOT IN ('cancelled_stale','cancelled')",('daily:%',)).fetchone()[0]
 incidents=[json.loads(r[0]) for r in store.execute('SELECT payload FROM settings WHERE name LIKE ?',('health_incident:%',)).fetchall()]
 local=now.astimezone(ISRAEL)
 if (local.hour,local.minute)>=(8,45) and daily.get('state')=='not_observed':
  incident(store,'daily_schedule',{'category':'scheduled_run_missing','retryable':False},now=now);incidents=[json.loads(r[0]) for r in store.execute('SELECT payload FROM settings WHERE name LIKE ?',('health_incident:%',)).fetchall()]
 elif daily.get('state') not in ('not_observed','running'):incident(store,'daily_schedule',None,now=now,resolved=True)
 inventory_jobs=[json.loads(r[0]) for r in store.execute("SELECT payload FROM settings WHERE name LIKE ? OR name LIKE ?",('stored-review-inventory:%','listing-contact-enrichment:%')).fetchall()]
 inventory_pending=sum(not (j.get('done') or j.get('synced_at')) for j in inventory_jobs)
 price_pending=sum(not r.get('synced_at') for r in prices)
 total_pending=pending_reports+price_pending+inventory_pending
 progress=get(store,'health_backlog_progress',{})
 delivered=max((c.get('last_success_at','') for c in comps if c['component'] in ('property_sync','price_sync','inventory_sync','listing_enrichment') and c.get('processed',0)>0),default='')
 if not total_pending:
  incident(store,'sync_backlog',None,now=now,resolved=True);progress={'last_pending':0,'last_progress_at':now.isoformat()}
 elif not progress or total_pending<progress.get('last_pending',0) or delivered>progress.get('last_progress_at',''):
  progress={'last_pending':total_pending,'last_progress_at':now.isoformat()}
 elif progress.get('last_progress_at','')<(now-dt.timedelta(minutes=30)).isoformat():incident(store,'sync_backlog',{'category':'synchronization_backlog_stalled','retryable':True},now=now)
 store.save_setting('health_backlog_progress',progress)
 incidents=[json.loads(r[0]) for r in store.execute('SELECT payload FROM settings WHERE name LIKE ?',('health_incident:%',)).fetchall()]
 blocked=sum(op.get('state')=='dead_letter' for op in ops)+store.execute('SELECT COUNT(*) FROM settings WHERE name LIKE ?',('sync_hold:%',)).fetchone()[0];pending=sum(op.get('state')=='pending' for op in ops)
 opens=[i for i in incidents if i.get('state')=='open']
 if daily.get('state')=='running' and daily.get('started_at','')<(now-dt.timedelta(minutes=45)).isoformat():incident(store,'daily_schedule',{'category':'scheduled_run_stalled','retryable':False},now=now)
 if unsent:incident(store,'email_delivery',{'category':'unsent_report_requires_safe_recovery','retryable':False},now=now)
 else:incident(store,'email_delivery',None,now=now,resolved=True)
 incidents=[json.loads(r[0]) for r in store.execute('SELECT payload FROM settings WHERE name LIKE ?',('health_incident:%',)).fetchall()];opens=[i for i in incidents if i.get('state')=='open']
 effective='failed' if daily.get('state')=='failed' or any(i.get('component') in ('daily_schedule','email_delivery') for i in opens) else 'partial' if pending_reports or pending or blocked or opens or price_pending or inventory_pending or daily.get('state') in ('partial','not_observed','running') else 'success'
 budget=get(store,'health_budget',{})
 return {'schema_version':1,'revision':sequence(store),'generated_at':now.isoformat(),'state':effective,'daily':daily,'components':comps,'counts':{'stored_properties':identities,'tax_pending':pending_tax,'tax_actionable_backlog':get(store,'health_funnel',{}).get('prioritized_tax_backlog',{}).get('total'),'eligible_non_tax':get(store,'health_funnel',{}).get('counts',{}).get('non_tax_pass'),'sent_reports':sent,'unsent_reports':unsent,'pending_reports':pending_reports,'pending_price_observations':price_pending,'pending_inventory_jobs':inventory_pending,'pending_operations':pending,'dead_letters':blocked},'last_scan':{'day':last_job[0],'state':last_job[1],'at':last_job[3],'received':stats.get('received',stats.get('received_this_run')),'discovery_state':stats.get('discovery_state'),'partial':stats.get('partial')} if last_job else None,'feedback':{k:feedback.get(k) for k in ('fetched_at','decisions','snapshot_version','decision_digest','applied','unchanged','stale_ignored','status_updates_after_previous_fetch','older_status_mismatches','new_source_identities')},'budget':budget,'incidents':[{k:v for k,v in i.items() if k not in ('private',)} for i in incidents[-20:]],'last_recovery_at':max((i.get('resolved_at') for i in incidents if i.get('resolved_at')),default=None)}

def publish(store,now=None,config=None,opener=None):
 config=config or review_configuration()
 if not config:return {'state':'disabled'}
 now=now or dt.datetime.now(UTC);report=snapshot(store,now);body=json.dumps(report,sort_keys=True,separators=(',',':')).encode();url,key,token=config;config=(url.rsplit('/',1)[0]+'/review-operations',key,token)
 def validate(d,*args):
  if d.get('accepted') is not True:raise ValueError('Health receipt missing')
 return request_json(store,'health_delivery','health-current',config,body,opener=opener,now=now,validate=validate,poll=True)

def finish(store,day,now):
 # Evaluate actual acknowledgments, not process exit status or a successful email alone.
 run=get(store,'daily_health:'+day,{'day':day,'stages':{}});view=snapshot(store,now)
 failures=[r for r in run.get('stages',{}).values() if r.get('state')=='failed']
 state='failed' if failures and any(k in ('email_delivery','discovery','daily_workflow') and v.get('state')=='failed' for k,v in run.get('stages',{}).items()) else 'partial' if failures or any(v.get('state')=='partial' for v in run.get('stages',{}).values()) or view['counts']['pending_reports'] or view['counts']['pending_operations'] or view['counts']['dead_letters'] or view['counts']['pending_price_observations'] or view['counts']['pending_inventory_jobs'] or any(i.get('state')=='open' for i in view['incidents']) else 'success'
 run.update(state=state,finished_at=now.isoformat());store.save_setting('daily_health:'+day,run);return state

def audit(store,boundary,now=None,config=None,opener=None):
 """Read-only source counts plus a remote signed feed. No business records changed."""
 from .sync_probe import probe
 now=now or dt.datetime.now(UTC)
 reviews={name[14:]:json.loads(raw) for name,raw in store.execute('SELECT name,payload FROM settings WHERE name LIKE ?',('penten_review:%',)).fetchall()}
 reports=store.execute("SELECT r.outbox_id,r.payload,r.synced_at,r.attempts,r.error_type FROM command_center_reports r JOIN outbox o ON o.id=r.outbox_id WHERE o.state='sent'").fetchall()
 return {'source_records':store.execute('SELECT COUNT(*) FROM properties').fetchone()[0],'price_history_records':store.execute('SELECT COUNT(*) FROM price_history').fetchone()[0],'cached_review_decisions':len(reviews),'cached_rejections':sum(r.get('status')=='rejected' for r in reviews.values()),'accepted_reports':sum(bool(r[2]) for r in reports),'pending_reports':sum(not r[2] for r in reports),'pending_report_cards':sum(len(json.loads(r[1]).get('opportunities',[])) for r in reports if not r[2]),'private_feedback_probe':probe(config or review_configuration(),opener)}

@bounded_recovery
def recover(store,boundary,now=None,opener=None):
 """Only saved sync operations; explicitly never sends emails or calls a listing provider."""
 now=now or dt.datetime.now(UTC);result={}
 from .review_feedback import refresh
 manual=get(store,'manual_recovery_requested',{})
 if manual.get('state')=='queued_no_email_or_provider_calls':
  from .sync_probe import probe
  if probe(review_configuration(),opener).get('state')=='authenticated':
   for name,raw in store.execute('SELECT name,payload FROM settings WHERE name LIKE ?',('sync_circuit:%',)).fetchall():
    value=json.loads(raw)
    if value.get('blocked') and value.get('error',{}).get('category') in ('authentication','configuration','endpoint_redirect'):
     store.save_setting(name,{**value,'blocked':False,'manually_rechecked_at':now.isoformat()})
   for name,raw in store.execute('SELECT name,payload FROM settings WHERE name LIKE ?',('sync_operation:%',)).fetchall():
    value=json.loads(raw)
    if value.get('state')=='dead_letter' and value.get('error',{}).get('category') in ('authentication','configuration','endpoint_redirect'):
     store.save_setting(name,{**value,'state':'pending','next_attempt_at':None})
  store.save_setting('manual_recovery_requested',{**manual,'state':'rechecked','checked_at':now.isoformat()})
 baseline='reliability_audit:2026-11-03:v1'
 if not get(store,baseline):
  try:
   before={**audit(store,boundary,now,opener=opener),'receiver':remote_audit(store,opener=opener)}
   store.save_setting(baseline,{'before':before})
   if before['pending_reports']:
    incident(store,'property_sync',{'category':'legacy_delivery_unacknowledged','retryable':True},now=now)
    try:publish(store,now,opener=opener)
    except Exception:pass
   logging.getLogger(__name__).info('RELIABILITY_AUDIT_BEFORE '+json.dumps(before,sort_keys=True))
  except Exception as exc:
   logging.getLogger(__name__).warning('RELIABILITY_AUDIT_DEFERRED '+type(exc).__name__);return {'audit':{'state':'blocked_before_recovery','category':type(exc).__name__}}
 try:result['feedback']=refresh(store,now,opener,review_configuration() or configuration())
 except Exception as e:
  from .sync_runtime import classify
  error=classify(e,operation='review-feedback');component(store,'review_feedback','failed' if not error.retryable else 'partial',now=now,pending=1,error=error.safe());incident(store,'review_feedback',error.safe(),now=now);result['feedback']={'state':'failed',**error.safe()}
 from .command_center import flush
 try:
  config=configuration()
  if not config and review_configuration():
   u,k,t=review_configuration();config=(u.rsplit('/',1)[0]+'/review-reports',k,t)
  result['reports']={'accepted':flush(store,now,opener,config_override=config)}
 except Exception as e:result['reports']={'state':'deferred','category':type(e).__name__}
 from .price_sync import safe_flush
 result['prices']=safe_flush(store)
 try:
  from .diagnostics import funnel
  store.save_setting('health_funnel',funnel(store,boundary,now))
 except Exception:pass
 pending_inventory(store,boundary,now,opener)
 try:
  previous_audit=get(store,'health_last_audit',{})
  need_audit=not previous_audit or previous_audit.get('at','')<(now-dt.timedelta(minutes=55)).isoformat() or store.execute("SELECT COUNT(*) FROM command_center_reports WHERE synced_at IS NULL").fetchone()[0]>0
  after=remote_audit(store,opener=opener) if need_audit else previous_audit['result'];result['audit']=after
  if need_audit:store.save_setting('health_last_audit',{'at':now.isoformat(),'result':after})
  good=after.get('feedback_matches') and not any(after.get(k) for k in ('remote_unaccepted_reports','missing_source_identities','digest_conflicts'))
  error={'category':'cross_system_drift','retryable':True}
  component(store,'consistency','success' if good else 'partial',now=now,processed=after.get('reports',0),pending=0 if good else 1,error=None if good else error,recovery='reconciled' if good else 'scheduled')
  incident(store,'consistency',None if good else error,now=now,resolved=good)
  saved=get(store,baseline,{})
  if 'before' in saved and 'after' not in saved:
   saved['after']={**audit(store,boundary,now,opener=opener),'receiver':after};store.save_setting(baseline,saved);logging.getLogger(__name__).info('RELIABILITY_AUDIT_AFTER '+json.dumps(saved['after'],sort_keys=True))
 except Exception as exc:result['audit']={'state':'deferred','category':type(exc).__name__}
 try:result['health']=publish(store,now,opener=opener)
 except Exception as e:result['health']={'state':'deferred','category':type(e).__name__}
 return result

def start_watchdog(url,boundary):
 """Existing dashboard workers, no new service. Session lock prevents overlap with scans."""
 if os.getenv('ENABLE_REVIEW_INVENTORY_SYNC','false').lower()!='true':return
 def loop():
  from .storage import Store
  while True:
   store=None
   try:
    store=Store(url,initialize=False)
    local=dt.datetime.now(UTC).astimezone(ISRAEL);protected=(local.hour==7 and local.minute>=50) or (local.hour==8 and local.minute<20)
    if protected:
     try:publish(store)
     except Exception:pass
    elif store.lock() and get(store,'watchdog_lease',{}).get('at','')<(dt.datetime.now(UTC)-dt.timedelta(seconds=240)).isoformat():
     store.save_setting('watchdog_lease',{'at':dt.datetime.now(UTC).isoformat()})
     value=recover(store,boundary);logging.getLogger(__name__).info('RELIABILITY_WATCHDOG '+json.dumps({'feedback':value.get('feedback',{}),'reports':value.get('reports'),'prices':value.get('prices',{}).get('state'),'health':value.get('health',{}).get('accepted')}))
   except Exception as e:
    logging.getLogger(__name__).error('RELIABILITY_WATCHDOG_FAILED '+type(e).__name__)
    if store:
     try:
      error={'category':'watchdog_runtime_error','retryable':True};component(store,'watchdog','failed',error=error);incident(store,'watchdog',error)
     except Exception:pass
   finally:
    if store:store.close()
   time.sleep(300)
 threading.Thread(target=loop,name='penten-sync-recovery',daemon=True).start()

def remote_audit(store,config=None,opener=None):
 """Pure read-only cross-system comparison. Identifiers are never emitted in logs."""
 from .command_center import signed_request
 from .request import NoRedirect
 config=config or review_configuration()
 if not config:return {'state':'disabled'}
 url,key,token=config;url=url.rsplit('/',1)[0]+'/review-reconcile';opener=opener or urllib.request.build_opener(NoRedirect)
 reports=store.execute("SELECT r.payload,r.synced_at FROM command_center_reports r JOIN outbox o ON o.id=r.outbox_id WHERE o.state='sent' ORDER BY o.job_id").fetchall()
 counts={'reports':len(reports),'remote_unaccepted_reports':0,'missing_source_identities':0,'digest_conflicts':0,'mapped_rejected_cards':0,'intentionally_excluded_missing':0};authority=None
 from .core import verification
 from .geography import load_boundary
 from .config import Settings
 boundary=load_boundary(Settings.from_env().boundary_file)
 for raw,synced in reports:
  p=json.loads(raw);ids=sorted({c['source_id'] for c in p.get('opportunities',[]) if c.get('source_id')});known=None
  exclusions=[]
  for source_id in ids:
   prop=store.get_property(source_id);review=get(store,'penten_review:'+source_id,{})
   if prop and verification(prop['payload'],boundary)[0]=='Rejected' and review.get('status') not in ('pursue','monitor','rejected') and not review.get('contract_state'):exclusions.append(source_id)
  for offset in range(0,max(1,len(ids)),25):
   nonce=uuid.uuid4().hex;body=json.dumps({'nonce':nonce,'reports':[{'report_id':p['report_id'],'digest':hashlib.sha256(raw.encode()).hexdigest(),'source_ids':ids[offset:offset+25],'excluded_source_ids':[i for i in ids[offset:offset+25] if i in exclusions]}]},separators=(',',':')).encode()
   with opener.open(signed_request((url,key,token),body,dt.datetime.now(UTC)),timeout=budget_timeout(20)) as response:
    data=response.read(65537)
    if len(data)>65536:raise ValueError('Audit response oversized')
    signature=response.headers.get('X-RentalSourcing-Response-Signature','')
    if not hmac.compare_digest(signature,'v1='+hmac.new(key.encode(),data,hashlib.sha256).hexdigest()):raise ValueError('Audit signature mismatch')
    result=json.loads(data)
    if result.get('nonce')!=nonce or result.get('accepted') is not True:raise ValueError('Audit receipt mismatch')
   authority=result['authority'];r=result['reports'][0];known=r['accepted'];counts['missing_source_identities']+=r.get('required_missing',r['missing']);counts['intentionally_excluded_missing']+=r.get('intentionally_excluded_missing',0);counts['mapped_rejected_cards']+=r['rejected']
   if known and not r['digest_matches']:counts['digest_conflicts']+=1
  if not known:counts['remote_unaccepted_reports']+=1
 if authority is None:
  nonce=uuid.uuid4().hex;body=json.dumps({'nonce':nonce,'reports':[]}).encode()
  with opener.open(signed_request((url,key,token),body,dt.datetime.now(UTC)),timeout=budget_timeout(20)) as response:
   data=response.read(65536);result=json.loads(data)
   if result.get('nonce')!=nonce or not hmac.compare_digest(response.headers.get('X-RentalSourcing-Response-Signature',''),'v1='+hmac.new(key.encode(),data,hashlib.sha256).hexdigest()):raise ValueError('Invalid authority assertion')
   authority=result['authority']
 feed=get(store,'penten_review_feed',{})
 return {**counts,'authority':authority,'feedback_matches':bool(authority and feed.get('decision_digest')==authority['snapshot_version'])}

def pending_inventory(store,boundary,now,opener=None):
 config=review_configuration()
 if not config:return
 from .review_inventory import run as inventory
 from .listing_enrichment import run as listing
 for prefix,runner,component_name in [('stored-review-inventory:',inventory,'inventory_sync'),('listing-contact-enrichment:',listing,'listing_enrichment')]:
  jobs=store.execute('SELECT name,payload FROM settings WHERE name LIKE ? ORDER BY name',(prefix+'%',)).fetchall();done=0
  for name,raw in jobs:
   ledger=json.loads(raw)
   if ledger.get('synced_at') or ledger.get('done'):continue
   if done>=2:break
   hold='sync_hold:'+hashlib.sha256(name.encode()).hexdigest()
   if get(store,hold):continue
   try:
    if runner is inventory:runner(store,boundary,now,execute=True,download=False,config=config,opener=opener,job_key=name,refresh_decisions=False)
    else:runner(store,config,opener=opener,job_key=name)
   except Exception as exc:
    if isinstance(exc,ValueError) and 'no longer passes' in str(exc):
     error={'category':'frozen_selection_no_longer_eligible','retryable':False};store.save_setting(hold,{'job':name,'component':component_name,'at':now.isoformat(),'error':error});component(store,component_name,'failed',now=now,pending=1,error=error,recovery='manual_action_required');incident(store,component_name,error,now=now)
   done+=1
