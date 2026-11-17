"""One owner-authorized Nov 17 metadata-batch diagnosis; never discovery/mail.

Permanent failures remain held. This single replay preserves the failed receipt,
uses its exact frozen metadata, and records a separate destination response.
"""
import datetime as dt
import hashlib
import json
import re
import urllib.error
import urllib.request
from .command_center import review_configuration,signed_request
from .request import NoRedirect
from .sync_runtime import get,budget_timeout,SyncFailure

JOB='listing-contact-enrichment:2026-11-17:evening:property-quality-v1'
KEY='incident_batch_revalidation:20261117:v1'

def safe_hint(value):
    text=str(value)[:1000]
    text=re.sub(r'https?://\S+|[\w.+-]+@[\w.-]+|[A-Za-z0-9_+/=-]{32,}','[redacted]',text)
    return re.sub(r'[\x00-\x1f<>]',' ',text)[:240]

def run(store,now,opener=None):
    if get(store,KEY):return get(store,KEY)
    job=get(store,JOB,{})
    frozen=job.get('inflight') or {};body=frozen.get('body')
    if not isinstance(body,str):return {'state':'not_required','provider_calls':0}
    identity=JOB+':'+str(job.get('offset'))+':'+str(job.get('cursor'))
    operation_key='sync_operation:listing_enrichment:'+hashlib.sha256(identity.encode()).hexdigest()
    operation=get(store,operation_key,{})
    if operation.get('state')!='dead_letter' or operation.get('error',{}).get('http_status')!=400:return {'state':'not_required','provider_calls':0}
    config=review_configuration()
    if not config:return {'state':'not_configured','provider_calls':0}
    if operation.get('digest')!=hashlib.sha256(body.encode()).hexdigest():return {'state':'frozen_digest_mismatch','provider_calls':0}
    try:timeout=budget_timeout(20)
    except SyncFailure:return {'state':'deferred_time_slice','requests':0,'provider_calls':0}
    url,key,token=config;config=(url.rsplit('/',1)[0]+'/review-listing-enrichment',key,token)
    audit={'state':'attempting','at':now.isoformat(),'job_key':JOB,'offset':job.get('offset'),'records':frozen.get('records'),
           'payload_sha256':operation['digest'],'requests':1,'provider_calls':0,'email_sends':0,'original_execution_confirmed':False}
    # The shared recovery lock and persisted intent prevent duplicate probes.
    claimed=store.execute('INSERT INTO settings(name,payload) VALUES (?,?) ON CONFLICT(name) DO NOTHING',(KEY,json.dumps(audit))).rowcount
    if not claimed:return get(store,KEY)
    opener=opener or urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(signed_request(config,body.encode(),now),timeout=timeout) as response:
            raw=response.read(524289)
            if response.status!=200 or len(raw)>524288:raise ValueError('Invalid bounded replay response')
            result=json.loads(raw)
        if result.get('accepted') is not True:raise ValueError('Metadata acknowledgment missing')
        audit.update(state='accepted',receipt={'accepted':True,'matched':result.get('matched'),'next_cursor':result.get('next_cursor')})
        # Do not manufacture an acknowledgment for the original failed call.
        # Its batch can progress using this separately recorded actual receipt.
        job.update(offset=job['offset']+frozen['records'],cursor=result.get('next_cursor'),batches=job['batches']+1,
                   summary=result.get('summary',{}),last_delivery={'accepted':True,'records':frozen['records'],'matched':result.get('matched'),'at':now.isoformat(),'revalidation':KEY})
        job.pop('inflight',None)
        from .collector import LIVE
        total=len(job['source_ids']) if job.get('source_ids') is not None else store.execute('SELECT COUNT(*) FROM properties WHERE source=?',(LIVE,)).fetchone()[0]
        job['done']=job['offset']>=total and job['cursor'] is None
        if job['cursor'] is None and not job['done']:job['cursor']=10000
        store.save_setting(JOB,job)
        store.save_setting(operation_key,{**operation,'state':'reconciled','reconciled_at':now.isoformat(),
            'original_execution_confirmed':False,'reconciliation_reason':'separate_exact_batch_revalidation_acknowledged','revalidation':KEY})
    except urllib.error.HTTPError as error:
        raw=error.read(4096)
        try:hint=json.loads(raw).get('error','Unclassified destination response')
        except (ValueError,AttributeError):hint='Non-JSON destination response'
        audit.update(state='held',http_status=error.code,error_hint=safe_hint(hint))
    except Exception as error:
        audit.update(state='held',error_type=type(error).__name__)
    store.save_setting(KEY,audit)
    return audit
