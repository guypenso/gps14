"""One bounded, idempotent discovery recovery. Never constructs or sends a digest."""
import datetime as dt
import json
from dataclasses import replace
from .collector import Client
from .engine import Engine
from .schedule import ISRAEL
from .storage import BudgetExceeded

class ListingOnlyClient(Client):
    def get(self,path,params=None,cache=True):
        if path!='/listings/sale':raise PermissionError('Catch-up authorizes sale-listing retrieval only')
        return super().get(path,params,cache)

def run(store,boundary,settings,day,*,clock=None,opener=None,deliver=True):
    """Caller holds Store.lock(). Request-scope reservation survives interrupted runs."""
    from .cloud import scheduled_scope
    from .command_center import review_configuration
    clock=clock or (lambda:dt.datetime.now(dt.timezone.utc));now=clock()
    if day!=now.astimezone(ISRAEL).date().isoformat():raise ValueError('Catch-up must target today in Asia/Jerusalem')
    if settings.approval_mode!='monthly_budget' or not settings.supplemental_requests:raise BudgetExceeded('Verified current-cycle supplemental approval is required')
    key='catchup-discovery:'+day
    saved=store.execute('SELECT payload FROM settings WHERE name=?',(key,)).fetchone();prior=json.loads(saved[0]) if saved else {}
    if prior.get('state')=='complete':return {**prior,'already_executed':True,'requests_this_invocation':0}
    effective=replace(settings,per_run=1,max_pages=1,retries=0,email_enabled=False)
    if not store.budget_health(effective,now,record_warning=False)['remaining'] and not prior.get('job_id'):raise BudgetExceeded('No verified allowance remains for catch-up')
    if prior.get('job_id'):
        job,query,next_scope=prior['job_id'],prior['query'],prior['next_scope']
    else:
        job,query,next_scope=scheduled_scope(store,day,'catchup',boundary)
        prior={'state':'processing','job_id':job,'query':query,'next_scope':next_scope,'started_at':now.isoformat(),'requests':0}
        store.save_setting(key,prior)
    client=ListingOnlyClient(store,effective,clock,opener,request_scope=(key,1))
    before={r[0] for r in store.execute('SELECT id FROM properties').fetchall()}
    try:stats=prior.get('stats') or Engine(store,boundary,client,effective,clock,allow_paid_tax=False,query_parameters=query).run(job,day)
    except Exception as error:
        store.save_setting(key,{**prior,'state':'pending_recovery','error_type':type(error).__name__,'requests':store.count_usage('request_scope:'+key),'last_attempt_at':clock().isoformat()});raise
    if stats.get('discovery_complete'):
        from .investment_policy import POLICY_VERSION
        store.save_setting('coverage_scope:'+POLICY_VERSION,{'cursor':next_scope,'completed_job':job,'at':clock().isoformat()})
    after={r[0] for r in store.execute('SELECT id FROM properties').fetchall()}
    receipt={**prior,'state':'complete','job_id':job,'started_at':prior['started_at'],'completed_at':clock().isoformat(),'requests':store.count_usage('request_scope:'+key),'requests_this_invocation':client.count,'listing_observations':stats.get('received_this_run',0),'new_source_ids':prior.get('new_source_ids',sorted(after-before)),'stats':stats,'outgoing_emails':0,'paid_tax_or_rent_requests':0}
    if deliver:
        from .future_discovery import process
        config=review_configuration()
        receipt['delivery']=process(store,boundary,job,clock(),config=config,opener=opener)
        from .price_sync import safe_flush
        receipt['price_sync']=safe_flush(store)
        if receipt['delivery']['state'] in {'pending','pending_recovery','not_configured'}:receipt['state']='pending_delivery'
    store.save_setting(key,receipt);store.save_setting('health_budget',store.budget_health(settings,clock(),record_warning=False))
    return receipt
